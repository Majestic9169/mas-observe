#!/usr/bin/env python3
"""
bridge.py - raw sslsniff + process JSONL  ->  trace.log for AgentSight's
script/multi_signal_correlator.py and script/llm_semantic_analyzer.py.

  sslsniff  (TLS plaintext fragments) --HTTP/1.1 reassembly--> source="http_parser"
  process   (EXEC/EXIT/FILE_OPEN/...) --field renames-------->  source="process"

Output, one JSON object per line, sorted by time:
  {"timestamp": <int ns, kernel CLOCK_MONOTONIC>, "source": ..., "data": {...}}

Timestamps are deliberately kept in nanoseconds on the kernel monotonic clock:
both eBPF programs stamp events with bpf_ktime_get_ns(), and the Python
correlator does its window maths in ns. (AgentSight's Rust TimestampNormalizer
converts to epoch ms; do NOT mix that output with this one.)

Usage:
  python3 bridge.py --ssl ssl.jsonl --proc proc.jsonl -o trace.jsonl \
                    --comm python
"""
import argparse
import gzip
import json
import sys
import zlib
from collections import Counter, defaultdict, deque

SECRET_HEADERS = {"authorization", "x-api-key", "cookie", "proxy-authorization"}
LLM_PATHS = ("/api/chat", "/api/generate", "/v1/chat/completions",
             "/v1/completions", "/v1/messages", "/v1/responses")
METHODS = (b"GET ", b"POST ", b"PUT ", b"DELETE ", b"PATCH ", b"HEAD ", b"OPTIONS ")


# --------------------------------------------------------------------------
# HTTP/1.1 reassembly (one instance per connection and direction)
# --------------------------------------------------------------------------
def dechunk(buf, pos):
    """Decode a chunked body starting at pos. -> (body, next_pos) or None if incomplete."""
    out = bytearray()
    while True:
        eol = buf.find(b"\r\n", pos)
        if eol < 0:
            return None
        size = int(buf[pos:eol].split(b";")[0].strip() or b"0", 16)  # ValueError -> junk
        pos = eol + 2
        if size == 0:
            while True:  # trailers, then the final blank line
                eol = buf.find(b"\r\n", pos)
                if eol < 0:
                    return None
                if eol == pos:
                    return bytes(out), pos + 2
                pos = eol + 2
        if len(buf) < pos + size + 2:
            return None
        out += buf[pos:pos + size]
        pos += size + 2


class H1Stream:
    def __init__(self):
        self.buf = bytearray()
        self.t_first = None
        self.junk = 0

    def feed(self, data, ts):
        if not self.buf:
            self.t_first = ts
        self.buf += data
        out = []
        while self.buf:
            try:
                m = self._parse_one()
            except ValueError:
                self.buf.clear()
                self.junk += 1
                break
            if m is None:
                break
            m["t_first"], m["t_done"] = self.t_first, ts
            out.append(m)
            self.t_first = ts  # leftover bytes start the next message
        return out

    def flush(self, ts):
        """Connection closed: emit a close-delimited response if one is pending."""
        buf = bytes(self.buf)
        end = buf.find(b"\r\n\r\n")
        if end < 0 or not buf.startswith(b"HTTP/"):
            self.buf.clear()
            return []
        m = self._head(buf, end)
        m["body"] = buf[end + 4:]
        m["t_first"], m["t_done"] = self.t_first, ts
        m["close_delimited"] = True
        self.buf.clear()
        return [m]

    @staticmethod
    def _head(buf, end):
        lines = buf[:end].decode("latin-1").split("\r\n")
        start, headers = lines[0], {}
        for ln in lines[1:]:
            k, _, v = ln.partition(":")
            headers[k.strip().lower()] = v.strip()
        m = {"headers": headers, "is_resp": start.startswith("HTTP/")}
        parts = start.split(" ", 2)
        if m["is_resp"]:
            m["status_code"] = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        else:
            m["method"] = parts[0]
            m["target"] = parts[1] if len(parts) > 1 else ""
        return m

    def _parse_one(self):
        buf = bytes(self.buf)
        end = buf.find(b"\r\n\r\n")
        if end < 0:
            plausible = buf.startswith(b"HTTP/") or buf.startswith(METHODS) or len(buf) < 8
            if len(buf) > 65536 or not plausible:
                self.buf.clear()
                self.junk += 1
            return None
        m = self._head(buf, end)
        pos = end + 4
        h = m["headers"]
        status = m.get("status_code", 0)
        if m["is_resp"] and (100 <= status < 200 or status in (204, 304)):
            body, nxt = b"", pos
        elif "chunked" in h.get("transfer-encoding", "").lower():
            r = dechunk(buf, pos)
            if r is None:
                return None
            body, nxt = r
        elif "content-length" in h:
            n = int(h["content-length"])
            if len(buf) < pos + n:
                return None
            body, nxt = buf[pos:pos + n], pos + n
        elif m["is_resp"]:
            return None  # close-delimited: wait for CLOSE
        else:
            body, nxt = b"", pos
        del self.buf[:nxt]
        m["body"] = body
        return m


