import json, os, time, hashlib, requests
from pathlib import Path

OLLAMA = "http://127.0.0.1:11434/api/chat"
MODEL = "qwen2.5-16k"
THINK = None            
AGENT = "worker-1"
SANDBOX = Path("sandbox").resolve()
LOG = Path("logs/agent.jsonl")
MAX_STEPS = 8

def fileaction(name, extra):
    return {"type": "object",
            "properties": {"action": {"const": name},
                           "args": {"type": "object",
                                    "properties": extra,
                                    "required": list(extra),
                                    "additionalProperties": False}},
            "required": ["action", "args"]}

SCHEMA = {"oneOf": [
    fileaction("read_file", {"filename": {"type": "string"}}),
    fileaction("write_file", {"filename": {"type": "string"},
                              "content": {"type": "string"}}),
    fileaction("finish", {"summary": {"type": "string"}}),
]}

SYSTEM = """You are a support-ticket triage agent.
Tools: read_file(filename), write_file(filename, content), finish(summary).
Task: read ticket_1.txt, classify it, then write result_1.json containing
JSON like {"category": "billing|bug|feature_request|other", "urgency": "low|high"}.
Then call finish. Respond ONLY with one JSON action per turn."""

def log(event, **kw):
    rec = {"event": event, "agent": AGENT, "pid": os.getpid(),
           "wall_ns": time.time_ns(),
           "mono_ns": time.clock_gettime_ns(time.CLOCK_MONOTONIC), **kw}
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")

def llm_call(messages, step):
    payload = {"model": MODEL, "stream": False, "format": SCHEMA,
               "keep_alive": "30m",
               "options": {"temperature": 0, "num_ctx": 8192,
                           "num_predict": 512, "seed": 1},
               "messages": messages}
    if THINK is not None:
        payload["think"] = THINK
    prompt_hash = hashlib.sha256(json.dumps(messages).encode()).hexdigest()[:12]
    log("llm_start", step=step, prompt_hash=prompt_hash)
    t0 = time.monotonic_ns()
    r = requests.post(OLLAMA, json=payload, timeout=300)
    r.raise_for_status()
    d = r.json()
    wall_ms = (time.monotonic_ns() - t0) / 1e6
    log("llm_end", step=step, prompt_hash=prompt_hash, client_ms=wall_ms,
        **{k: d.get(k) for k in ("total_duration", "load_duration",
           "prompt_eval_count", "prompt_eval_duration", "eval_count",
           "eval_duration", "done_reason")})
    return d["message"]["content"]

def safe_path(name):
    p = (SANDBOX / name).resolve()
    p.relative_to(SANDBOX)         
    return p

def run_tool(action, args):
    try:
        if action == "read_file":
            return safe_path(args["filename"]).read_text()
        if action == "write_file":
            safe_path(args["filename"]).write_text(args["content"])
            return "ok"
    except Exception as e:
        return f"ERROR: {e}"

def main():
    msgs = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": "Begin."}]
    log("task_start")
    for step in range(MAX_STEPS):
        raw = llm_call(msgs, step)
        act = json.loads(raw)
        action, args = act["action"], act["args"]
        log("decision", step=step, action=action, args=args)
        msgs.append({"role": "assistant", "content": raw})
        if action == "finish" and not (SANDBOX / "result_1.json").exists():
            log("finish_rejected", step=step)
            msgs.append({"role": "user",
                        "content": "ERROR: result_1.json has not been written yet. Write it before finishing."})
            continue
        if action == "finish":
            break;
        result = run_tool(action, args)
        log("tool_result", step=step, action=action, result=str(result)[:200])
        msgs.append({"role": "user", "content": f"TOOL RESULT: {result}"})
    out = SANDBOX / "result_1.json"
    ok = out.exists()
    log("task_end", steps=step + 1, output_exists=ok,
        output=out.read_text() if ok else None)

if __name__ == "__main__":
    main()
