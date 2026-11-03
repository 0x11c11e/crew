"""Attached workers: the worker is an interactive Codex in a tmux pane instead of a headless run.

`crew up` with the strip layout starts it in the right pane and records the pane in
.crew/workers/<name>/pane (`crew attach NAME` does the same for a Codex opened by hand). `crew run`
then types one line into that pane pointing at the brief, and waits for task-NN.result.md. Codex writes
nothing itself: its notify hook runs `crew codex-notify <json>` after every turn, and that writes the
result and posts it, but only while a task is open (.crew/workers/<name>/current). A turn with no open
task is Iman talking to Codex directly, and is left alone.
"""
from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from .bus import Bus
from .config import Config, ConfigError, Worker, ensure_state, find_project, load

SOCKET = "crew"
NOTIFY = '["crew","codex-notify"]'
POLL = 0.5                      # seconds between looks for the result file
TYPE_PAUSE = 0.5                # between typing the line and Enter, so the TUI does not take it for a paste
LINE = ("Read the brief in {path} and do it. When done, reply with a short summary of what you changed "
        "and how you verified it.")


def tmux(*args: str, socket: str = "") -> subprocess.CompletedProcess:
    """Every tmux call this module makes. socket: a server path from $TMUX; empty: the crew server."""
    cmd = ["tmux", "-S", socket] if socket else ["tmux", "-L", SOCKET]
    try:
        return subprocess.run(cmd + list(args), text=True, capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


def interactive_command(w: Worker, cwd: Path | str) -> list[str]:
    """The interactive Codex for the right pane. Approvals off: nobody watches that pane for prompts."""
    cmd = ["codex", "-s", w.sandbox, "-a", "never", "-C", str(cwd), "-c", f"notify={NOTIFY}",
           "-c", f'model_reasoning_effort="{w.effort}"']
    if w.model:
        cmd += ["-m", w.model]
    return cmd


def wdir(cfg: Config, name: str) -> Path:
    return cfg.state / "workers" / name


def write_pane(cfg: Config, name: str, pane: str, socket: str = "") -> Path:
    from .worker import write_private
    ensure_state(cfg.root)
    d = wdir(cfg, name)
    d.mkdir(parents=True, exist_ok=True)
    p = d / "pane"
    write_private(p, f"{pane}\n{socket}\n")
    return p


def read_pane(cfg: Config, name: str) -> tuple[str, str] | None:
    try:
        lines = (wdir(cfg, name) / "pane").read_text().splitlines()
    except OSError:
        return None
    if not lines or not lines[0].strip():
        return None
    return lines[0].strip(), (lines[1].strip() if len(lines) > 1 else "")


def live_pane(cfg: Config, name: str) -> tuple[str, str] | None:
    """The worker's pane when it has one and tmux still knows it; a gone pane says so once on stderr."""
    rec = read_pane(cfg, name)
    if rec is None:
        return None
    pane, socket = rec
    if tmux("display", "-p", "-t", pane, "#{pane_id}", socket=socket).returncode == 0:
        return rec
    print(f"{name}: attached pane is gone; running headless", file=sys.stderr)
    return None


def attach(cfg: Config, name: str) -> int:
    """`crew attach NAME`: this pane (a Codex you started yourself) becomes NAME."""
    cfg.worker(name)
    pane = os.environ.get("TMUX_PANE")
    if not pane or not os.environ.get("TMUX"):
        raise ConfigError("not inside tmux: run `crew attach NAME` in the pane where Codex will run")
    socket = os.environ["TMUX"].split(",")[0]
    p = write_pane(cfg, name, pane, socket)
    print(f"{name} is attached to pane {pane}; `crew run {name}` now types into it ({p.relative_to(cfg.root)})")
    return 0


# ----------------------------------------------------------------------------- dispatch

def run(cfg: Config, w: Worker, task: str, pane: tuple[str, str], timeout: int | None, out, fd: int) -> int:
    """Type the task into the attached pane and wait for its result file. The lock is held by the caller."""
    from .worker import load_state, save_state, write_private
    name, arch = w.name, cfg.architect.name
    bus = Bus(cfg.root)
    timeout = timeout or w.timeout
    st = load_state(cfg, name)
    n = int(st.get("tasks", 0)) + 1
    os.ftruncate(fd, 0)
    os.pwrite(fd, f"{n}\n".encode(), 0)
    d = wdir(cfg, name)
    task_path = d / f"task-{n:02d}.md"
    result_path = d / f"task-{n:02d}.result.md"
    write_private(task_path, task)
    result_path.unlink(missing_ok=True)
    cwd = cfg.worker_cwd(w)
    if int(st.get("tasks", 0)) == 0:
        bus.post(name, None, "connect", f"{name} online · Codex, attached to pane {pane[0]}")
    bus.post(arch, name, "task", task, n=n, resume=False, cwd=str(cwd), kind=w.kind, model=w.model or None,
             attached=True)
    bus.post(name, None, "status", "working", n=n)
    current = d / "current"
    write_private(current, f"{n}\n")

    def on_term(signum, frame):
        raise KeyboardInterrupt(signum)

    main = threading.current_thread() is threading.main_thread()
    old = {sig: signal.signal(sig, on_term) for sig in (signal.SIGTERM, signal.SIGHUP)} if main else {}
    t0 = time.time()
    try:
        line = LINE.format(path=task_path.resolve())
        target, socket = pane
        tmux("send-keys", "-t", target, "-l", line, socket=socket)
        time.sleep(TYPE_PAUSE)
        tmux("send-keys", "-t", target, "Enter", socket=socket)
        while not result_path.exists():
            if time.time() - t0 > timeout:
                bus.post(name, arch, "error", f"timed out after {timeout}s waiting for the attached pane")
                bus.post(name, None, "status", "failed (timeout)", n=n)
                st = load_state(cfg, name)
                st.update(tasks=max(n, int(st.get("tasks", 0))), last_exit=124, kind=w.kind)
                save_state(cfg, name, st)
                print(f"{name}: task {n} timed out after {timeout}s", file=out)
                return 124
            time.sleep(POLL)
    except KeyboardInterrupt as e:
        hup = bool(e.args) and e.args[0] == signal.SIGHUP
        bus.post(name, arch, "error", "stopped: the terminal closed" if hup else "stopped by the user")
        bus.post(name, None, "status", "stopped", n=n)
        st = load_state(cfg, name)
        st.update(tasks=max(n, int(st.get("tasks", 0))), last_exit=130, kind=w.kind)
        save_state(cfg, name, st)
        print(f"{name}: task {n} stopped", file=out)
        return 130
    finally:
        current.unlink(missing_ok=True)
        for sig, h in old.items():
            signal.signal(sig, h)
    secs = round(time.time() - t0)
    final = result_path.read_text().strip()
    st = load_state(cfg, name)
    st.update(tasks=max(n, int(st.get("tasks", 0))), last_result=str(result_path), last_exit=0, kind=w.kind)
    save_state(cfg, name, st)
    print(f"{name}: task {n} done in {secs}s · exit 0 · attached pane {pane[0]} · thread {st.get('thread_id')}\n"
          f"result file: {result_path}\n\n{final}", file=out)
    return 0


# ----------------------------------------------------------------------------- the notify hook

def _project() -> Path | None:
    env = os.environ.get("CREW_PROJECT")
    return Path(env).expanduser().resolve() if env else find_project()


def notify(argv: list[str]) -> int:
    """`crew codex-notify <json>`: Codex finished a turn. Never raises: Codex runs this inline."""
    try:
        return _notify(argv)
    except Exception as e:                                   # one line, never a traceback into Codex
        print(f"crew codex-notify: {type(e).__name__}: {e}", file=sys.stderr)
        return 0


def _notify(argv: list[str]) -> int:
    from .worker import load_state, save_state, write_private
    try:
        payload = json.loads(argv[-1]) if argv else {}
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    root = _project()
    if root is None:
        return 0
    cfg = load(root)
    open_tasks = [w for w in cfg.workers if (wdir(cfg, w.name) / "current").is_file()]
    if not open_tasks:
        return 0                                             # someone talking to Codex directly
    me = os.environ.get("TMUX_PANE")
    if len(open_tasks) > 1 and me:
        open_tasks = [w for w in open_tasks if (read_pane(cfg, w.name) or ("",))[0] == me] or open_tasks
    w = open_tasks[0]
    d = wdir(cfg, w.name)
    try:
        n = int((d / "current").read_text().strip())
    except (OSError, ValueError):
        return 0
    text = payload.get("last-assistant-message") or payload.get("last_assistant_message") or ""
    final = text.strip() if isinstance(text, str) and text.strip() else "(no final message)"
    thread = payload.get("thread-id") or payload.get("thread_id") or None
    st = load_state(cfg, w.name)
    if thread:
        st["thread_id"] = thread
    st.update(tasks=max(n, int(st.get("tasks", 0))), kind=w.kind)
    save_state(cfg, w.name, st)
    result = d / f"task-{n:02d}.result.md"
    tmp = result.with_suffix(".tmp")
    write_private(tmp, final + "\n")
    tmp.replace(result)                                      # the waiting `crew run` never sees half a file
    try:
        secs = max(0, round(time.time() - (d / f"task-{n:02d}.md").stat().st_mtime))
    except OSError:
        secs = None
    bus = Bus(cfg.root)
    bus.post(w.name, cfg.architect.name, "result", final, exit_code=0, n=n, seconds=secs, thread_id=thread,
             result_path=str(result), attached=True)
    bus.post(w.name, None, "status", "idle", n=n)
    return 0


def pane_shell(cfg: Config, w: Worker, prefix: str) -> str:
    """The right pane's shell command: the same PATH and CREW_PROJECT prefix as the architect's."""
    return f"{prefix} " + " ".join(shlex.quote(c) for c in interactive_command(w, cfg.worker_cwd(w)))