# --------------------------------------------------------------------------
# LLM payload normalisation (the "Ollama adapter" AgentSight lacks)
# --------------------------------------------------------------------------
def _json_objects(text):
    try:
        return [json.loads(text)]
    except ValueError:
        pass
    objs = []  # NDJSON (Ollama stream:true) or SSE
    for ln in text.splitlines():
        ln = ln.strip()
        if ln.startswith("data:"):
            ln = ln[5:].strip()
        if not ln or ln == "[DONE]":
            continue
        try:
            objs.append(json.loads(ln))
        except ValueError:
            pass
    return objs


def normalize_llm(path, req_json, resp_text):
    objs = [o for o in _json_objects(resp_text) if isinstance(o, dict)]
    if not objs:
        return None
    last = objs[-1]
    model = last.get("model") or (req_json or {}).get("model")
    text, provider, timing = "", "unknown", {}
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    if path.startswith("/api/"):  # Ollama native
        provider = "ollama"
        for o in objs:
            msg = o.get("message") or {}
            text += (msg.get("content") or "") + (o.get("response") or "")
            if msg.get("tool_calls"):
                text += " " + json.dumps(msg["tool_calls"])
        usage = {"prompt_tokens": last.get("prompt_eval_count", 0),
                 "completion_tokens": last.get("eval_count", 0)}
        timing = {k: last.get(k) for k in
                  ("total_duration", "load_duration", "prompt_eval_duration", "eval_duration")}
    elif "choices" in last:  # OpenAI-compatible (also Ollama /v1)
        provider = "openai-compatible"
        for o in objs:
            for c in o.get("choices", []):
                text += ((c.get("message") or {}).get("content") or
                         (c.get("delta") or {}).get("content") or "")
        u = next((o["usage"] for o in reversed(objs) if o.get("usage")), {})
        usage = {"prompt_tokens": u.get("prompt_tokens", 0),
                 "completion_tokens": u.get("completion_tokens", 0)}
    elif isinstance(last.get("content"), list):  # Anthropic
        provider = "anthropic"
        text = " ".join(b.get("text", "") for b in last["content"] if isinstance(b, dict))
        u = last.get("usage", {})
        usage = {"prompt_tokens": u.get("input_tokens", 0),
                 "completion_tokens": u.get("output_tokens", 0)}
    usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
    return {"provider": provider, "model": model, "text": text, "usage": usage, "timing": timing}


# --------------------------------------------------------------------------
# The bridge
# --------------------------------------------------------------------------
def payload_bytes(ev):
    if ev.get("data_hex"):
        return bytes.fromhex(ev["data_hex"])
    return (ev.get("data") or "").encode("utf-8", "replace")


def decode_body(m):
    body, enc = m["body"], m["headers"].get("content-encoding", "").lower()
    try:
        if enc == "gzip":
            body = gzip.decompress(body)
        elif enc == "deflate":
            body = zlib.decompress(body)
    except Exception:
        m["decode_error"] = True
    return body.decode("utf-8", "replace")


