# AGENTS.md: 3-agent LLM testbed with reproducible fault injection

## Goal

Build a minimal multi-agent LLM workload on one Linux machine, with scripted, seeded
fault injection and a complete ground-truth log. A separate observability framework will later watch it from outside via eBPF and be graded against your ground truth. Capture, bridging and analysis code are out of scope.

## Hard rules

- Python 3 stdlib + `requests` only. No agent frameworks.
- Each agent is its own OS process. All inter-agent and agent->Ollama traffic is HTTPS over real localhost sockets (so TLS-plaintext tracing can see it).
- Ground truth is an app-level side channel. Do NOT put ground-truth IDs in headers or bodies sent on the wire (the observer must infer links). Optional flag `--propagate-trace-id` adds a W3C `traceparent` header, for an ablation only.
- Real Ollama calls only; never fake latency for LLM calls.
- Do not modify `agents.py` in major ways; reuse its patterns (schema-constrained JSON actions, Ollama timing fields, `CLOCK_MONOTONIC` + wall timestamps on every event).
- No root needed. Target about 600 lines total or less.

## Environment

- Ollama on 127.0.0.1:11434, started with `OLLAMA_NUM_PARALLEL=1 OLLAMA_MAX_LOADED_MODELS=1
  OLLAMA_KEEP_ALIVE=30m`. Model `qwen2.5:latest`; options `temperature 0`, `num_ctx 8192`,
  capped `num_predict`, per-request seed derived from (run seed, ticket, attempt, step).
- TLS terminator: `socat OPENSSL-LISTEN:11436,cert=certs/both.pem,verify=0,fork,reuseaddr
  TCP:127.0.0.1:11434`. Agents call `https://127.0.0.1:11436` with `verify=certs/cert.pem`.
  Provide `scripts/start_tls.sh`. The current cert was made with `-days 7`: regenerate
  with `-days 365`.
- The runner must fail fast if the TLS port is down or `ollama ps` shows no loaded model,
  and must send one warm-up request (excluded from results) before t0.

## Topology

    planner --POST https://127.0.0.1:9101/task--->  worker --> Ollama
    planner --POST https://127.0.0.1:9102/review-->  critic --> Ollama
    planner retries the worker (attempt+1, max 2) when the critic rejects.

- planner: client/orchestrator, `--concurrency 2` tickets in flight (so requests overlap
  and the Ollama queue builds). No HTTP server.
- worker: threaded HTTPS server. Loop as in `agents.py` with tools `read_file`,
  `write_file`, `finish`. Reads the ticket, reads the shared `policy.txt`, writes
  `result_<ticket>_a<attempt>.json` (`{"category","urgency"}`), finishes.
- critic: threaded HTTPS server. Deterministic schema check plus one schema-constrained
  LLM call returning `{"verdict":"approve|reject","reason":str}`.
- Tickets: 20 synthetic, template-generated from the run seed, each with a known label.
- Per-run sandbox at `runs/<run_id>/sandbox/`; shared `policy.txt` lives there.
- SLO: per-ticket end-to-end latency (dispatch -> final approve or give-up) <= `SLO_MS`
  = 1.5 x baseline p95, computed from >=3 baseline runs and saved to `slo.json`.
  Success = approved. Also record whether the label was actually correct.

## Faults (scenario JSON, times relative to t0)

Runner passes one shared `t0` (CLOCK_MONOTONIC ns) to every process; each agent reads
the scenario file and activates faults by time. No control channel.

| type | target | mechanism | params | required |
|---|---|---|---|---|
| `burst` | worker or critic | before each handled request, issue K extra junk LLM calls | k, num_predict | yes |
| `context_bloat` | worker | pad every LLM prompt with P tokens of filler | tokens | yes |
| `reject_loop` | critic | force `reject` for the first R attempts of each ticket | r | yes |
| `slow_tool` | worker | `read_file(policy.txt)` sleeps D ms (app-level) | delay_ms | yes |
| `cpu_hog` | any agent | runner spawns H busy-loop processes pinned with `taskset` to the target's CPU | hogs | optional |
| `bad_output` | worker | write a wrong category on attempt 0 with probability p | p | optional |

Example: `{"name":"burst_worker","tickets":20,"concurrency":2,"faults":[{"id":"f1",
"type":"burst","target":"worker","start_s":20,"duration_s":30,"params":{"k":3,"num_predict":200}}]}`

## Reproducibility
- `python run_experiment.py scenarios/X.json --seed N` -> `runs/<run_id>/`.
- Same seed => same ticket order, fault schedule and per-ticket outputs. Latencies will
  differ; do not assert on them.
- `run_meta.json`: scenario, seed, env vars, Ollama version, model digest, kernel, git
  hash, and all agent/hog pids.

## Ground truth: `runs/<run_id>/ground_truth.jsonl`
One JSON object per line. Every event has `run_id, role, pid, event, mono_ns, wall_ns`.
- `proc_start {cmdline, ppid}`
- `fault_start` / `fault_end {fault_id, type, target, params}`
- `task_start` / `task_end {ticket_id, attempt}`
- `msg_send` / `msg_recv {msg_id, src, dst, path, body_sha256, ticket_id, attempt}`.
  Sender mints `msg_id`; the receiver logs only `body_sha256`, so edges are joined by hash.
  Bodies contain ticket and attempt, so they are unique.
- `llm_start` / `llm_end {purpose: work|fault_burst|fault_bloat, step, prompt_sha256,
  client_ms, queue_wait_ms = client_ms - total_duration/1e6, plus Ollama's total_duration,
  load_duration, prompt_eval_count, prompt_eval_duration, eval_count, eval_duration,
  done_reason}`
- `tool_call {tool, abs_path, content_sha256, ok}`
- `slo {ticket_id, e2e_ms, attempts, approved, correct, violated}`

Every event caused by an injected fault (extra calls, sleeps, forced rejects, padding)
carries that `fault_id`. This is the attribution ground truth.

`summary.json`: per-ticket rows plus run-level p50/p95, retries, SLO violations.

## Tools to write
- `tools/check_ground_truth.py`: validates a run: every task has start/end; every send
  has a matching recv by hash; every write has a `tool_call`; every fault window has
  start and end; no event outside [t0, end].
- `tools/analyze_runs.py <baselines...> <fault_run>`: prints p95 e2e, retries, mean
  queue_wait inside the fault window vs baseline, plus the baseline run-to-run spread.

## Layout
    mas/{common,worker,critic,planner,faults}.py   run_experiment.py
    scenarios/*.json   scripts/start_tls.sh   tools/*.py   runs/ (gitignored)

## Existing files
- Reuse patterns from `agents.py`; reuse `certs/`.
- Ignore: `bridge.py gen_synth.py score.py proxy.py harness_test_*.py opencode.json
  Modelfile.qwen25 agentsight-*.db* snap.json output/ logs/ sandbox/`.

## Definition of done
1. Baseline run finishes in <5 min, writes all three output files, exits non-zero if any
   process crashed.
2. Same seed twice => identical ticket order and per-ticket outputs.
3. `check_ground_truth.py` passes on every run.
4. Each required fault, vs 3 saved baselines, shows a p95-latency or retry-count change
   in its window clearly larger than the baseline spread. Tune params until it does.
5. No orphan agents, hogs or bound ports after exit (also after Ctrl-C).

## Report back
Deviations from this file; measured baseline numbers (LLM calls per ticket, p50/p95,
queue_wait distribution); and whether Ollama really serves FIFO at `NUM_PARALLEL=1`
(check this from queue_wait vs arrival order).
