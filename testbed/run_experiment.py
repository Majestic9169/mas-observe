#!/usr/bin/env python3
"""python run_experiment.py scenarios/X.json --seed N  ->  runs/<run_id>/"""
import argparse, json, os, platform, signal, socket, ssl, subprocess, sys, threading, time
from pathlib import Path
import requests
from mas import common as C

ROOT = C.ROOT
HOG = "import os\np=os.getppid()\nwhile os.getppid()==p: pass"

def sh(*cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10, cwd=ROOT).stdout.strip()
    except Exception:
        return ""

def tls_up(port):
    try:
        cx = ssl.create_default_context(cafile=C.CA)
        with socket.create_connection(("127.0.0.1", port), timeout=2) as s:
            cx.wrap_socket(s, server_hostname="127.0.0.1").close()
        return True
    except Exception:
        return False

def ollama_env():
    pid = sh("pgrep", "-f", "ollama serve").split("\n")[0]
    try:
        raw = open(f"/proc/{pid}/environ").read().split("\0")
        return {k: v for k, v in (x.split("=", 1) for x in raw if "=" in x) if k.startswith("OLLAMA_")}
    except Exception:
        return {k: v for k, v in os.environ.items() if k.startswith("OLLAMA_")}

def kill_group(p):
    for sig, wait in ((signal.SIGTERM, 3), (signal.SIGKILL, 3)):
        try:
            os.killpg(p.pid, sig)
        except ProcessLookupError:
            return
        try:
            p.wait(wait)
            return
        except subprocess.TimeoutExpired:
            pass

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--propagate-trace-id", action="store_true")
    ap.add_argument("--timeout", type=int, default=900)
    a = ap.parse_args()
    scen = json.load(open(a.scenario))
    # ---- fail fast
    if not tls_up(11436):
        sys.exit("TLS port 11436 is down: run scripts/start_tls.sh")
    if len(sh("ollama", "ps").splitlines()) < 2:
        sys.exit(f"no model loaded: curl -s localhost:11434/api/generate -d '{{\"model\":\"{C.MODEL}\",\"keep_alive\":\"30m\"}}'")
    r = requests.post(f"{C.OLLAMA_URL}/api/chat", verify=C.CA, timeout=300, json={
        "model": C.MODEL, "stream": False, "keep_alive": "30m", "options": {"num_predict": 8, "num_ctx": 8192, "temperature": 0},
        "messages": [{"role": "user", "content": "Say ok."}]})
    r.raise_for_status()

    run_id = f"{scen['name']}_s{a.seed}_{time.strftime('%Y%m%d-%H%M%S')}"
    run_dir = ROOT / "runs" / run_id
    sandbox = run_dir / "sandbox"
    sandbox.mkdir(parents=True)
    json.dump(scen, open(run_dir / "scenario.json", "w"), indent=1)
    gt = C.GT(run_dir, "runner")
    gt.emit("warmup", total_duration=r.json().get("total_duration"))   # excluded from results

    # ---- sandbox contents (shared policy + one ticket file per ticket)
    tickets = C.make_tickets(a.seed, scen["tickets"])
    files = {"policy.txt": (
        "Triage policy.\ncategory billing: charges, invoices, refunds, pricing.\n"
        "category bug: something is broken, crashing or erroring.\n"
        "category feature_request: asks for new functionality.\n"
        "category other: anything else (questions, contacts, hours).\n"
        "urgency high: customer says urgent, blocked, ASAP or needs it today. Otherwise low.\n")}
    files |= {f"ticket_{t['id']}.txt": t["text"] + "\n" for t in tickets}
    for n, txt in files.items():
        (sandbox / n).write_text(txt)
        gt.emit("tool_call", tool="write_file", abs_path=str((sandbox / n).resolve()),
                content_sha256=C.sha(txt), ok=True)

    cpus = sorted(os.sched_getaffinity(0))
    cpu = {"worker": cpus[-1], "critic": cpus[-2], "planner": cpus[-3]}
    t0 = C.mono() + 6 * 10**9
    procs, hogs, stop = {}, {}, threading.Event()
    meta = {"scenario": scen, "seed": a.seed, "run_id": run_id, "t0_mono_ns": t0,
            "propagate_trace_id": a.propagate_trace_id, "cpu_pinning": cpu,
            "ollama_env": ollama_env(), "agent_env": {k: v for k, v in os.environ.items() if k in ("MODEL", "OLLAMA_URL")},
            "ollama_version": requests.get("http://127.0.0.1:11434/api/version").json().get("version"),
            "model": C.MODEL, "model_digest": next((m["digest"] for m in requests.get(
                "http://127.0.0.1:11434/api/tags").json()["models"] if m["name"] == C.MODEL), None),
            "kernel": platform.release(), "git_hash": sh("git", "rev-parse", "HEAD"),
            "git_dirty": bool(sh("git", "status", "--porcelain", "--", ".")), "pids": {}}
    def save_meta():
        meta["pids"] = {**{k: p.pid for k, p in procs.items()},
                        "hogs": [h.pid for hs in hogs.values() for h in hs], "runner": os.getpid()}
        json.dump(meta, open(run_dir / "run_meta.json", "w"), indent=1)

    def spawn(role, *extra):
        log = open(run_dir / f"{role}.log", "w")
        p = subprocess.Popen(["taskset", "-c", str(cpu[role]), sys.executable, "-m", f"mas.{role}",
                              "--run-dir", str(run_dir), "--t0", str(t0), "--seed", str(a.seed),
                              *(["--propagate-trace-id"] if a.propagate_trace_id else []), *extra],
                             cwd=ROOT, stdout=log, stderr=log, start_new_session=True)
        procs[role] = p
        save_meta()

    def faults_thread():
        evs = sorted([(f["start_s"], 0, f) for f in scen["faults"]] +
                     [(f["start_s"] + f["duration_s"], 1, f) for f in scen["faults"]],
                     key=lambda e: (e[0], e[1]))
        open_f = {}
        for t, kind, f in evs:
            if stop.wait(max(0, (t0 + t * 10**9 - C.mono()) / 1e9)):
                break
            info = dict(fault_id=f["id"], type=f["type"], target=f["target"], params=f["params"])
            gt.emit("fault_start" if kind == 0 else "fault_end", **info)
            if kind == 0:
                open_f[f["id"]] = f
            else:
                open_f.pop(f["id"], None)
            if f["type"] == "cpu_hog":
                if kind == 0:
                    hogs[f["id"]] = [subprocess.Popen(
                        ["taskset", "-c", str(cpu[f["target"]]), sys.executable, "-c", HOG],
                        start_new_session=True) for _ in range(f["params"]["hogs"])]
                    for h in hogs[f["id"]]:
                        gt.emit("proc_start", cmdline=f"cpu_hog {f['id']}", ppid=os.getpid(), hog_pid=h.pid)
                    save_meta()
                else:
                    for h in hogs.get(f["id"], []):
                        kill_group(h)
        for f in open_f.values():    # run ended inside a window: close it
            gt.emit("fault_end", fault_id=f["id"], type=f["type"], target=f["target"],
                    params=f["params"], truncated=True)
            for h in hogs.get(f["id"], []):
                kill_group(h)

    rc, ft = 1, threading.Thread(target=faults_thread, daemon=True)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    try:
        gt.emit("run_start", t0_mono_ns=t0, run_id=run_id)
        spawn("worker"); spawn("critic")
        while not (tls_up(9101) and tls_up(9102)):
            if any(p.poll() is not None for p in procs.values()) or C.mono() > t0:
                raise RuntimeError("agent servers failed to start; see runs/%s/*.log" % run_id)
            time.sleep(0.2)
        spawn("planner", "--tickets", str(scen["tickets"]), "--concurrency", str(scen["concurrency"]))
        ft.start()
        deadline = time.time() + a.timeout
        while procs["planner"].poll() is None:
            dead = [r for r in ("worker", "critic") if procs[r].poll() is not None]
            if dead or time.time() > deadline:
                raise RuntimeError(f"{dead or 'timeout'}: process crashed/timed out")
            time.sleep(0.5)
        if procs["planner"].returncode:
            raise RuntimeError(f"planner exited {procs['planner'].returncode}")
        stop.set(); ft.join()
        gt.emit("run_end", t_end=C.mono())
        s = C.summarize(run_dir)
        print(f"{run_id}: p50={s['e2e_p50_ms']/1000:.1f}s p95={s['e2e_p95_ms']/1000:.1f}s "
              f"retries={s['retries']} approved={s['approved']}/{s['tickets']} correct={s['correct']} "
              f"llm/ticket={s['llm_calls_per_ticket']:.1f} qwait_mean={s['queue_wait_ms']['mean']:.0f}ms")
        rc = 0
    except (KeyboardInterrupt, SystemExit, Exception) as e:
        print(f"ABORT: {e!r}", file=sys.stderr)
        rc = 1
    finally:
        stop.set()
        for p in [*procs.values(), *[h for hs in hogs.values() for h in hs]]:
            kill_group(p)
        try:
            gt.emit("run_end", t_end=C.mono(), aborted=rc != 0) if rc else None
            save_meta()
        except Exception:
            pass
    sys.exit(rc)

if __name__ == "__main__":
    main()
