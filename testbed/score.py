import json, sys, re
corr = json.load(open(sys.argv[1]))["correlations"]
truth = json.load(open(sys.argv[2]))
tp = fp_sib = fp_own_other = fp_noise = missed = 0
attached = []
for c in corr:
    p = c["llm_interaction"]["pid"]
    refs = c["llm_interaction"]["extracted_references"]
    # ground-truth effect of THIS call = the file the response names, opened by this pid
    want = [t for t in truth if t["pid"] == p and any(t["file"] in r for r in refs)]
    fo = [a for a in c["system_actions"] if a["action_type"] == "FILE_OPEN"]
    attached.append(len(c["system_actions"]))
    got = {a["arguments"][0] if a["arguments"] else None for a in fo}
    hit = any(any(t["file"] in str(g) for g in got) for t in want)
    tp += hit; missed += (not hit)
    for a in fo:
        path = " ".join(map(str, a["arguments"]))
        if want and want[0]["file"] in path: continue
        if a["pid"] == p: fp_own_other += 1          # same agent, different call
        elif a["pid"] == 999: fp_noise += 1          # unrelated process
        else: fp_sib += 1                            # the OTHER agent's file
n = len(corr); tot_fo = tp + fp_sib + fp_own_other + fp_noise
print(f"interactions with correlations : {n}")
print(f"true effect found (recall)      : {tp}/{n}")
print(f"FILE_OPEN attached in total     : {tot_fo}  -> precision {tp}/{tot_fo} = {tp/tot_fo:.0%}")
print(f"  wrong: sibling agent's file   : {fp_sib}")
print(f"  wrong: same agent, other call : {fp_own_other}")
print(f"  wrong: unrelated process      : {fp_noise}")
print(f"system actions attached per call: min {min(attached)} / max {max(attached)} (truth: 1)")
