"""Bring a project's crew up in tmux, or stop it.

wide:  Claude Code left, the dashboard right (and an optional raw worker log).
strip: Claude Code left, a narrow status strip in the middle, the worker's own interactive Codex right.

All crew sessions live on one dedicated tmux server (`tmux -L crew`), so they never touch your
own tmux sessions. Each project gets its own session, `crew-<name>-<hash of its path>`.
"""
from __future__ import annotations

import hashlib
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import attached, protocol
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
set -g status-right " crew · Ctrl-b d detaches · crew stop closes "
set -g pane-border-status top
set -g pane-border-format " #{{pane_title}} "
set -g allow-set-title off
set -g pane-border-style "fg=#3B3650"
set -g pane-active-border-style "fg=#C084FC"
"""


def tmux_literal(s: str, strftime: bool = True) -> str:
    """Text that tmux shows as-is: no #{formats} or #(commands), and with strftime (status lines
    pass through it, pane titles do not) no %-sequences either."""
    s = re.sub(r"[\x00-\x1f\x7f]+", " ", s).replace("#", "##")
    return s.replace("%", "%%") if strftime else s


def status_left(cfg: Config) -> str:
    return f" crew · {tmux_literal(cfg.name)} "


def trio(cfg: Config):
    """The worker for the right pane when the layout is strip, else None. Only a Codex worker can be
    attached today, so a project whose first worker is Claude keeps the wide layout."""
    if cfg.layout == "strip" and cfg.workers and cfg.workers[0].kind == "codex":
        return cfg.workers[0]
    return None


def pane_titles(cfg: Config) -> tuple[str, ...]:
    """Border labels for the panes in order, escaped for select-pane -T: architect, dashboard, raw log;
    or with the strip layout architect, status, worker."""
    arch = "architect · Claude Code" + (f" · {cfg.architect.model}" if cfg.architect.model else "")
    w = trio(cfg)
    titles = (arch, "status", f"worker · Codex · {w.model or 'default'}") if w else \
        (arch, "dashboard · q quits, 1-9 focus an agent", "raw worker log")
    return tuple(tmux_literal(t, strftime=False) for t in titles)


def session_name(cfg: Config) -> str:
    """crew-<name>-<hash of the project path>: two clones with one name, or names that differ only
    in characters tmux refuses ('.' and ':'), still get sessions of their own."""
    base = re.sub(r"[^A-Za-z0-9_-]+", "-", cfg.name).strip("-") or "project"
    digest = hashlib.sha1(str(Path(cfg.root).resolve()).encode()).hexdigest()[:6]
    return f"crew-{base}-{digest}"


@dataclass
class Panes:
    architect: str          # the shell command for the left pane
    dashboard: str          # the right pane (the middle one with the strip layout)
    raw: str | None         # optional bottom-right pane
    worker: str | None = None   # strip layout: the worker's interactive agent, right


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
    prefix = f"cd {root} && PATH={q(str(BIN))}:\"$PATH\" CREW_PROJECT={root} CREW_PYTHON={q(py)}"
    architect = f"{prefix} {claude_cmd}"
    w = trio(cfg)
    dashboard = (f"cd {root} && PYTHONPATH={q(str(REPO))} COLORTERM=truecolor "
                 f"{q(py)} -m crew dashboard --project {root} --layout {'strip' if w else 'wide'}"
                 f" || {{ echo; read -r -p 'dashboard exited with an error, press Enter'; }}")
    if w:
        return Panes(architect, dashboard, None, attached.pane_shell(cfg, w, prefix))
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
    if panes.worker:
        w(f"\n# terminal 2 (middle, 44+ columns): the status strip\n{panes.dashboard}\n")
        w(f"\n# terminal 3 (right): the worker, interactive Codex"
          f" (from a hand-opened Codex instead: crew attach {cfg.workers[0].name})\n{panes.worker}\n")
        return
    w(f"\n# terminal 2 (right, {cfg.dashboard.split}% wide): the dashboard\n{panes.dashboard}\n")
    if panes.raw:
        w(f"\n# terminal 3 (optional): newest raw worker log\n{panes.raw}\n")


def up(cfg: Config, fresh: bool = False, detached: bool = False, claude_args: str = "",
       dry_run: bool = False) -> int:
    if cfg.layout == "strip" and not trio(cfg):
        print("layout strip needs a codex worker on the right; using wide")
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
    if panes.worker:
        # architect 40% | status 22% (48 columns of the first 220) | worker the rest
        tmux("split-window", "-h", "-t", f"={name}:main.0", "-l", "60%", "-c", str(cfg.root),
             in_bash(panes.dashboard))
        r = tmux("split-window", "-h", "-t", f"={name}:main.1", "-l", "63%", "-c", str(cfg.root), "-P", "-F",
                 "#{pane_id}", in_bash(panes.worker), capture=True)
        pane_id = (getattr(r, "stdout", "") or "").strip() or f"={name}:main.2"
        attached.write_pane(cfg, cfg.workers[0].name, pane_id)
    else:
        tmux("split-window", "-h", "-t", f"={name}:main", "-l", f"{cfg.dashboard.split}%", "-c", str(cfg.root),
             in_bash(panes.dashboard))
    if panes.raw:
        tmux("split-window", "-v", "-t", f"={name}:main.1", "-l", "30%", "-c", str(cfg.root), in_bash(panes.raw))
    tmux("set-option", "-t", f"={name}:", "status-left", status_left(cfg), check=False, capture=True)
    titles = pane_titles(cfg)
    for i in range(3 if panes.raw or panes.worker else 2):
        tmux("select-pane", "-t", f"={name}:main.{i}", "-T", titles[i], check=False, capture=True)
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
