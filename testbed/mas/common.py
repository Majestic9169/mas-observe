"""Shared plumbing: ground-truth log, Ollama client, HTTPS server/client, tickets, summary."""
import argparse, hashlib, json, os, random, ssl, sys, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parent.parent
CA, KEY = str(ROOT / "certs/cert.pem"), str(ROOT / "certs/key.pem")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "https://127.0.0.1:11436").rstrip("/")
MODEL = os.environ.get("MODEL", "qwen2.5:latest")
PORTS = {"worker": 9101, "critic": 9102}
CATEGORIES = ["billing", "bug", "feature_request", "other"]
URGENCIES = ["low", "high"]
MAX_ATTEMPTS = 3          # first try + max 2 retries

mono = lambda: time.clock_gettime_ns(time.CLOCK_MONOTONIC)
def sha(b): return hashlib.sha256(b if isinstance(b, bytes) else b.encode()).hexdigest()
def derive_seed(*parts): return int(sha("|".join(map(str, parts)))[:8], 16)

# ---------------------------------------------------------------- ground truth
class GT:
    """Append-only JSONL shared by all processes (O_APPEND, one write() per line)."""
    def __init__(self, run_dir, role):
        self.run_id, self.role = Path(run_dir).name, role
        self.fd = os.open(Path(run_dir) / "ground_truth.jsonl",
                          os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    def emit(self, event, **kw):
        rec = {"run_id": self.run_id, "role": self.role, "pid": os.getpid(), "event": event,
               "mono_ns": mono(), "wall_ns": time.time_ns(), **kw}
        os.write(self.fd, (json.dumps(rec) + "\n").encode())

def load_gt(run_dir):
    return [json.loads(l) for l in open(Path(run_dir) / "ground_truth.jsonl")]

# -------------------------------------------------------------------- tickets
TEMPLATES = {
    "billing": ["I was charged {amt} twice for my {plan} plan this month. {urg}",
                "My invoice {n} shows the wrong company name. Please correct it and resend. {urg}",
                "Why did the price of my {plan} subscription go up without notice? I want a refund. {urg}"],
    "bug": ["The {feat} page crashes with a null pointer error every time I click save in {browser}. {urg}",
            "After the last update the {feat} export produces an empty file in {browser}. {urg}",
            "Login fails with error 500 when I open {feat} on {browser}. {urg}"],
    "feature_request": ["Could you add {thing} to the {feat} page? It would save our team a lot of time. {urg}",
                        "It would be great to have {thing} in {feat}. Any plans for that? {urg}",
                        "Please consider supporting {thing} for {feat}. {urg}"],
    "other": ["What are your office hours for phone support on {day}? {urg}",
              "Do you have a partner program for agencies based in {city}? {urg}",
              "Who should I contact about a press inquiry from {city}? {urg}"]}
URG_TEXT = {"high": ["This is urgent, we are blocked.", "Please fix this today, ASAP."],
            "low": ["No rush.", "Whenever you get a chance."]}
FILL = dict(amt=["$49", "$120", "$9.99", "$300"], plan=["Pro", "Team", "Starter", "Enterprise"],
            n=["#4411", "#7302", "#1288"], feat=["dashboard", "reports", "settings", "billing portal"],
            browser=["Firefox", "Chrome", "Safari"], thing=["dark mode", "CSV import", "SSO", "webhooks"],
            day=["Monday", "Friday", "Sunday"], city=["Berlin", "Lagos", "Toronto", "Osaka"])

def make_tickets(seed, n):
    """Deterministic from seed; list order == dispatch order. Label = category (+urgency)."""
    rng = random.Random(seed)
    cats = [CATEGORIES[i % 4] for i in range(n)]
    rng.shuffle(cats)
    out = []
    for i, cat in enumerate(cats):
        urg = rng.choice(URGENCIES)
        fills = {k: rng.choice(v) for k, v in FILL.items()}
        text = rng.choice(TEMPLATES[cat]).format(urg=rng.choice(URG_TEXT[urg]), **fills)
        out.append({"id": f"T{i:02d}", "text": text, "label": {"category": cat, "urgency": urg}})
    return out

# ------------------------------------------------------------------------ llm
def llm_call(gt, messages, *, purpose, step, seed, num_predict=256, fmt=None, fault_id=None,
             ticket_id=None, attempt=None):
    payload = {"model": MODEL, "stream": False, "keep_alive": "30m", "messages": messages,
               "options": {"temperature": 0, "num_ctx": 8192, "num_predict": num_predict, "seed": seed}}
    if fmt:
        payload["format"] = fmt
    ids = {"purpose": purpose, "step": step, "ticket_id": ticket_id, "attempt": attempt,
           "prompt_sha256": sha(json.dumps(messages))}
    if fault_id:
        ids["fault_id"] = fault_id
    gt.emit("llm_start", **ids)
    t = mono()
    try:
        r = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=600, verify=CA)
        r.raise_for_status()
        d = r.json()
    except Exception as e:
        gt.emit("llm_end", **ids, error=repr(e), client_ms=(mono() - t) / 1e6)
        raise
    ms = (mono() - t) / 1e6
    # NB: in Ollama 0.40 total_duration already includes scheduler queueing, so the spec's
    # queue_wait_ms only captures client/TLS overhead; ollama_queue_ms is the real wait.
    busy = (d.get("load_duration", 0) + d.get("prompt_eval_duration", 0) + d.get("eval_duration", 0)) / 1e6
    gt.emit("llm_end", **ids, client_ms=ms, queue_wait_ms=ms - d["total_duration"] / 1e6,
            ollama_queue_ms=d["total_duration"] / 1e6 - busy,
            **{k: d.get(k) for k in ("total_duration", "load_duration", "prompt_eval_count",
               "prompt_eval_duration", "eval_count", "eval_duration", "done_reason")})
    return d["message"]["content"]

