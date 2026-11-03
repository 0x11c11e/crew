"""`crew run NAME`: run one worker on one task and narrate everything it does onto the bus.

The adapter (workers/codex.py or workers/claude.py) builds the command line and maps each line
of the agent's JSON stream to bus events. This module does the rest: task files, state, the raw
log, the timeout, Ctrl-C, the final result and the summary the architect reads.
"""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

from . import attached
from .bus import Bus
from .config import Config, ConfigError, Worker, ensure_state
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


def private(path: Path, mode: str = "w"):
    """open() for a file only the user may read: .crew/ is 0700 already, this is belt and braces."""
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if mode == "a" else os.O_TRUNC)
    fd = os.open(path, flags, 0o600)
    os.fchmod(fd, 0o600)                # a file left by an older crew keeps its mode otherwise
    return os.fdopen(fd, mode)


def write_private(path: Path, text: str) -> None:
    with private(path) as fh:
        fh.write(text)


def save_state(cfg: Config, name: str, st: dict) -> None:
    p = worker_dir(cfg, name) / "state.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    write_private(tmp, json.dumps(st, indent=2))
    tmp.replace(p)


def _kill(proc: subprocess.Popen, sig=signal.SIGTERM) -> None:
    try:
        os.killpg(proc.pid, sig)
    except Exception:
        try:
            proc.send_signal(sig)
        except Exception:
            pass


def _reap(proc: subprocess.Popen | None) -> None:
    """Stop the agent's whole process group and wait for the agent itself. Safe to call twice."""
    if proc is None:
        return
    _kill(proc)                         # leftover children too, even when the agent already exited
    if proc.poll() is None:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _kill(proc, signal.SIGKILL)
            proc.wait()


