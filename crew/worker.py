"""`crew run NAME`: run one worker on one task and narrate everything it does onto the bus.

The adapter (workers/codex.py or workers/claude.py) builds the command line and maps each line
of the agent's JSON stream to bus events. This module does the rest: task files, state, the raw
log, the timeout, Ctrl-C, the final result and the summary the architect reads.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from .bus import Bus
from .config import Config, ConfigError
from .workers import ADAPTERS, Ctx


def worker_dir(cfg: Config, name: str) -> Path:
    return cfg.state / "workers" / name


def load_state(cfg: Config, name: str) -> dict:
    try:
        st = json.loads((worker_dir(cfg, name) / "state.json").read_text())
        if isinstance(st, dict):
            return st
    except Exception:
        pass
    return {"thread_id": None, "tasks": 0}


def save_state(cfg: Config, name: str, st: dict) -> None:
    p = worker_dir(cfg, name) / "state.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=2))
    tmp.replace(p)


def _kill(proc: subprocess.Popen, sig=signal.SIGTERM) -> None:
    try:
        os.killpg(proc.pid, sig)
    except Exception:
        try:
            proc.send_signal(sig)
        except Exception:
            pass


def run(cfg: Config, name: str, task: str, cont: bool = False, timeout: int | None = None,
        out=None) -> int:
    out = out or sys.stdout
    w = cfg.worker(name)
    if not task.strip():
        raise ConfigError("the task is empty")
    adapter = ADAPTERS[w.kind]
    bus = Bus(cfg.root)
    arch = cfg.architect.name
    timeout = timeout or w.timeout

    st = load_state(cfg, name)
    n = int(st.get("tasks", 0)) + 1
    resume_id = None
    if cont:
        if st.get("thread_id") and st.get("kind", w.kind) == w.kind:
            resume_id = st["thread_id"]
        else:
            print(f"crew: {name} has no thread to continue; starting a new one", file=sys.stderr)
    wdir = worker_dir(cfg, name)
    wdir.mkdir(parents=True, exist_ok=True)
    task_path = wdir / f"task-{n:02d}.md"
    result_path = wdir / f"task-{n:02d}.result.md"
    raw_path = wdir / f"task-{n:02d}.raw.jsonl"
    task_path.write_text(task)
    cwd = cfg.worker_cwd(w)

    if int(st.get("tasks", 0)) == 0 and not resume_id:
        bus.post(name, None, "connect", f"{name} online · {adapter.describe(w)}")
    bus.post(arch, name, "task", task, n=n, resume=bool(resume_id), cwd=str(cwd), kind=w.kind, model=w.model or None)
    bus.post(name, None, "status", f"starting {w.kind}…", n=n)

    cmd = adapter.build_command(w, task, resume_id, result_path, cwd)
    if not shutil.which(cmd[0]):
        msg = f"`{cmd[0]}` is not on PATH; install it and sign in"
        bus.post(name, arch, "error", msg)
        bus.post(name, None, "status", "failed (not installed)", n=n)
        print(f"{name}: {msg}", file=out)
        return 127

    ctx = Ctx(name=name, architect=arch, cwd=str(cwd), resume=bool(resume_id), thread_id=resume_id)
    saved_thread = st.get("thread_id")
    st["kind"] = w.kind
    t0 = time.time()
    timed_out = threading.Event()
    env = dict(os.environ, CREW_WORKER="1", CREW_PROJECT=str(cfg.root))

    def on_term(signum, frame):
        raise KeyboardInterrupt

    old_term = signal.signal(signal.SIGTERM, on_term) if threading.current_thread() is threading.main_thread() else None
    rc = None
    with open(raw_path, "w") as raw:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1, errors="replace", cwd=str(cwd), env=env,
                                start_new_session=True)

        def kill_on_timeout() -> None:
            timed_out.set()
            _kill(proc, signal.SIGKILL)

        timer = threading.Timer(timeout, kill_on_timeout)
        timer.daemon = True
        timer.start()
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                raw.write(line)
                raw.flush()
                for o in adapter.map_event(line, ctx):
                    bus.post(name, o.dst, o.kind, o.text, **o.data)
                if ctx.thread_id and ctx.thread_id != saved_thread:
                    saved_thread = ctx.thread_id
                    st["thread_id"] = ctx.thread_id
                    save_state(cfg, name, st)
            rc = proc.wait()
        except KeyboardInterrupt:
            _kill(proc)
            try:
                proc.wait(timeout=5)
            except Exception:
                _kill(proc, signal.SIGKILL)
            bus.post(name, arch, "error", "stopped by the user")
            bus.post(name, None, "status", "stopped", n=n)
            st.update(tasks=n, thread_id=ctx.thread_id or st.get("thread_id"), last_exit=130)
            save_state(cfg, name, st)
            print(f"{name}: task {n} stopped", file=out)
            return 130
        finally:
            timer.cancel()
            if old_term is not None:
                signal.signal(signal.SIGTERM, old_term)

    secs = round(time.time() - t0)
    if timed_out.is_set():
        bus.post(name, arch, "error", f"timed out after {timeout}s")
    final = adapter.final_message(ctx, result_path)
    if timed_out.is_set():
        rc = 124
    elif rc is None or rc < 0:
        rc = 1
    elif rc == 0 and ctx.failed:
        rc = 1
    ok = rc == 0 and bool(final)
    if final:
        result_path.write_text(final.rstrip() + "\n")
    st.update(tasks=n, thread_id=ctx.thread_id or st.get("thread_id"), last_result=str(result_path),
              last_exit=rc, kind=w.kind)
    save_state(cfg, name, st)
    data = dict(exit_code=rc if rc != 0 or ok else 1, seconds=secs, commands=ctx.commands, tokens=ctx.tokens,
                thread_id=ctx.thread_id, result_path=str(result_path), n=n)
    if ctx.cost is not None:
        data["cost_usd"] = round(ctx.cost, 4)
    bus.post(name, arch, "result", final or "(no final message)", **data)
    bus.post(name, None, "status", "idle" if ok else f"failed (exit {data['exit_code']})", n=n)
    print(f"{name}: task {n} {'done' if ok else 'FAILED'} in {secs}s · exit {rc} · {ctx.commands} commands · "
          f"{ctx.tokens} tokens · thread {ctx.thread_id}\nresult file: {result_path}\n\n{final}", file=out)
    return rc if rc != 0 else (0 if ok else 1)


def status_lines(cfg: Config) -> list[str]:
    rows = []
    for w in cfg.workers:
        st = load_state(cfg, w.name)
        rows.append(f"{w.name:12} {w.kind:6} {w.model or 'default':14} tasks {int(st.get('tasks', 0)):2}  "
                    f"thread {st.get('thread_id') or '-'}  last exit {st.get('last_exit', '-')}  "
                    f"{st.get('last_result') or ''}".rstrip())
    return rows or ["no workers configured"]
