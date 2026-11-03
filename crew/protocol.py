"""Renders the architect's two files: .crew/ARCHITECT.md and .crew/claude-settings.json.

The command the protocol tells the architect to type and the pattern its allowlist matches come
from the same constant, CMD, so they can never drift apart.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .config import Config, ensure_state

CMD = "crew"                                              # what the architect types
ALLOWED_SUBCOMMANDS = ("say", "run", "verify", "post", "status", "tail")
REPO = Path(__file__).resolve().parents[1]                # repository root (contains crew/ and bin/)
HOOK = Path(__file__).resolve().parent / "hook.py"
HOOK_EVENTS = (
    ("SessionStart", None),
    ("UserPromptSubmit", None),
    ("PreToolUse", "Bash|Edit|Write|MultiEdit|Read|Agent"),
    ("PostToolUse", "Bash"),
    ("Stop", None),
)

ARCHITECT_MD = """\
# You are the architect ({arch})

Project: {name}. A dashboard beside you shows every message on the bus; keep each message short
and human. Workers implement; you plan, dispatch, verify.

## Your workers
{workers}

## Protocol, every request
1. Plan, one paragraph, then post it:  {cmd} say "Plan: ..."
2. Dispatch one worker per task, each in the background (Bash with run_in_background):
{dispatch}
   Add --continue to keep a worker's thread for a follow-up (it remembers what it did).
   Add --read-only when the worker must only look and report (a question, a review, a scan).
   Workers can run in parallel when their briefs form a contract.
3. Wait. You are re-invoked when a worker exits; its output names the result file.
4. Verify for yourself (run the tests, run the program, read the diff), then post the verdict:
   {cmd} verify {first} pass "..."   |   {cmd} verify {first} fail "what is wrong"
5. Iterate with --continue, or close:  {cmd} say "Done: ..."

## Rules
- Size the brief to the request. A question or a look around gets a read-only brief that answers
  it, not an audit; add no deliverables the user did not ask for. A small question you can answer
  from one or two files needs no worker at all.
- A brief is a short markdown note: `# <title>` first (the dashboard shows it on the worker's
  card), then what to do, what done looks like, and what not to touch.
- Never implement a task yourself; a one-line fix only after a worker failed twice.
- Keep your own tokens lean: no reading raw .crew/ logs, no re-reading files you already saw.
- Say what you are doing in one line before each step.
"""


def command(sub: str) -> str:
    """The exact prefix the architect types for a subcommand, e.g. 'crew say'."""
    return f"{CMD} {sub}"


def allow_patterns() -> list[str]:
    return [f"Bash({command(s)}:*)" for s in ALLOWED_SUBCOMMANDS]


def render_architect(cfg: Config) -> str:
    workers = "\n".join(f"- {w.name} — {w.describe()}" for w in cfg.workers) or \
        "- (none configured; add [[workers]] to crew.toml)"
    lines = []
    for i, w in enumerate(cfg.workers):
        if i == 0:
            lines.append(f"   {command('run')} {w.name} --task-file tasks/01-something.md")
        else:
            lines.append(f'   {command("run")} {w.name} --task "..."')
    if not lines:
        lines.append(f'   {command("run")} <worker> --task "..."')
    first = cfg.workers[0].name if cfg.workers else "<worker>"
    text = ARCHITECT_MD.format(arch=cfg.architect.name, name=cfg.name, workers=workers, cmd=CMD,
                               dispatch="\n".join(lines), first=first)
    notes = (cfg.architect.notes or "").strip()
    if notes:
        text += "\n" + notes + "\n"
    return text


def hook_command(event: str, python: str | None = None) -> str:
    py = python or sys.executable
    return f"{_sh(py)} {_sh(str(HOOK))} {event}"


def _sh(s: str) -> str:
    import shlex
    return shlex.quote(s)


def render_settings(cfg: Config | None = None, python: str | None = None) -> dict:
    hooks: dict = {}
    for event, matcher in HOOK_EVENTS:
        entry: dict = {}
        if matcher:
            entry["matcher"] = matcher
        entry["hooks"] = [{"type": "command", "command": hook_command(event, python), "timeout": 5}]
        hooks[event] = [entry]
    return {"permissions": {"allow": allow_patterns()}, "hooks": hooks}


def write_all(cfg: Config) -> tuple[Path, Path]:
    """Render both files into .crew/. Cheap, so `crew up` does it every time."""
    ensure_state(cfg.root)
    md = cfg.state / "ARCHITECT.md"
    md.write_text(render_architect(cfg))
    st = cfg.state / "claude-settings.json"
    st.write_text(json.dumps(render_settings(cfg), indent=2) + "\n")
    return md, st