def lock(cfg: Config, name: str) -> int | None:
    """Hold .crew/workers/<name>/.lock for one run. None when another run already holds it."""
    wdir = worker_dir(cfg, name)
    wdir.mkdir(parents=True, exist_ok=True)
    fd = os.open(wdir / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def running_task(cfg: Config, name: str) -> str:
    try:
        return (worker_dir(cfg, name) / ".lock").read_text().strip() or "?"
    except OSError:
        return "?"


READ_ONLY_TOOLS = "Read Glob Grep"
READ_ONLY_DENIED = "Bash Edit Write MultiEdit NotebookEdit"   # deny rules win over a project's own allow rules


def read_only(w: Worker) -> Worker:
    """The same worker, unable to change files: for questions and reviews."""
    if w.kind == "codex":
        return replace(w, sandbox="read-only")
    return replace(w, permission_mode="default", allowed_tools=READ_ONLY_TOOLS, disallowed_tools=READ_ONLY_DENIED)


def run(cfg: Config, name: str, task: str, cont: bool = False, timeout: int | None = None,
        out=None, readonly: bool = False) -> int:
    """One task, one worker at a time: a second run of a busy worker exits 1 at once. A worker with a live
    attached pane (crew up's strip layout, or crew attach) gets the task typed into it instead."""
    w = cfg.worker(name)
    if not task.strip():
        raise ConfigError("the task is empty")
    pane = attached.live_pane(cfg, name) if w.kind == "codex" else None
    if pane and readonly:
        raise ConfigError(f"{name} is attached to an interactive Codex, whose sandbox was chosen when its "
                          "pane started; --read-only cannot apply. Ask it in the pane, or close the pane "
                          "to run headless")
    if readonly:
        w = read_only(w)
    ensure_state(cfg.root)                    # task files land in .crew/ before the first post
    fd = lock(cfg, name)
    if fd is None:
        msg = f"{name} is already running (task {running_task(cfg, name)})"
        print(msg, file=sys.stderr)
        Bus(cfg.root).post(name, cfg.architect.name, "error", f"refused a second task: {msg}")
        return 1
    try:
        if pane:
            return attached.run(cfg, w, task, pane, timeout, out or sys.stdout, fd)
        return _run(cfg, w, task, cont, timeout, out or sys.stdout, fd, readonly)
    finally:
        os.close(fd)


def _run(cfg: Config, w: Worker, task: str, cont: bool, timeout: int | None, out, fd: int,
         readonly: bool) -> int:
    name = w.name
    adapter = ADAPTERS[w.kind]
    bus = Bus(cfg.root)
    arch = cfg.architect.name
    timeout = timeout or w.timeout

    st = load_state(cfg, name)
    n = int(st.get("tasks", 0)) + 1
    os.ftruncate(fd, 0)
    os.pwrite(fd, f"{n}\n".encode(), 0)
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
    write_private(task_path, task)
    cwd = cfg.worker_cwd(w)

    if int(st.get("tasks", 0)) == 0 and not resume_id:
        bus.post(name, None, "connect", f"{name} online · {adapter.describe(w)}")
    bus.post(arch, name, "task", task, n=n, resume=bool(resume_id), cwd=str(cwd), kind=w.kind,
             model=w.model or None, read_only=readonly or None)
    bus.post(name, None, "status", f"starting {w.kind}{' (read-only)' if readonly else ''}…", n=n)

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
        raise KeyboardInterrupt(signum)

    main = threading.current_thread() is threading.main_thread()
    old_handlers = {sig: signal.signal(sig, on_term) for sig in (signal.SIGTERM, signal.SIGHUP)} if main else {}
    rc = None
    map_errors = 0
    proc = None
    timer = None
    try:
        with private(raw_path) as raw:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, bufsize=1, errors="replace", cwd=str(cwd), env=env,
                                    start_new_session=True)

            def kill_on_timeout() -> None:
                timed_out.set()
                _kill(proc, signal.SIGKILL)

            timer = threading.Timer(timeout, kill_on_timeout)
            timer.daemon = True
            timer.start()
            assert proc.stdout is not None
            for line in proc.stdout:
                raw.write(line)
                raw.flush()
                try:
                    outs = adapter.map_event(line, ctx)
                except Exception as e:
                    outs = []
                    map_errors += 1
                    if map_errors == 1:
                        bus.post(name, arch, "error", f"skipped an event the {w.kind} adapter could not read "
                                 f"({type(e).__name__}: {e}); see {raw_path.name}")
                for o in outs:
                    bus.post(name, o.dst, o.kind, o.text, **o.data)
                if ctx.thread_id and ctx.thread_id != saved_thread:
                    saved_thread = ctx.thread_id
                    st["thread_id"] = ctx.thread_id
                    save_state(cfg, name, st)
            rc = proc.wait()
    except KeyboardInterrupt as e:
        hup = bool(e.args) and e.args[0] == signal.SIGHUP
        _reap(proc)
        bus.post(name, arch, "error", "stopped: the terminal closed" if hup else "stopped by the user")
        bus.post(name, None, "status", "stopped", n=n)
        st.update(tasks=n, thread_id=ctx.thread_id or st.get("thread_id"), last_exit=130)
        save_state(cfg, name, st)
        print(f"{name}: task {n} stopped", file=out)
        return 130
    finally:
        if timer is not None:
            timer.cancel()
        _reap(proc)                     # every exit path: the agent and anything it started go too
        for sig, h in old_handlers.items():
            signal.signal(sig, h)

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
        write_private(result_path, final.rstrip() + "\n")
    st.update(tasks=n, thread_id=ctx.thread_id or st.get("thread_id"), last_result=str(result_path),
              last_exit=rc, kind=w.kind)
    save_state(cfg, name, st)
    data = dict(exit_code=rc if rc != 0 or ok else 1, seconds=secs, commands=ctx.commands, tokens=ctx.tokens,
                thread_id=ctx.thread_id, result_path=str(result_path), n=n, map_errors=map_errors or None)
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
