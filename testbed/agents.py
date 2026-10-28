#!/usr/bin/env python3
"""Single-agent ticket-triage worker: schema-constrained actions over Ollama."""
import argparse, hashlib, json, os, sys, time
from pathlib import Path
import requests

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
CA = os.environ.get("OLLAMA_CA") or True       # path to cert.pem when using TLS
MODEL = os.environ.get("MODEL", "qwen2.5:latest")
THINK = {"0": False, "1": True}.get(os.environ.get("THINK"))  # only for thinking models
AGENT = os.environ.get("AGENT_NAME", "worker-1")
SANDBOX = Path(os.environ.get("SANDBOX", "sandbox")).resolve()
LOG = Path(os.environ.get("AGENT_LOG", "logs/agent.jsonl"))
MAX_STEPS = 8
CATEGORIES = ["billing", "bug", "feature_request", "other"]
URGENCIES = ["low", "high"]

# id: (text, expected_category). Only category is scored as ground truth.
TICKETS = {
    1: ("Hi, I was charged twice for my subscription this month and nobody is "
        "answering my emails. Please fix this today.", "billing"),
    2: ("The export button crashes the app every time I click it in Firefox. "
        "The console shows a null pointer error.", "bug"),
    3: ("It would be great if you could add a dark mode option to the "
        "settings page.", "feature_request"),
    4: ("Just wondering what your office hours are for phone support.", "other"),
    5: ("My invoice shows the wrong company name. No rush, fix it whenever "
        "you can.", "billing"),
}

def action_schema(name, props):
    return {"type": "object",
            "properties": {"action": {"const": name},
                           "args": {"type": "object", "properties": props,
                                    "required": list(props),
                                    "additionalProperties": False}},
            "required": ["action", "args"]}

SCHEMA = {"oneOf": [
    action_schema("read_file", {"filename": {"type": "string"}}),
    action_schema("write_file", {"filename": {"type": "string"},
                                 "content": {"type": "string"}}),
    action_schema("finish", {"summary": {"type": "string"}}),
]}

SYSTEM = """You are a support-ticket triage agent. Files live in your sandbox; use bare filenames.
Tools:
- read_file(filename)
- write_file(filename, content)
- finish(summary)
Task: read TICKET, classify it, write OUT, then call finish.
OUT must contain exactly a JSON object like {"category": "billing", "urgency": "low"}.
category is one of: CATS. urgency is one of: URGS.
Reply with exactly one action per turn. Do not call finish before OUT is written."""

RUN = {"id": None}

def log(event, **kw):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {"event": event, "agent": AGENT, "run": RUN["id"], "pid": os.getpid(),
           "wall_ns": time.time_ns(),
           "mono_ns": time.clock_gettime_ns(time.CLOCK_MONOTONIC), **kw}
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")

def llm_call(messages, step, seed):
    payload = {"model": MODEL, "stream": False, "format": SCHEMA, "keep_alive": "30m",
               "options": {"temperature": 0, "num_ctx": 8192, "num_predict": 256,
                           "seed": seed},
               "messages": messages}
    if THINK is not None:
        payload["think"] = THINK
    phash = hashlib.sha256(json.dumps(messages).encode()).hexdigest()[:12]
    log("llm_start", step=step, prompt_hash=phash)
    t0 = time.monotonic_ns()
    r = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=300, verify=CA)
    r.raise_for_status()
    d = r.json()
    log("llm_end", step=step, prompt_hash=phash,
        client_ms=(time.monotonic_ns() - t0) / 1e6,
        **{k: d.get(k) for k in ("total_duration", "load_duration",
           "prompt_eval_count", "prompt_eval_duration", "eval_count",
           "eval_duration", "done_reason")})
    return d["message"]["content"]

