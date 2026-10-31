"""Worker: threaded HTTPS server; agents.py-style schema-constrained tool loop over Ollama."""
import json, random, time
from pathlib import Path
from . import common as C, faults as F

MAX_STEPS = 8

def action_schema(name, props):
    return {"type": "object",
            "properties": {"action": {"const": name},
                           "args": {"type": "object", "properties": props, "required": list(props),
                                    "additionalProperties": False}},
            "required": ["action", "args"]}
SCHEMA = {"oneOf": [action_schema("read_file", {"filename": {"type": "string"}}),
                    action_schema("write_file", {"filename": {"type": "string"},
                                                 "content": {"type": "string"}}),
                    action_schema("finish", {"summary": {"type": "string"}})]}
SYSTEM = """You are a support-ticket triage agent. Files live in your sandbox; use bare filenames.
Tools:
- read_file(filename)
- write_file(filename, content)
- finish(summary)
Task: read TICKET, then read policy.txt, classify the ticket following the policy, write OUT, then call finish.
OUT must contain exactly a JSON object like {"category": "billing", "urgency": "low"}.
category is one of: CATS. urgency is one of: URGS.
Reply with exactly one action per turn. Do not call finish before OUT is written."""

def validate(text):
    try:
        o = json.loads(text)
    except Exception as e:
        return False, f"not valid JSON ({e})", None
    if not isinstance(o, dict) or o.get("category") not in C.CATEGORIES:
        return False, f"category must be one of {C.CATEGORIES}", None
    if o.get("urgency") not in C.URGENCIES:
        return False, f"urgency must be one of {C.URGENCIES}", None
    return True, "", o

def run_tool(ctx, action, a, out, tid, att):
    ev = dict(tool=action, ticket_id=tid, attempt=att)
    try:
        p = (ctx.sandbox / a["filename"].removeprefix("sandbox/")).resolve()
        p.relative_to(ctx.sandbox)
        if action == "read_file":
            if p.name == "policy.txt" and (f := ctx.faults.active("slow_tool")):
                time.sleep(f["params"]["delay_ms"] / 1000)
                ev.update(fault_id=f["id"], delay_ms=f["params"]["delay_ms"])
            text = p.read_text()
            ctx.gt.emit("tool_call", abs_path=str(p), content_sha256=C.sha(text), ok=True, **ev)
            return text[:4000]
        content = a["content"]
        if p.name == out:
            ok, err, obj = validate(content)
            if not ok:
                ctx.gt.emit("tool_call", abs_path=str(p), content_sha256=C.sha(content), ok=False, **ev)
                return f"ERROR: {err}. File not written; fix it and write again."
            f = ctx.faults.active("bad_output")
            if f and att == 0 and random.Random(C.derive_seed(ctx.seed, "bad", tid)).random() < f["params"]["p"]:
                obj["category"] = random.Random(C.derive_seed(ctx.seed, "badcat", tid)).choice(
                    [c for c in C.CATEGORIES if c != obj["category"]])
                content = json.dumps(obj)
                ev["fault_id"] = f["id"]
        p.write_text(content)
        ctx.gt.emit("tool_call", abs_path=str(p), content_sha256=C.sha(content), ok=True, **ev)
        return "ok"
    except Exception as e:
        ctx.gt.emit("tool_call", abs_path=a.get("filename"), content_sha256=None, ok=False, **ev)
        return f"ERROR: {e}"

def handle(ctx, path, body):
    tid, att = body["ticket_id"], body["attempt"]
    ctx.gt.emit("task_start", ticket_id=tid, attempt=att)
    burst = F.burst(ctx, tid, att)
    ticket, out = f"ticket_{tid}.txt", f"result_{tid}_a{att}.json"
    system = (SYSTEM.replace("TICKET", ticket).replace("OUT", out)
              .replace("CATS", ", ".join(C.CATEGORIES)).replace("URGS", ", ".join(C.URGENCIES)))
    begin = "Begin." + (f" A reviewer rejected your previous answer: {body['feedback']}" if body.get("feedback") else "")
    hist, steps = [{"role": "user", "content": begin}], 0
    for step in range(MAX_STEPS):
        steps = step + 1
        bloat = ctx.faults.active("context_bloat")
        sysmsg = system + (("\n\nReference notes (ignore):\n" + F.filler(
            bloat["params"]["tokens"], ctx.seed, tid, att)) if bloat else "")
        raw = C.llm_call(ctx.gt, [{"role": "system", "content": sysmsg}] + hist,
                         purpose="fault_bloat" if bloat else "work", step=step, fmt=SCHEMA,
                         seed=C.derive_seed(ctx.seed, tid, att, step), ticket_id=tid, attempt=att,
                         fault_id=bloat["id"] if bloat else None)
        try:
            act = json.loads(raw)
            action, a = act["action"], act["args"]
        except Exception:
            break
        hist.append({"role": "assistant", "content": raw})
        if action == "finish":
            p = ctx.sandbox / out
            if p.exists() and validate(p.read_text())[0]:
                break
            hist.append({"role": "user", "content": f"ERROR: {out} is missing or invalid. Write it before finishing."})
            continue
        hist.append({"role": "user", "content": f"TOOL RESULT: {run_tool(ctx, action, a, out, tid, att)}"})
    for t in burst:
        t.join()
    p = ctx.sandbox / out
    result = validate(p.read_text())[2] if p.exists() else None
    ctx.gt.emit("task_end", ticket_id=tid, attempt=att)
    return {"ticket_id": tid, "attempt": att, "result": result, "steps": steps}

if __name__ == "__main__":
    ctx = C.init("worker")
    C.tls_server(ctx, lambda path, body: handle(ctx, path, body))
