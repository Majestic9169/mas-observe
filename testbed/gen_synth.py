#!/usr/bin/env python3
"""Synthetic sslsniff/process JSONL in the exact field layout the C loaders print.

Two sibling agents (pids 100, 200) under one orchestrator (pid 50), Ollama-native
/api/chat over TLS. Each agent: LLM call -> response -> opens its OWN result file.
Agents are offset by 250 ms so each one's file-open lands inside the other's
500 ms post-response window. Ground truth: a file open belongs to the agent
whose name is in the file.
"""
import json, sys

out_dir = sys.argv[1]
S = 1_000_000_000
BOOT = 86_000 * S  # arbitrary "ns since boot"
seq = [0]
ssl, proc, truth = [], [], []


def rec(ts, fn, pid, tid, handle, data, comm="python", truncated=False):
    seq[0] += 1
    ssl.append({"function": fn, "timestamp_ns": ts, "capture_seq": seq[0], "comm": comm,
                "pid": pid, "len": len(data), "buf_size": len(data), "uid": 1000, "tid": tid,
                "transport_handle": handle, "process_start_ns": BOOT, "ringbuf_reserve_failures": 0,
                "tls_library": "openssl", "connection_closed": False, "latency_ms": 0,
                "is_handshake": False, "data": data.decode("latin-1"), "data_hex": data.hex(),
                "truncated": truncated})


def pevent(ts, event, pid, comm="python", **kw):
    proc.append({"timestamp": ts, "event": event, "comm": comm, "pid": pid, **kw})


def call(agent, pid, handle, t0, i, chunked):
    fname = f"result_{agent}_{i}.json"
    req_body = json.dumps({"model": "qwen2.5:latest", "stream": False,
                           "messages": [{"role": "user", "content": f"triage ticket {agent}{i}"}]}).encode()
    hdr = (f"POST /api/chat HTTP/1.1\r\nHost: 127.0.0.1:11436\r\nUser-Agent: python-requests/2.32\r\n"
           f"Authorization: Bearer SECRET\r\nContent-Type: application/json\r\n"
           f"Content-Length: {len(req_body)}\r\n\r\n").encode()
    rec(t0, "WRITE/SEND", pid, pid, handle, hdr)                 # headers and body: separate SSL_write
    rec(t0 + 1_000_000, "WRITE/SEND", pid, pid, handle, req_body)
    content = json.dumps({"action": "write_file", "args": {"filename": fname, "content": "{}"}})
    resp_json = json.dumps({"model": "qwen2.5:latest", "created_at": "2026-10-29T00:00:00Z",
                            "message": {"role": "assistant", "content": content}, "done": True,
                            "total_duration": 300_000_000, "load_duration": 1_000_000,
                            "prompt_eval_count": 120 + i, "prompt_eval_duration": 50_000_000,
                            "eval_count": 40, "eval_duration": 240_000_000}).encode()
    if chunked:
        mid = len(resp_json) // 2
        body = b"".join(b"%x\r\n%s\r\n" % (len(p), p) for p in (resp_json[:mid], resp_json[mid:])) + b"0\r\n\r\n"
        rh = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n"
    else:
        body = resp_json
        rh = (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n" % len(resp_json))
    wire = rh + body
    cut = len(rh) + 10                                           # response arrives in two TLS reads
    t_resp = t0 + 300 * 1_000_000
    rec(t_resp - 5_000_000, "READ/RECV", pid, pid, handle, wire[:cut])
    rec(t_resp, "READ/RECV", pid, pid, handle, wire[cut:])
    t_open = t_resp + 50 * 1_000_000
    pevent(t_open, "FILE_OPEN", pid, count=1, filepath=f"/work/sandbox/{fname}", flags=577)
    truth.append({"ts": t_open, "pid": pid, "agent": agent, "file": fname})


pevent(BOOT + 0, "EXEC", 50, ppid=1, filename="/usr/bin/python3", full_command="python3 orchestrator.py")
pevent(BOOT + 10_000_000, "EXEC", 100, ppid=50, filename="/usr/bin/python3", full_command="python3 agent.py --name A")
pevent(BOOT + 11_000_000, "EXEC", 200, ppid=50, filename="/usr/bin/python3", full_command="python3 agent.py --name B")
for i in range(5):
    call("A", 100, "0x7f0000a000", BOOT + S + i * S, i, chunked=True)
    call("B", 200, "0x7f0000b000", BOOT + S + i * S + 250_000_000, i, chunked=False)
# noise: unrelated process writing during the run, plus TLS-terminator side (socat) double capture
pevent(BOOT + 2 * S + 100_000_000, "FILE_OPEN", 999, comm="vim", count=1, filepath="/home/u/notes.txt", flags=0)
rec(BOOT + 2 * S + 5_000_000, "READ/RECV", 777, 777, "0x7f0000c000", b"POST /api/chat HTTP/1.1\r\nHost: x\r\n\r\n", comm="socat")
pevent(BOOT + 6 * S, "EXIT", 100, ppid=50, exit_code=0)
pevent(BOOT + 6 * S, "WARNING", 0)

ssl.sort(key=lambda e: e["capture_seq"])
with open(f"{out_dir}/ssl.jsonl", "w") as f:
    f.write("libbpf: some warning line that is not json\n")
    for e in ssl: f.write(json.dumps(e) + "\n")
with open(f"{out_dir}/proc.jsonl", "w") as f:
    for e in proc: f.write(json.dumps(e) + "\n")
json.dump(truth, open(f"{out_dir}/truth.json", "w"))
print(f"ssl={len(ssl)} proc={len(proc)} truth={len(truth)}")