# ----------------------------------------------------------- process context
class Ctx: pass

def init(role, extra=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--t0", type=int, required=True)
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--propagate-trace-id", action="store_true")
    if extra:
        extra(ap)
    c = Ctx()
    c.args, c.role = ap.parse_args(), role
    c.run_dir, c.t0, c.seed = Path(c.args.run_dir).resolve(), c.args.t0, c.args.seed
    c.sandbox = c.run_dir / "sandbox"
    c.scenario = json.load(open(c.run_dir / "scenario.json"))
    from .faults import Faults
    c.faults = Faults(c.scenario, c.t0, role)
    c.gt = GT(c.run_dir, role)
    c.gt.emit("proc_start", cmdline=" ".join(sys.argv), ppid=os.getppid())
    ppid = os.getppid()           # die with the runner (no orphans if it is SIGKILLed)
    def watchdog():
        while os.getppid() == ppid:
            time.sleep(1)
        os._exit(3)
    threading.Thread(target=watchdog, daemon=True).start()
    return c

def tls_server(ctx, handle):
    """Threaded HTTPS server; handle(path, body) -> dict. Logs msg_recv/msg_send by body hash."""
    role = ctx.role
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            body = json.loads(raw)
            ids = dict(path=self.path, ticket_id=body.get("ticket_id"), attempt=body.get("attempt"))
            ctx.gt.emit("msg_recv", src="planner", dst=role, body_sha256=sha(raw), kind="req", **ids)
            try:
                out, code = json.dumps(handle(self.path, body), sort_keys=True).encode(), 200
            except Exception as e:
                import traceback; traceback.print_exc()
                out, code = json.dumps({"error": repr(e)}).encode(), 500
            ctx.gt.emit("msg_send", msg_id=uuid.uuid4().hex, src=role, dst="planner",
                        body_sha256=sha(out), kind="resp", **ids)
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
    srv = ThreadingHTTPServer(("127.0.0.1", PORTS[role]), H)
    srv.daemon_threads = True
    sc = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    sc.load_cert_chain(CA, KEY)
    srv.socket = sc.wrap_socket(srv.socket, server_side=True)
    srv.serve_forever()

def post(ctx, dst, path, body):
    raw = json.dumps(body, sort_keys=True).encode()
    ids = dict(path=path, ticket_id=body["ticket_id"], attempt=body["attempt"])
    ctx.gt.emit("msg_send", msg_id=uuid.uuid4().hex, src=ctx.role, dst=dst, body_sha256=sha(raw),
                kind="req", **ids)
    hdr = {"Content-Type": "application/json"}
    if ctx.args.propagate_trace_id:   # ablation only
        hdr["traceparent"] = f"00-{sha(body['ticket_id'])[:32]}-{uuid.uuid4().hex[:16]}-01"
    r = requests.post(f"https://127.0.0.1:{PORTS[dst]}{path}", data=raw, headers=hdr,
                      verify=CA, timeout=900)
    ctx.gt.emit("msg_recv", src=dst, dst=ctx.role, body_sha256=sha(r.content), kind="resp", **ids)
    r.raise_for_status()
    return r.json()

# -------------------------------------------------------------------- summary
def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, max(0, -(-len(xs) * p // 100) - 1))] if xs else None

def summarize(run_dir):
    ev = load_gt(run_dir)
    rows = sorted((e for e in ev if e["event"] == "slo"), key=lambda e: e["order"])
    llm = [e for e in ev if e["event"] == "llm_end" and "error" not in e]
    calls = {}
    for e in llm:
        if e["purpose"] != "fault_burst":
            calls[e["ticket_id"]] = calls.get(e["ticket_id"], 0) + 1
    qw = [e["ollama_queue_ms"] for e in llm if e["purpose"] != "fault_burst"]
    e2e = [r["e2e_ms"] for r in rows]
    keys = ("ticket_id", "order", "label", "result", "attempts", "approved", "correct",
            "e2e_ms", "violated")
    s = {"tickets": len(rows), "e2e_p50_ms": pct(e2e, 50), "e2e_p95_ms": pct(e2e, 95),
         "retries": sum(r["attempts"] - 1 for r in rows), "approved": sum(r["approved"] for r in rows),
         "correct": sum(r["correct"] for r in rows),
         "slo_violations": sum(bool(r["violated"]) for r in rows),
         "llm_calls_per_ticket": sum(calls.values()) / max(1, len(rows)),
         "queue_wait_ms": {"mean": sum(qw) / max(1, len(qw)), "p50": pct(qw, 50),
                           "p95": pct(qw, 95), "max": max(qw, default=None)},
         "rows": [{k: r.get(k) for k in keys} | {"llm_calls": calls.get(r["ticket_id"], 0)}
                  for r in rows]}
    json.dump(s, open(Path(run_dir) / "summary.json", "w"), indent=1)
    return s