def validate(text):
    try:
        obj = json.loads(text)
    except Exception as e:
        return False, f"not valid JSON ({e})", None
    if not isinstance(obj, dict):
        return False, "must be a JSON object", None
    if obj.get("category") not in CATEGORIES:
        return False, f"category must be one of {CATEGORIES}", obj
    if obj.get("urgency") not in URGENCIES:
        return False, f"urgency must be one of {URGENCIES}", obj
    return True, "", obj

def safe_path(name):
    name = name.removeprefix("sandbox/")
    p = (SANDBOX / name).resolve()
    p.relative_to(SANDBOX)  # raises if the path escapes the sandbox
    return p

def run_tool(action, a, out_name):
    try:
        if action == "read_file":
            return safe_path(a["filename"]).read_text()[:4000]
        if action == "write_file":
            p = safe_path(a["filename"])
            if p.name == out_name:
                ok, err, _ = validate(a["content"])
                if not ok:
                    return f"ERROR: {err}. File not written; fix it and write again."
            p.write_text(a["content"])
            return "ok"
    except Exception as e:
        return f"ERROR: {e}"

def run_once(n, run_id, guard, seed):
    RUN["id"] = run_id
    ticket, out = f"ticket_{n}.txt", f"result_{n}.json"
    SANDBOX.mkdir(parents=True, exist_ok=True)
    (SANDBOX / ticket).write_text(TICKETS[n][0] + "\n")
    (SANDBOX / out).unlink(missing_ok=True)
    system = (SYSTEM.replace("TICKET", ticket).replace("OUT", out)
              .replace("CATS", ", ".join(CATEGORIES)).replace("URGS", ", ".join(URGENCIES)))
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": "Begin."}]
    t0, rejected, steps = time.time(), 0, 0
    log("task_start", ticket=n)
    for step in range(MAX_STEPS):
        steps = step + 1
        try:
            raw = llm_call(msgs, step, seed)
            act = json.loads(raw)
            action, a = act["action"], act["args"]
        except Exception as e:
            log("llm_error", step=step, error=repr(e))
            print(f"  llm error: {e!r}", file=sys.stderr)
            break
        log("decision", step=step, action=action, args=a)
        msgs.append({"role": "assistant", "content": raw})
        if action == "finish":
            p = SANDBOX / out
            ok = p.exists() and validate(p.read_text())[0]
            if guard and not ok:
                rejected += 1
                log("finish_rejected", step=step)
                msgs.append({"role": "user", "content":
                             f"ERROR: {out} is missing or invalid. Write it before finishing."})
                continue
            break
        result = run_tool(action, a, out)
        log("tool_result", step=step, action=action, result=str(result)[:200])
        msgs.append({"role": "user", "content": f"TOOL RESULT: {result}"})
    p = SANDBOX / out
    ok, _, obj = validate(p.read_text()) if p.exists() else (False, "", None)
    correct = bool(ok and obj["category"] == TICKETS[n][1])
    log("task_end", ticket=n, steps=steps, valid=ok, correct=correct, rejected=rejected)
    return {"valid": ok, "correct": correct, "steps": steps,
            "rejected": rejected, "secs": time.time() - t0}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticket", type=int, default=1, choices=sorted(TICKETS))
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--cycle", action="store_true", help="rotate through tickets 1-5")
    ap.add_argument("--no-guard", action="store_true", help="allow premature finish")
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    results = []
    for i in range(a.runs):
        n = (i % len(TICKETS)) + 1 if a.cycle else a.ticket
        r = run_once(n, f"{int(time.time())}-{i}", not a.no_guard, a.seed)
        results.append(r)
        print(f"run {i}: ticket={n} valid={r['valid']} correct={r['correct']} "
              f"steps={r['steps']} rejected={r['rejected']} {r['secs']:.1f}s")
    v = sum(r["valid"] for r in results)
    c = sum(r["correct"] for r in results)
    print(f"valid {v}/{len(results)}, correct category {c}/{len(results)}")
    sys.exit(0 if v == len(results) else 1)

if __name__ == "__main__":
    main()