class Bridge:
    def __init__(self, comms):
        self.comms = set(comms)
        self.streams = {}
        self.pending = defaultdict(deque)
        self.h2 = set()
        self.out = []
        self.stats = Counter()
        self.xid = 0

    # ---- sslsniff ----
    def ssl_event(self, ev):
        fn = ev.get("function")
        if fn == "CAPTURE_LOSS":
            # ring-buffer loss is tracer-wide: no stream can be trusted any more
            self.stats["capture_loss_resets"] += 1
            self.streams.clear()
            return
        if self.comms and ev.get("comm") not in self.comms:
            self.stats["ssl_skipped_comm"] += 1
            return
        pid, tid = ev.get("pid", 0), ev.get("tid", 0)
        handle = ev.get("transport_handle")
        conn = (pid, ev.get("process_start_ns", 0), handle if handle not in (None, "0x0") else ("tid", tid))
        if fn == "CLOSE":
            for d in ("w", "r"):
                st = self.streams.pop(conn + (d,), None)
                if st:
                    for m in st.flush(ev["timestamp_ns"]):
                        self._emit(m, conn, pid, tid, ev.get("comm", ""))
            return
        if fn not in ("READ/RECV", "WRITE/SEND") or ev.get("is_handshake"):
            return
        data = payload_bytes(ev)
        if not data or conn in self.h2:
            return
        key = conn + ("w" if fn == "WRITE/SEND" else "r",)
        st = self.streams.setdefault(key, H1Stream())
        if not st.buf and data.startswith(b"PRI * HTTP/2"):
            self.h2.add(conn)  # HTTP/2 is out of scope for this bridge
            self.stats["http2_connections_skipped"] += 1
            return
        if ev.get("truncated"):
            self.stats["truncated_records"] += 1
            self.streams.pop(key, None)  # framing cannot be realigned
            return
        for m in st.feed(data, ev["timestamp_ns"]):
            self._emit(m, conn, pid, tid, ev.get("comm", ""))
        self.stats["ssl_junk"] += st.junk
        st.junk = 0

    def _emit(self, m, conn, pid, tid, comm):
        headers = {k: v for k, v in m["headers"].items() if k not in SECRET_HEADERS}
        text = decode_body(m)
        if not m["is_resp"]:
            self.xid += 1
            path = m["target"]
            try:
                reqj = json.loads(text) if text else None
            except ValueError:
                reqj = None
            self.pending[conn].append({"xid": self.xid, "t_done": m["t_done"], "path": path,
                                       "method": m["method"], "host": headers.get("host", ""),
                                       "json": reqj if isinstance(reqj, dict) else None})
            data = {"message_type": "request", "method": m["method"], "path": path,
                    "host": headers.get("host", ""), "headers": headers, "body": text,
                    "pid": pid, "tid": tid, "comm": comm, "exchange_id": self.xid,
                    "start_timestamp": m["t_first"]}
            if isinstance(reqj, dict) and reqj.get("model"):
                data["model"] = reqj["model"]
            self.out.append((m["t_done"], "http_parser", data))
            self.stats["http_requests"] += 1
            return
        if 100 <= m.get("status_code", 0) < 200:
            return
        q = self.pending[conn]
        req = q.popleft() if q else None
        if req is None:
            self.stats["unmatched_responses"] += 1
        data = {"message_type": "response", "status_code": m.get("status_code"),
                "headers": headers, "pid": pid, "tid": tid, "comm": comm,
                "start_timestamp": m["t_first"], "body": text}
        if req:
            data.update({"path": req["path"], "method": req["method"], "host": req["host"],
                         "exchange_id": req["xid"],
                         "latency_ms": (m["t_done"] - req["t_done"]) / 1e6})
            if req["path"].startswith(LLM_PATHS):
                llm = normalize_llm(req["path"], req["json"], text)
                if llm:
                    # The correlator reads OpenAI-shaped bodies; keep the original verbatim.
                    data["raw_body"] = text
                    data["body"] = json.dumps({"model": llm["model"], "usage": llm["usage"],
                                               "choices": [{"message": {"role": "assistant",
                                                                        "content": llm["text"]}}]})
                    data.update(provider=llm["provider"], model=llm["model"],
                                usage=llm["usage"], server_timing=llm["timing"])
                    self.stats["llm_exchanges"] += 1
        self.out.append((m["t_done"], "http_parser", data))
        self.stats["http_responses"] += 1

    # ---- process ----
    def proc_event(self, ev):
        name = ev.get("event")
        if name in (None, "PROBE_LIVENESS", "WARNING", "UNKNOWN", "FILE_OPEN_LOST"):
            self.stats["proc_dropped_" + str(name)] += 1
            return
        d = dict(ev)
        ts = d.pop("timestamp")
        d["syscall"] = name
        # The correlator's argument matcher only reads args/filename/path/command/syscall.
        if name == "FILE_OPEN":
            d["path"] = d.get("filepath", "")
        elif name == "EXEC":
            d["command"] = d.get("full_command", "")
        self.out.append((ts, "process", d))
        self.stats["proc_" + name] += 1

    def finish(self):
        for key, st in list(self.streams.items()):
            if st.buf:
                self.stats["unfinished_streams"] += 1
        self.out.sort(key=lambda t: t[0])  # stable: keeps feed order for equal timestamps
        return [{"timestamp": ts, "source": src, "data": d} for ts, src, d in self.out]


def read_jsonl(path, stats, label):
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                yield json.loads(line)
            except ValueError:
                stats[label + "_bad_lines"] += 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--ssl", required=True, help="sslsniff JSONL")
    ap.add_argument("--proc", required=True, help="process JSONL")
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("--comm", action="append", default=[],
                    help="keep only sslsniff events from this comm (repeatable). Use this to drop "
                         "the TLS-terminator (e.g. socat) side and avoid double capture.")
    a = ap.parse_args()

    b = Bridge(a.comm)
    # Feed sslsniff in capture order: reassembly depends on it.
    for ev in sorted(read_jsonl(a.ssl, b.stats, "ssl"),
                     key=lambda e: (e.get("capture_seq", 0), e.get("timestamp_ns", 0))):
        b.ssl_event(ev)
    for ev in read_jsonl(a.proc, b.stats, "proc"):
        b.proc_event(ev)
    events = b.finish()
    with open(a.output, "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    print(f"wrote {len(events)} events -> {a.output}", file=sys.stderr)
    for k, v in sorted(b.stats.items()):
        print(f"  {k}: {v}", file=sys.stderr)


if __name__ == "__main__":
    main()
