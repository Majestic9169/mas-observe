"""Time-windowed fault activation, read from the scenario file by every agent (no control channel)."""
import random, threading
from . import common as C

class Faults:
    def __init__(self, scenario, t0, role):
        self.t0, self.mine = t0, [f for f in scenario.get("faults", []) if f["target"] == role]
    def active(self, typ):
        now = C.mono()
        for f in self.mine:
            s = self.t0 + f["start_s"] * 10**9
            if f["type"] == typ and s <= now < s + f["duration_s"] * 10**9:
                return f

WORDS = ("the of and to a in is it you that he was for on are with as his they be at one have this "
         "from or had by hot but some what there we can out other were all your when up use word "
         "how said an each she which do their time if will way about many then them write would "
         "like so these her long make thing see him two has look more day could go come did").split()

def filler(tokens, *key):
    rng = random.Random(C.derive_seed(*key))
    return " ".join(rng.choice(WORDS) for _ in range(tokens))   # ~1 token per word

def burst(ctx, ticket_id, attempt):
    """Fire K junk LLM calls concurrently with the request; caller joins the returned threads."""
    f = ctx.faults.active("burst")
    if not f:
        return []
    def junk(j):
        prompt = f"Write a long, detailed story (#{j}, for {ticket_id}/{attempt}) about a lighthouse keeper."
        C.llm_call(ctx.gt, [{"role": "user", "content": prompt}], purpose="fault_burst", step=j,
                   seed=C.derive_seed(ctx.seed, "burst", ticket_id, attempt, j),
                   num_predict=f["params"]["num_predict"], fault_id=f["id"],
                   ticket_id=ticket_id, attempt=attempt)
    ths = [threading.Thread(target=junk, args=(j,)) for j in range(f["params"]["k"])]
    for t in ths:
        t.start()
    return ths
