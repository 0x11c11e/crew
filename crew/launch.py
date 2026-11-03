"""Bring a project's crew up in tmux (Claude Code left, dashboard right), or stop it.

All crew sessions live on one dedicated tmux server (`tmux -L crew`), so they never touch your
own tmux sessions. Each project gets its own session, `crew-<name>`.
"""
from __future__ import annotations

import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import protocol
from .bus import Bus, unique
from .config import Config, ensure_state

SOCKET = "crew"
REPO = protocol.REPO
BIN = REPO / "bin"

TMUX_CONF = """\
# tmux config for the crew server only (crew up starts tmux with -L {socket} -f this file)
set -g default-terminal "screen-256color"
set -ga terminal-overrides ",*:Tc"
set -g mouse on
set -g history-limit 50000
set -g status-style "bg=#1F1B2E,fg=#C084FC"
set -g status-left-length 40
set -g status-right-length 80
set -g status-left " crew "
set -g status-right " Ctrl-b d detaches · crew stop closes "
set -g pane-border-style "fg=#3B3650"
set -g pane-active-border-style "fg=#C084FC"
"""


def session_name(cfg: Config) -> str:
    """tmux session names cannot contain '.' or ':'."""
    return "crew-" + (re.sub(r"[^A-Za-z0-9_-]+", "-", cfg.name).strip("-") or "project")


@dataclass
class Panes:
    architect: str          # the shell command for the left pane
    dashboard: str          # the right pane
    raw: str | None         # optional bottom-right pane


def pane_commands(cfg: Config, claude_args: str = "", python: str | None = None) -> Panes:
    py = python or sys.executable
    q = shlex.quote
    root = q(str(cfg.root))
    claude = ["claude", "--settings", ".crew/claude-settings.json",
              "--append-system-prompt-file", ".crew/ARCHITECT.md"]
    if cfg.architect.model:
        claude += ["--model", cfg.architect.model]
    claude_cmd = " ".join(q(c) for c in claude) + (f" {claude_args.strip()}" if claude_args.strip() else "")
    # Prepend, never replace: $PATH is expanded by the pane's shell, not here.
    architect = (f"cd {root} && PATH={q(str(BIN))}:\"$PATH\" CREW_PROJECT={root} CREW_PYTHON={q(py)} {claude_cmd}")
    dashboard = (f"cd {root} && PYTHONPATH={q(str(REPO))} COLORTERM=truecolor "
                 f"{q(py)} -m crew dashboard --project {root}"
                 f" || {{ echo; read -r -p 'dashboard exited with an error, press Enter'; }}")
    raw = None
    if cfg.dashboard.raw:
        raw = (f"cd {root} && cur=; pid=; while true; do "
               f"f=$(ls -t .crew/workers/*/task-*.raw.jsonl 2>/dev/null | head -1); "
               f"if [ -n \"$f\" ] && [ \"$f\" != \"$cur\" ]; then [ -n \"$pid\" ] && kill $pid 2>/dev/null; "
               f"cur=$f; clear; echo \"== $f\"; tail -n 20 -F \"$f\" & pid=$!; fi; sleep 1; done")
    return Panes(architect, dashboard, raw)


def tmux(*args: str, conf: Path | None = None, check: bool = True, capture: bool = False):
    cmd = ["tmux", "-L", SOCKET]
    if conf:
        cmd += ["-f", str(conf)]
    return subprocess.run(cmd + list(args), check=check, text=True,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None)


def in_bash(cmd: str) -> str:
    """Run a pane command under bash whatever tmux's default shell is."""
    return "bash -c " + shlex.quote(cmd)


def has_session(name: str) -> bool:
    if not shutil.which("tmux"):
        return False
    return tmux("has-session", "-t", f"={name}", check=False, capture=True).returncode == 0


def prepare(cfg: Config) -> tuple[Path, Path]:
    """Steps 2 and 3 of `crew up`: the state folder and the rendered protocol and settings."""
    ensure_state(cfg.root)
    return protocol.write_all(cfg)


