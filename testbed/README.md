# Multi-agent LLM testbed with reproducible fault injection

A minimal 3-agent LLM workload on one Linux machine, with scripted, seeded fault injection and a
complete ground-truth log. An external observability framework (eBPF / TLS-plaintext tracing, in the
spirit of AgentSight) watches it from outside and is graded against that ground truth: can it
recover the causal links between agents, and attribute SLO violations to the injected faults?

## Architecture

```
planner --HTTPS :9101 /task---->  worker --HTTPS :11436--> TLS (socat) --> Ollama :11434
planner --HTTPS :9102 /review-->  critic --HTTPS :11436--> TLS (socat) --> Ollama :11434
planner retries the worker (attempt+1, up to 2 retries) when the critic rejects.
```


| agent | what it is | what it does |
|---|---|---|
| `planner` | client / orchestrator, no server | Dispatches 20 synthetic tickets, 2 in flight at once, so requests overlap and the Ollama queue builds. Retries on critic reject. |
| `worker` | threaded HTTPS server | `agents.py`-style loop with schema-constrained actions `read_file` / `write_file` / `finish`. Reads the ticket and shared `policy.txt`, writes `result_<ticket>_a<attempt>.json`. |
| `critic` | threaded HTTPS server | Deterministic schema check plus one schema-constrained LLM verdict `{"verdict": "approve\|reject", "reason": str}`. |

Each agent is its own OS process. All inter-agent and agent-to-Ollama traffic is real HTTPS over
localhost sockets, so TLS-plaintext tracing can see it. Real Ollama calls only (`qwen2.5:latest`,
temperature 0, `num_ctx 8192`, per-request seed derived from run seed, ticket, attempt, step).

**Ground truth is an app-level side channel.** No ground-truth IDs travel in headers or bodies, so the
observer has to infer links. The optional `--propagate-trace-id` flag adds a W3C `traceparent` header
for ablation only.

## Setup

Requires Python 3, `requests`, `socat`, `openssl`, `taskset`, and Ollama.

```bash
# 1. Ollama (one slot, one model, so requests queue FIFO)
OLLAMA_NUM_PARALLEL=1 OLLAMA_MAX_LOADED_MODELS=1 OLLAMA_KEEP_ALIVE=30m ollama serve

# 2. Preload the model (the runner fails fast if nothing is loaded)
curl -s localhost:11434/api/generate -d '{"model":"qwen2.5:latest","keep_alive":"30m"}'

# 3. HTTPS terminator on :11436 (regenerates certs/ for 365 days if missing or near expiry)
scripts/start_tls.sh
```

## Running

```bash
python run_experiment.py scenarios/baseline.json --seed 1      # -> runs/<run_id>/
python tools/check_ground_truth.py runs/<run_id>               # validate the ground truth
python tools/analyze_runs.py runs/<base1> runs/<base2> runs/<base3> runs/<fault_run>
python tools/analyze_runs.py --write-slo runs/<base1> runs/<base2> runs/<base3>   # writes slo.json
```

The runner warms the model up (excluded from results), sets a shared `t0` (`CLOCK_MONOTONIC`) for all
processes, runs the scenario, and exits non-zero if any process crashed. Ctrl-C and crashes tear down
all agents and hogs; no orphans or bound ports remain.

**Reproducibility:** the same seed gives the same ticket order, fault schedule and per-ticket outputs.
Latencies differ, so don't assert on them.

## Faults

Defined in a scenario JSON, with times relative to `t0`. Every agent reads the scenario and activates
faults by the clock, so there is no control channel. Example:

```json
{
  "name": "burst_worker", 
  "tickets": 20, 
  "concurrency": 2, 
  "faults": [
    {
      "id": "f1", "type": "burst", "target": "worker", "start_s": 15, "duration_s": 40,
      "params": {"k": 3, "num_predict": 200}
    }
  ]
}
```

| group | type | target | mechanism | params |
|---|---|---|---|---|
| LLM contention | `burst` | worker / critic | Before each handled request, fire K extra junk LLM calls that queue in Ollama | `k`, `num_predict` |
| LLM contention | `context_bloat` | worker | Pad every LLM prompt with P tokens of filler | `tokens` |
| Local slowdown | `slow_tool` | worker | `read_file(policy.txt)` sleeps D ms | `delay_ms` |
| Local slowdown | `cpu_hog` (optional) | any agent | Runner spawns H busy loops pinned (`taskset`) to the target's CPU | `hogs` |
| Behavioural | `reject_loop` | critic | Force `reject` for the first R attempts of each ticket | `r` |
| Behavioural | `bad_output` (optional) | worker | Write a wrong category on attempt 0 with probability p | `p` |

Shipped scenarios are in [`scenarios/`](scenarios/), each with one fault active from 15 s to 55 s.

How they show up: contention faults raise queue wait and p95 with retries unchanged, and the cause
(extra Ollama load) can sit in a different agent from the victim. `slow_tool` adds latency with no
queueing signature. Behavioural faults add retries and round trips, and the root cause (critic) is far
from the symptom (worker).

## Ground truth

