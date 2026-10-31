"""Planner: client/orchestrator (no server). Dispatches tickets, retries worker on critic reject."""
import json, time
from concurrent.futures import ThreadPoolExecutor
from . import common as C

def main():
    def extra(ap):
        ap.add_argument("--tickets", type=int, default=20)
        ap.add_argument("--concurrency", type=int, default=2)
    ctx = C.init("planner", extra)
    tickets = C.make_tickets(ctx.seed, ctx.args.tickets)
    try:
        slo_ms = json.load(open(C.ROOT / "slo.json"))["slo_ms"]
    except FileNotFoundError:
        slo_ms = None
    time.sleep(max(0, (ctx.t0 - C.mono()) / 1e9))

    def run_ticket(i, t):
        tid, d0, feedback, w, approved = t["id"], C.mono(), None, {"result": None}, False
        for att in range(C.MAX_ATTEMPTS):
            ctx.gt.emit("task_start", ticket_id=tid, attempt=att)
            w = C.post(ctx, "worker", "/task", {"ticket_id": tid, "attempt": att, "feedback": feedback})
            c = C.post(ctx, "critic", "/review", {"ticket_id": tid, "attempt": att, "text": t["text"],
                                                  "result": w["result"]})
            ctx.gt.emit("task_end", ticket_id=tid, attempt=att)
            if c["verdict"] == "approve":
                approved = True
                break
            feedback = c["reason"]
        e2e = (C.mono() - d0) / 1e6
        correct = bool(w["result"] and w["result"]["category"] == t["label"]["category"])
        ctx.gt.emit("slo", ticket_id=tid, e2e_ms=e2e, attempts=att + 1, approved=approved,
                    correct=correct, violated=None if slo_ms is None else (e2e > slo_ms or not approved),
                    order=i, label=t["label"], result=w["result"])

    with ThreadPoolExecutor(ctx.args.concurrency) as ex:
        for f in [ex.submit(run_ticket, i, t) for i, t in enumerate(tickets)]:
            f.result()          # re-raise any failure -> non-zero exit

if __name__ == "__main__":
    main()
