#!/usr/bin/env python3
"""Claude Code hook → crew bus.

Run as a script: `python3 /abs/path/crew/hook.py <Event>` (or `crew hook <Event>`).
Reads the hook payload (JSON) on stdin, posts one event for the architect, and always exits 0
with nothing on stdout, so it can never block or alter what Claude Code does. The event name is
taken from the payload's hook_event_name, with argv[1] as a fallback.
"""
import json
import os
import sys
from pathlib import Path

if __package__ in (None, ""):
    # Run as a script: make `import crew` resolve to this repository, not to crew/crew/.
    _repo = str(Path(__file__).resolve().parents[1])
    if sys.path and Path(sys.path[0]).resolve() == Path(__file__).resolve().parent:
        sys.path.pop(0)
    sys.path.insert(0, _repo)

SELF_NOISE = ("crew ",)          # `crew run/say/verify/...` post their own, nicer events


def _walk_up(start: Path) -> Path | None:
    for d in (start, *start.parents):
        if (d / "crew.toml").is_file():
            return d
    return None


def find_root(payload: dict) -> Path | None:
    """CLAUDE_PROJECT_DIR, else the payload's cwd (or its crew.toml ancestor), else walk up from cwd."""
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env and Path(env).is_dir():
        return Path(env).resolve()
    cwd = payload.get("cwd")
    if cwd and Path(cwd).is_dir():
        p = Path(cwd).resolve()
        return _walk_up(p) or p
    return _walk_up(Path(os.getcwd()).resolve())


def architect_name(root: Path) -> str:
    try:
        import tomllib
        raw = tomllib.loads((root / "crew.toml").read_text())
        return str((raw.get("architect") or {}).get("name") or "fable")
    except Exception:
        return "fable"


def rel(path: str, root: Path) -> str:
    try:
        return os.path.relpath(path, root) if path.startswith(str(root)) else path
    except Exception:
        return path


def is_self(cmd: str) -> bool:
    return any(n in cmd for n in SELF_NOISE)


def handle(payload: dict, fallback: str = "") -> None:
    root = find_root(payload)
    if root is None:
        return
    from crew.bus import Bus
    bus = Bus(root)
    me = architect_name(root)
    d = payload
    event = d.get("hook_event_name") or fallback
    tool = d.get("tool_name") or ""
    ti = d.get("tool_input") or {}
    if event == "SessionStart":
        bus.post(me, None, "connect", f"{me} online · architect (Claude Code)")
    elif event == "UserPromptSubmit":
        bus.post("user", me, "prompt", d.get("prompt") or "")
    elif event == "PreToolUse":
        if tool == "Bash":
            cmd = ti.get("command") or ""
            if not is_self(cmd):
                bus.post(me, None, "exec", cmd, desc=ti.get("description"))
        elif tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            bus.post(me, None, "edit", rel(ti.get("file_path") or ti.get("notebook_path") or "", root))
        elif tool == "Read":
            bus.post(me, None, "read", rel(ti.get("file_path") or "", root))
        elif tool == "Agent":
            bus.post(me, None, "status", f"spawning a subagent: {ti.get('description') or ''}")
    elif event == "PostToolUse":
        if tool == "Bash":
            cmd = ti.get("command") or ""
            if not is_self(cmd):
                tr = d.get("tool_response")
                if isinstance(tr, dict):            # Bash: {"stdout": ..., "stderr": ..., ...}
                    out = str(tr.get("stdout") or "")
                    if tr.get("stderr"):
                        out += "\n" + str(tr["stderr"])
                    rc = tr.get("exit_code")
                else:
                    out, rc = (str(tr) if tr else ""), None
                bus.post(me, None, "exec_done", cmd, status="completed", exit_code=rc, output=out.rstrip()[-1200:])
    elif event == "Stop":
        last = (d.get("last_assistant_message") or "").strip()
        if last:
            bus.post(me, "user", "message", last)
        bus.post(me, "user", "status", "waiting for you")


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if os.environ.get("CREW_WORKER"):
        return 0                       # a Claude worker must never post as the architect
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}
    try:
        handle(payload, argv[0] if argv else "")
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    try:
        main()
    finally:
        os._exit(0)