`runs/<run_id>/ground_truth.jsonl`: one JSON object per line. Every event has
`run_id, role, pid, event, mono_ns, wall_ns`.

| event | fields |
|---|---|
| `proc_start` | `cmdline, ppid` |
| `fault_start` / `fault_end` | `fault_id, type, target, params` |
| `task_start` / `task_end` | `ticket_id, attempt` |
| `msg_send` / `msg_recv` | `msg_id` (sender only), `src, dst, path, body_sha256, ticket_id, attempt, kind` (req/resp). Edges are joined by body hash. |
| `llm_start` / `llm_end` | `purpose` (work / fault_burst / fault_bloat), `step, prompt_sha256, client_ms, queue_wait_ms, ollama_queue_ms`, plus Ollama's timing fields |
| `tool_call` | `tool, abs_path, content_sha256, ok` |
| `slo` | `ticket_id, e2e_ms, attempts, approved, correct, violated` |
| `fault_effect` | `fault_id, kind` (forced rejects) |

Every event caused by an injected fault carries its `fault_id`; that is the attribution ground truth.
Other outputs per run: `summary.json` (per-ticket rows, p50/p95, retries, SLO violations, queue wait
stats) and `run_meta.json` (scenario, seed, env, Ollama version, model digest, kernel, git hash, all pids).

**SLO:** per-ticket end-to-end latency (dispatch to final approve or give-up) must be at most
`SLO_MS` = 1.5 x the mean baseline p95, saved to `slo.json`. A ticket is violated if it exceeds that
or is not approved. Correctness of the label is recorded separately.

## Measured numbers (seed 1, 20 tickets, concurrency 2)

Baseline (3 runs): about 90 s per run, 5.5 LLM calls per ticket, p50 7.1-7.4 s, p95 8.7-9.1 s,
2 retries, `SLO_MS` = 13,333 ms.

| scenario | p95 | retries | note |
|---|---|---|---|
| baseline | 8.9 s | 2 | spread about 0.4 s |
| `burst` on worker | 33.9 s | 2 | in-window queue wait 5.3 s |
| `burst` on critic | 34.8 s | 2 | |
| `context_bloat` (4000 tokens) | 22.4 s | 2 | |
| `reject_loop` (r=1) | 15.7 s | 7 | |
| `slow_tool` (8000 ms) | 14.8 s | 2 | |
| `bad_output` (p=0.5) | 15.3 s | 5 | |
| `cpu_hog` (4 hogs) | 9.0 s | 2 | no effect: agents are I/O-bound and the model runs on the GPU |

**Queue wait caveat:** in Ollama 0.40.11, `total_duration` already includes scheduler queueing, so the
spec's `queue_wait_ms = client_ms - total_duration` only measures client and TLS overhead (about 5 ms).
Use `ollama_queue_ms` (total minus load, prompt-eval and eval time) for the real wait. On this data
Ollama served strictly FIFO: 0 service-order inversions across 100 overlapping request pairs.

## Layout

```
mas/{common,worker,critic,planner,faults}.py   run_experiment.py
scenarios/*.json   scripts/start_tls.sh   tools/{check_ground_truth,analyze_runs}.py
certs/   runs/ (gitignored)   agents.py (original single-agent worker, reused patterns)
```

Files not used by the testbed: `bridge.py gen_synth.py score.py proxy.py harness_test_*.py
opencode.json Modelfile.qwen25 agentsight-*.db* snap.json output/ logs/ sandbox/`.

---

# History: earlier experiments

## Recording with AgentSight

AgentSight hooks SSL reads/writes, which is why an HTTPS forwarder sits in front of Ollama (plain HTTP).

Build fixes needed on this distro:
- It linked against a hardcoded `readline` symbol in `/usr/bin/bash`; this distro loads it dynamically from `/usr/lib/libreadline`, so AgentSight couldn't find it.
- It crossed the 512-byte stack frame limit with this clang version; fixed (an upstream issue was raised).

```bash
sudo mount -t tracefs nodev /sys/kernel/tracing          # tracefs must be mounted

# terminal 1 (sudo for eBPF; HOME=$HOME so it writes to the normal home, not root's)
sudo env "HOME=$HOME" ./agentsight record -c python

# terminal 2: the single-agent baseline (5 ticket types, 20/20 correct in testing)
OLLAMA_URL=https://127.0.0.1:11436 OLLAMA_CA=certs/cert.pem python agents.py --runs 20 --cycle

# results
./agentsight report prompts          # or --json
./agentsight report audit --json
```

## Earlier harness tests

- **Test 3:** the same task through `opencode` against `qwen2.5-16k` (`ollama create qwen2.5-16k -f Modelfile.qwen25`, config in `opencode.json`). It failed (success rate below 5%) because opencode's system prompt is larger than the context window, which `proxy.py` showed by logging to `logs/wire.jsonl`.
- **Test 2:** `harness_test_2.py`, schema following in a more realistic setting (`sandbox/ticket_1.txt`).
- **Test 1:** `harness_test_1.py`, basic schema adherence as a precursor to tool use. qwen2.5 and nemotron-3-nano both pass.
