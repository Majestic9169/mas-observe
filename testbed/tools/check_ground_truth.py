#!/usr/bin/env python3
"""Validate runs/<id>/ground_truth.jsonl. Exit 1 on any failure."""
import json, sys
from collections import Counter
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mas import common as C

def check(run_dir):
    run_dir, errs = Path(run_dir), []
    ev = C.load_gt(run_dir)
    meta = json.load(open(run_dir / "run_meta.json"))
    t0 = meta["t0_mono_ns"]
    ends = [e["mono_ns"] for e in ev if e["event"] == "run_end"]
    if not ends:
        return ["no run_end event"]
    # no event outside [t0, end]  (process start-up / warm-up happen before t0 by design)
    for e in ev:
        if e["event"] not in ("proc_start", "warmup", "run_start", "tool_call") or (
                e["event"] == "tool_call" and e["role"] != "runner"):
            if not t0 <= e["mono_ns"] <= ends[0]:
                errs.append(f"event outside [t0,end]: {e['event']} {e['role']} {(e['mono_ns']-t0)/1e9:.2f}s")
    # every task has start/end (per role, ticket, attempt)
    st, en = (Counter((e["role"], e["ticket_id"], e["attempt"]) for e in ev if e["event"] == k)
              for k in ("task_start", "task_end"))
    errs += [f"task start/end mismatch {k}: {st[k]} vs {en[k]}" for k in st | en if st[k] != en[k]]
    # every send has a matching recv by hash (and vice versa)
    sends = Counter((e["body_sha256"], e["src"], e["dst"], e["path"]) for e in ev if e["event"] == "msg_send")
    recvs = Counter((e["body_sha256"], e["src"], e["dst"], e["path"]) for e in ev if e["event"] == "msg_recv")
    errs += [f"send without recv {k}" for k in sends - recvs] + [f"recv without send {k}" for k in recvs - sends]
    # llm_start/llm_end pair up
    ls = Counter((e["prompt_sha256"], e["pid"]) for e in ev if e["event"] == "llm_start")
    le = Counter((e["prompt_sha256"], e["pid"]) for e in ev if e["event"] == "llm_end")
    errs += [f"llm start/end mismatch {k}" for k in ls | le if ls[k] != le[k]]
    # every file in the sandbox has a tool_call write with the same final content hash
    wrote = {(e["abs_path"], e["content_sha256"]) for e in ev
             if e["event"] == "tool_call" and e["tool"] == "write_file" and e["ok"]}
    for p in (run_dir / "sandbox").iterdir():
        if (str(p.resolve()), C.sha(p.read_bytes())) not in wrote:
            errs.append(f"file without matching write tool_call: {p.name}")
    # every fault window has start and end
    fs = Counter(e["fault_id"] for e in ev if e["event"] == "fault_start")
    fe = Counter(e["fault_id"] for e in ev if e["event"] == "fault_end")
    errs += [f"fault {k}: {fs[k]} starts, {fe[k]} ends" for k in fs | fe if fs[k] != 1 or fe[k] != 1]
    # attribution: events tagged with a fault_id must refer to a declared fault
    ids = {f["id"] for f in meta["scenario"]["faults"]}
    errs += [f"unknown fault_id {e['fault_id']}" for e in ev if e.get("fault_id") and e["fault_id"] not in ids]
    return errs

if __name__ == "__main__":
    bad = 0
    for d in sys.argv[1:]:
        errs = check(d)
        print(f"{'FAIL' if errs else 'ok  '} {d}")
        for m in errs[:20]:
            print("   ", m)
        bad += bool(errs)
    sys.exit(1 if bad else 0)
