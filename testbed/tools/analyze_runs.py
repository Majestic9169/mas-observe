#!/usr/bin/env python3
"""analyze_runs.py <baseline_run>... <fault_run>   (or --write-slo <baseline_run>...)"""
import json, statistics as stt, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mas import common as C

def load(d):
    ev, meta = C.load_gt(d), json.load(open(Path(d) / "run_meta.json"))
    slo = [e for e in ev if e["event"] == "slo"]
    return ev, meta["t0_mono_ns"], slo

def stats(d, win):
    """Whole-run and in-window metrics. win = (start_s, end_s) relative to t0, or None."""
    ev, t0, slo = load(d)
    out = {"p95_s": C.pct([e["e2e_ms"] for e in slo], 95) / 1000,
           "retries": sum(e["attempts"] - 1 for e in slo)}
    if win:
        lo, hi = t0 + win[0] * 1e9, t0 + win[1] * 1e9
        # tickets whose [dispatch, end] overlaps the window
        w = [e for e in slo if e["mono_ns"] >= lo and e["mono_ns"] - e["e2e_ms"] * 1e6 <= hi]
        q = [e["ollama_queue_ms"] for e in ev if e["event"] == "llm_end" and e["purpose"] != "fault_burst"
             and "error" not in e and lo <= e["mono_ns"] - e["client_ms"] * 1e6 <= hi]
        out |= {"win_n": len(w), "win_p95_s": (C.pct([e["e2e_ms"] for e in w], 95) or 0) / 1000,
                "win_retries": sum(e["attempts"] - 1 for e in w), "win_qwait_ms": sum(q) / max(1, len(q))}
    return out

def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--write-slo" in sys.argv:
        p95 = [C.pct([e["e2e_ms"] for e in load(d)[2]], 95) for d in args]
        slo = {"slo_ms": 1.5 * stt.mean(p95), "baseline_p95_ms": p95, "baseline_runs": args,
               "rule": "1.5 x mean of baseline p95"}
        json.dump(slo, open(C.ROOT / "slo.json", "w"), indent=1)
        print(f"wrote slo.json: SLO_MS={slo['slo_ms']:.0f} from p95s {[round(x) for x in p95]}")
        return
    *bases, fault = args
    ev, t0, _ = load(fault)
    wins = [(e["fault_id"], e["type"], (e["mono_ns"] - t0) / 1e9) for e in ev if e["event"] == "fault_start"]
    ends = {e["fault_id"]: (e["mono_ns"] - t0) / 1e9 for e in ev if e["event"] == "fault_end"}
    for fid, typ, s in wins or [(None, "none", None)]:
        win = (s, ends[fid]) if fid else None
        B, F = [stats(b, win) for b in bases], stats(fault, win)
        print(f"\n== fault {fid} {typ} window={win and [round(x, 1) for x in win]} ==")
        print(f"{'metric':14}{'baseline mean':>14}{'spread(max-min)':>17}{'fault':>10}{'delta/spread':>14}")
        for k in F:
            vals = [b[k] for b in B]
            m, spread = stt.mean(vals), max(vals) - min(vals)
            ratio = (F[k] - m) / spread if spread else float("inf") if F[k] != m else 0.0
            print(f"{k:14}{m:14.2f}{spread:17.2f}{F[k]:10.2f}{ratio:14.1f}")

if __name__ == "__main__":
    main()
