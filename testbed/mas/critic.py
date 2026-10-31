"""Critic: threaded HTTPS server; deterministic schema check + one schema-constrained LLM verdict."""
from . import common as C, faults as F

SCHEMA = {"type": "object", "properties": {"verdict": {"enum": ["approve", "reject"]},
                                           "reason": {"type": "string"}},
          "required": ["verdict", "reason"]}
SYSTEM = """You are a QA reviewer for support-ticket triage. Given a ticket and a proposed JSON answer, decide.
category: billing (charges, invoices, refunds, pricing), bug (something is broken or crashing),
feature_request (asks for new functionality), other (anything else, e.g. questions).
urgency: high only if the customer says it is urgent, blocking, ASAP or needs it today; else low.
Approve if both fields are right for the ticket, otherwise reject with a short reason.
Reply with JSON {"verdict": "approve" or "reject", "reason": "<short>"}."""

def handle(ctx, path, body):
    tid, att, res = body["ticket_id"], body["attempt"], body.get("result")
    ctx.gt.emit("task_start", ticket_id=tid, attempt=att)
    burst = F.burst(ctx, tid, att)
    ok = (isinstance(res, dict) and res.get("category") in C.CATEGORIES
          and res.get("urgency") in C.URGENCIES)
    if not ok:
        verdict, reason = "reject", "schema check failed"
    else:
        raw = C.llm_call(ctx.gt, [{"role": "system", "content": SYSTEM},
                                  {"role": "user", "content": f"Ticket: {body['text']}\nAnswer: {res}"}],
                         purpose="work", step=0, fmt=SCHEMA, num_predict=128, ticket_id=tid, attempt=att,
                         seed=C.derive_seed(ctx.seed, "critic", tid, att))
        try:
            o = __import__("json").loads(raw)
            verdict, reason = o["verdict"], o["reason"]
        except Exception:
            verdict, reason = "reject", "unparseable verdict"
    f = ctx.faults.active("reject_loop")
    if f and att < f["params"]["r"]:
        verdict, reason = "reject", "forced"
        ctx.gt.emit("fault_effect", fault_id=f["id"], kind="forced_reject", ticket_id=tid, attempt=att)
    for t in burst:
        t.join()
    ctx.gt.emit("task_end", ticket_id=tid, attempt=att)
    return {"ticket_id": tid, "attempt": att, "verdict": verdict, "reason": reason}

if __name__ == "__main__":
    ctx = C.init("critic")
    C.tls_server(ctx, lambda path, body: handle(ctx, path, body))