def archive(cfg: Config, fresh: bool) -> list[str]:
    """Step 4: reset the bus; with fresh, move worker state (threads) aside too."""
    notes = []
    dest = Bus(cfg.root).reset()
    if dest:
        notes.append(f"previous bus archived to {dest.relative_to(cfg.root)}")
    wdir = cfg.state / "workers"
    if fresh and wdir.is_dir() and any(wdir.iterdir()):
        (cfg.state / "archive").mkdir(exist_ok=True)
        target = unique(cfg.state / "archive" / time.strftime("workers-%Y%m%d-%H%M%S"))
        wdir.rename(target)
        notes.append(f"worker threads archived to {target.relative_to(cfg.root)}")
    return notes


def print_plan(cfg: Config, panes: Panes, out=None) -> None:
    w = (out or sys.stdout).write
    w(f"project   {cfg.root}\n")
    w(f"session   tmux -L {SOCKET} attach -t {session_name(cfg)}\n")
    w(f"\n# terminal 1 (left): the architect, Claude Code\n{panes.architect}\n")
    w(f"\n# terminal 2 (right, {cfg.dashboard.split}% wide): the dashboard\n{panes.dashboard}\n")
    if panes.raw:
        w(f"\n# terminal 3 (optional): newest raw worker log\n{panes.raw}\n")


def up(cfg: Config, fresh: bool = False, detached: bool = False, claude_args: str = "",
       dry_run: bool = False) -> int:
    panes = pane_commands(cfg, claude_args)
    md, st = prepare(cfg)
    if dry_run:
        print(f"rendered  {md.relative_to(cfg.root)}, {st.relative_to(cfg.root)}")
        print_plan(cfg, panes)
        print("\n(dry run: bus not reset, tmux not started)")
        return 0
    name = session_name(cfg)
    if not shutil.which("tmux"):
        for n in archive(cfg, fresh):
            print(n)
        print("tmux is not on PATH. Run these in two terminals (the dashboard one 120+ columns wide):\n")
        print_plan(cfg, panes)
        return 0
    if has_session(name):
        print(f"{name} is already running; attaching. `crew stop` first to restart it.")
        return attach(name, detached)
    for n in archive(cfg, fresh):
        print(n)
    conf = cfg.state / "tmux.conf"
    conf.write_text(TMUX_CONF.format(socket=SOCKET))
    tmux("source-file", str(conf), check=False, capture=True)          # if the server already runs
    tmux("new-session", "-d", "-s", name, "-n", "main", "-x", "220", "-y", "50", "-c", str(cfg.root),
         "-e", "COLORTERM=truecolor", in_bash(panes.architect), conf=conf)
    tmux("split-window", "-h", "-t", f"={name}:main", "-l", f"{cfg.dashboard.split}%", "-c", str(cfg.root),
         in_bash(panes.dashboard))
    if panes.raw:
        tmux("split-window", "-v", "-t", f"={name}:main.1", "-l", "30%", "-c", str(cfg.root), in_bash(panes.raw))
    tmux("set-option", "-t", f"={name}:", "status-left", f" crew · {cfg.name} ", check=False, capture=True)
    tmux("select-pane", "-t", f"={name}:main.0")
    return attach(name, detached)


def attach(name: str, detached: bool) -> int:
    cmd = f"tmux -L {SOCKET} attach -t {name}"
    if not detached and not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("not a terminal, so not attaching.")
        detached = True
    if detached:
        print(f"started detached; attach with: {cmd}")
        return 0
    return subprocess.call(["tmux", "-L", SOCKET, "attach", "-t", f"={name}"])


def stop(cfg: Config) -> int:
    name = session_name(cfg)
    if not has_session(name):
        print(f"{name} is not running")
        return 0
    tmux("kill-session", "-t", f"={name}", check=False)
    print(f"closed {name}")
    return 0
