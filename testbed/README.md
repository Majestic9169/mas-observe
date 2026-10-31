# Setting up the Testbed for Agentic Workflow

```bash
OLLAMA_MAX_LOADED_MODELS=1 OLLAMA_NUM_PARALLEL=1 OLLAMA_KEEP_ALIVE=30m OLLAMA_HOST=0.0.0.0:11434 ollama serve
```

So far
- we have one agent that can work
- we need 3 agents doing different things communicating with each other via some tool/network
- we need to be able to inject faults and slo violations artificially

## Test 4

Create `agents.py` that runs a single agent ticket classifier worker, we now start linking with agentsight

### SSL forwarding

```bash
mkdir -p certs && cd certs
openssl req -x509 -newkey rsa:2048 -nodes -days 7 -keyout key.pem -out cert.pem \
      -subj "/CN=localhost" -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"
cat cert.pem key.pem > both.pem
cd ..
socat OPENSSL-LISTEN:11436,cert=certs/both.pem,verify=0,fork,reuseaddr TCP:127.0.0.1:11434
```

agentsight hooks to SSL reads/writes so we add an https forwarder between OLLAMA (plain http) and the python agent

### Agentsight build

fixed 2 major build issues in agentsight
- linked against hardcoded readline symbol in /usr/bin/bash (my distro dynamically gets from /usr/lib/libreadline so agentsight couldn't find it)
- crossed 512-byte stack frame size with my version of clang, so we fixed that, i raised an issue on the repo too easy enough fix

### Recording with agentsight

You need tracefs mounted

```bash
sudo mount -t tracefs nodev /sys/kernel/tracing
```

in one terminal

```bash
sudo env "HOME=$HOME" ./agentsight record -c python
# Trace Monitoring
# ============================================================
# ============================================================
# Starting flexible trace monitoring with 3 runners and 1 global analyzers...
# Press Ctrl+C to stop
# Starting web server on http://127.0.0.1:7395/
# [2026-10-28T17:33:05Z WARN  agentsight_capture_core::runners::common] [Process] STDERR: libbpf: elf: failed to find symbol 'readline' in '/usr/bin/bash'
# ^C
# 
# Received shutdown signal, shutting down...
# [HTTPFilter Global Metrics] No metrics available
# [SSLFilter Global Metrics] No metrics available
# ✓ Shutdown requested. Stopping monitoring.
# 
# Recorded 2m to /home/user/git-repos/btp/testbed/agentsight-20261028-230304.db
# 63 API calls · 0 tokens · 2 execs · 11 files · 1 network endpoints
# Run: agentsight report
```

sudo needed for ebpf, "HOME=??" needed because it writes to logs and stuff in your normal home and not root wala home

in another terminal we run our agent

```bash
OLLAMA_URL=https://127.0.0.1:11436 OLLAMA_CA=certs/cert.pem python agents.py --runs 20 --cycle
# run 0: ticket=1 valid=True correct=True steps=3 rejected=0 7.9s
# run 1: ticket=2 valid=True correct=True steps=3 rejected=0 3.0s
# run 2: ticket=3 valid=True correct=True steps=3 rejected=0 2.9s
# run 3: ticket=4 valid=True correct=True steps=3 rejected=0 3.0s
# run 4: ticket=5 valid=True correct=True steps=3 rejected=0 3.1s
# run 5: ticket=1 valid=True correct=True steps=3 rejected=0 2.9s
# run 6: ticket=2 valid=True correct=True steps=3 rejected=0 2.9s
# run 7: ticket=3 valid=True correct=True steps=3 rejected=0 3.0s
# run 8: ticket=4 valid=True correct=True steps=3 rejected=0 3.0s
# run 9: ticket=5 valid=True correct=True steps=3 rejected=0 3.0s
# run 10: ticket=1 valid=True correct=True steps=3 rejected=0 3.0s
# run 11: ticket=2 valid=True correct=True steps=3 rejected=0 3.0s
# run 12: ticket=3 valid=True correct=True steps=3 rejected=0 3.0s
# run 13: ticket=4 valid=True correct=True steps=3 rejected=0 3.1s
# run 14: ticket=5 valid=True correct=True steps=3 rejected=0 3.0s
# run 15: ticket=1 valid=True correct=True steps=3 rejected=0 3.0s
# run 16: ticket=2 valid=True correct=True steps=3 rejected=0 3.0s
# run 17: ticket=3 valid=True correct=True steps=3 rejected=0 3.0s
# run 18: ticket=4 valid=True correct=True steps=3 rejected=0 3.1s
# run 19: ticket=5 valid=True correct=True steps=3 rejected=0 3.1s
# valid 20/20, correct category 20/20
```

tested against 5 types of tickets that it must classify

view results with 

```bash
./agentsight report prompts 
./agentsight report prompts --json
./agentsight report audit --json
```

## Test 3

Register `qwen2.5-16k` with [./Modelfile.qwen25](./Modelfile.qwen25)

```bash
ollama create qwen2.5-16k -f Modelfile.qwen25
ollama show qwen2.5-16k --modelfile | grep num_ctx
```

Create opencode.json

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "ollama": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Ollama (local)",
      "options": { "baseURL": "http://localhost:11434/v1" },
      "models": { "qwen2.5-16k": { "name": "Qwen2.5 16k" } }
    }
  }
}
```

Run same task via opencode

```bash
opencode run -m ollama/qwen2.5-16k "Read sandbox/ticket_1.txt, classify it, and write sandbox/result_1.json with category and urgency"
```

Fail beautifully because opencode's system prompt is way larger than the context window

This we know because we added `proxy.py` to see what it tells the agent, which writes [`./logs/wire.jsonl`](./logs/wire.jsonl)

success rate of < 5%

## Test 2

slightly more complex schema following in realistic setting

```bash
mkdir -p logs/ sandbox/
echo "Hi, I was charged twice for my subscription this month and nobody is answering my emails. Please fix this today." > sandbox/ticket_1.txt
python ./harness_test_2.py
```

## Test 1

simple schema following, can the local agent adhere to a schema, basic precursor to using tools

```bash
python ./harness_test_1.py
```

qwen2.5 and nemotron-3-nano both pass

