"""crew command line. Run `crew --help`, or `crew` alone to be asked for a project directory."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import __version__, config
from .bus import Bus, Follower, fmt
from .config import ConfigError

SUBCOMMANDS = ("init", "up", "run", "say", "verify", "post", "tail", "status", "dashboard", "replay",
               "snapshot", "hook", "stop", "attach")


# ----------------------------------------------------------------------------- prompts

def interactive() -> bool:
    try:
        return sys.stdin.isatty()
    except Exception:
        return False


def ask(question: str, default: str, yes: bool) -> str:
    """Print `question [default]: ` and read a line; --yes or no tty returns the default."""
    if yes:
        return default
    try:
        ans = input(f"{question} [{default}]: ").strip()
    except EOFError:
        return default
    return ans or default


def confirm(question: str, yes: bool) -> bool:
    if yes:
        return True
    try:
        ans = input(f"{question} [Y/n] ").strip().lower()
    except EOFError:
        return True
    return ans in ("", "y", "yes")


def ask_choice(question: str, default: str, choices, yes: bool) -> str:
    while True:
        ans = ask(f"{question} ({' | '.join(choices)})", default, yes)
        if ans in choices:
            return ans
        print(f"  please answer one of: {', '.join(choices)}")


def ask_int(question: str, default: int, yes: bool, lo: int = 1, hi: int = 9) -> int:
    """Ask until the answer is in lo..hi. An out-of-range default is never retried: when the answer
    is the default (--yes, no tty, end of input or a blank line), warn once and clamp it into range."""
    while True:
        ans = ask(question, str(default), yes)
        if ans.isdigit() and lo <= int(ans) <= hi:
            return int(ans)
        if ans == str(default):
            fixed = min(max(default, lo), hi)
            print(f"  {question.lower()} must be from {lo} to {hi}; using {fixed}")
            return fixed
        print(f"  please answer a number from {lo} to {hi}")


def worker_count(s: str) -> int:
    """argparse type for --workers: an integer from 1 to 9."""
    try:
        n = int(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {s!r}") from None
    if not 1 <= n <= 9:
        raise argparse.ArgumentTypeError(f"must be from 1 to 9, got {n}")
    return n


# ----------------------------------------------------------------------------- init

def do_init(root: Path, yes: bool, workers: int | None = None, kind: str | None = None,
            model: str | None = None, worker_model: str | None = None, force: bool = False,
            show: bool = True) -> Path:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise ConfigError(f"{root} is not a directory")
    path = root / config.FILENAME
    if path.exists() and not force:
        raise ConfigError(f"{path} already exists; edit it, or pass --force to overwrite it")
    name = ask("Project name", root.name, yes)
    arch_model = ask("Architect model (blank = your Claude Code default)", model or "", yes) \
        if not yes else (model or "")
    if arch_model in ("''", '""'):
        arch_model = ""
    n = ask_int("Number of workers", workers or 1, yes)
    k = ask_choice("Worker kind", kind or "codex", config.KINDS, yes)
    w_model = ask("Worker model (blank = the agent's default)", worker_model or "", yes)
    if w_model in ("''", '""'):
        w_model = ""
    effort = ask_choice("Codex reasoning effort", "medium", config.EFFORTS, yes) if k == "codex" else "medium"
    text = config.template(name=name, architect_model=arch_model, workers=n, kind=k, effort=effort,
                           model=w_model)
    config.write(root, text)
    config.load(root)                         # validate what we wrote
    if show:
        print(f"\nwrote {path}:\n")
        print(text)
    return path


# ----------------------------------------------------------------------------- bring-up (crew [DIR])

def bring_up(a: argparse.Namespace) -> int:
    from . import launch
    yes = a.yes or not interactive()
    if a.dir is None:
        cwd = os.getcwd()
        # a configured project needs no question: `crew` in it just brings it up
        d = cwd if (Path(cwd) / config.FILENAME).is_file() else ask("Project directory", cwd, yes)
    else:
        d = a.dir
    root = Path(d).expanduser().resolve()
    if not root.is_dir():
        raise ConfigError(f"{root} is not a directory")
    if not (root / config.FILENAME).exists():
        if not confirm(f"No {config.FILENAME} in {root}. Generate one?", yes):
            print("nothing to do")
            return 1
        do_init(root, yes)
        if not confirm("Launch now?", yes):
            print(f"later: crew up {root}")
            return 0
    cfg = config.load(root)
    return launch.up(cfg, fresh=a.fresh, detached=a.detached, claude_args=a.claude_args or "", dry_run=a.dry_run)


def bring_up_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="crew", description="Bring the architect/worker workflow up on a project.")
    p.add_argument("dir", nargs="?", help="project directory (asked for when omitted)")
    p.add_argument("--yes", "-y", action="store_true", help="answer every question with its default")
    p.add_argument("--fresh", action="store_true", help="also archive worker threads")
    p.add_argument("--detached", action="store_true", help="do not attach to tmux")
    p.add_argument("--dry-run", action="store_true", help="print the pane commands and exit")
    p.add_argument("--claude-args", default="", help='extra flags for claude, e.g. "--model sonnet"')
    return p


# ----------------------------------------------------------------------------- the subcommands

def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="crew",
        description="Claude Code as the architect, Codex/Claude headless sessions as workers, one live dashboard.",
        epilog="`crew [DIR]` with no subcommand brings the workflow up, generating crew.toml if needed.")
    p.add_argument("--version", "-V", action="version", version=f"crew {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")
    proj = argparse.ArgumentParser(add_help=False)
    proj.add_argument("--project", help="project directory (default: $CREW_PROJECT, then the nearest crew.toml)")

    s = sub.add_parser("init", help="write crew.toml and .crew/ in a project")
    s.add_argument("dir", nargs="?", default=".")
    s.add_argument("--yes", "-y", action="store_true", help="accept every default")
    s.add_argument("--workers", type=worker_count, help="number of workers, 1 to 9 (default 1)")
    s.add_argument("--kind", choices=config.KINDS, help="worker kind (default codex)")
    s.add_argument("--model", help="architect model (default: your Claude Code default)")
    s.add_argument("--worker-model", help="model for every worker (default: each agent's own)")
    s.add_argument("--force", action="store_true", help="overwrite an existing crew.toml")

    s = sub.add_parser("up", help="start Claude Code and the dashboard in tmux")
    s.add_argument("dir", nargs="?")
    s.add_argument("--fresh", action="store_true", help="also archive worker threads so they start over")
    s.add_argument("--detached", action="store_true", help="do not attach; print the attach command")
    s.add_argument("--claude-args", default="", help='extra flags for claude, e.g. "--model sonnet"')
    s.add_argument("--dry-run", action="store_true", help="render files, print the pane commands, start nothing")

    s = sub.add_parser("run", parents=[proj], help="run one worker on one task")
    s.add_argument("name")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--task", help="the brief as text")
    g.add_argument("--task-file", help="a markdown brief")
    s.add_argument("--continue", dest="cont", action="store_true", help="resume this worker's thread")
    s.add_argument("--timeout", type=int, help="seconds before the worker is killed (default from crew.toml)")
    s.add_argument("--read-only", dest="readonly", action="store_true",
                   help="this task may read but not change files (codex: read-only sandbox; "
                        "claude: default permissions, Read Glob Grep only)")

    s = sub.add_parser("say", parents=[proj], help="the architect says something on the bus")
    s.add_argument("text")
    s.add_argument("--to", dest="dst")

    s = sub.add_parser("verify", parents=[proj], help="the architect's verdict on a worker")
    s.add_argument("name")
    s.add_argument("verdict", choices=["pass", "fail"])
    s.add_argument("text", nargs="?", default="")

    s = sub.add_parser("post", parents=[proj], help="append any event to the bus")
    s.add_argument("--from", dest="src", required=True)
    s.add_argument("--to", dest="dst")
    s.add_argument("--kind", required=True)
    s.add_argument("--text", default="")
    s.add_argument("--data", help="JSON object with extra fields")

    s = sub.add_parser("tail", parents=[proj], help="print the bus")
    s.add_argument("-n", type=int, default=30)
    s.add_argument("-f", action="store_true", help="keep following")

    sub.add_parser("status", parents=[proj], help="each worker: kind, model, thread, tasks, last result")

    s = sub.add_parser("dashboard", parents=[proj], help="the live dashboard")
    s.add_argument("--events", help="events file to follow (default <project>/.crew/events.jsonl)")
    s.add_argument("--layout", choices=config.LAYOUTS, help="wide or strip (default from crew.toml)")

    s = sub.add_parser("replay", parents=[proj], help="play a recorded events file")
    s.add_argument("file")
    s.add_argument("--speed", type=float, default=2.0)
    s.add_argument("--layout", choices=config.LAYOUTS, help="wide or strip (default from crew.toml)")

    s = sub.add_parser("snapshot", parents=[proj], help="render one dashboard frame to SVG")
    s.add_argument("out")
    src = s.add_mutually_exclusive_group()
    src.add_argument("--events")
    src.add_argument("--replay")
    s.add_argument("--focus")
    s.add_argument("--scroll", type=int, default=0)
    s.add_argument("--width", type=int, default=150)
    s.add_argument("--height", type=int, default=45)
    s.add_argument("--layout", choices=config.LAYOUTS, help="wide or strip (default from crew.toml)")

    s = sub.add_parser("hook", help="Claude Code hook entry (reads the payload on stdin)")
    s.add_argument("event", nargs="?", default="")

    s = sub.add_parser("attach", parents=[proj], help="make this tmux pane (an interactive Codex) a worker")
    s.add_argument("name")

    s = sub.add_parser("stop", help="close a project's tmux session")
    s.add_argument("dir", nargs="?")
    return p


def project_cfg(explicit: str | None) -> config.Config:
    return config.load(config.resolve_project(explicit))


def optional_cfg(explicit: str | None) -> config.Config | None:
    """The project config when one is named or found, else None (replays need none)."""
    try:
        root = config.resolve_project(explicit)
    except ConfigError:
        return None
    try:
        return config.load(root)
    except ConfigError:
        if explicit:
            raise
        return None


def dispatch(a: argparse.Namespace) -> int:
    c = a.cmd
    if c == "init":
        do_init(Path(a.dir), a.yes or not interactive(), a.workers, a.kind, a.model, a.worker_model, a.force)
        print(f"next: crew up {Path(a.dir).resolve()}")
        return 0
    if c in ("up", "stop"):
        from . import launch
        cfg = config.load(Path(a.dir).expanduser().resolve()) if a.dir else project_cfg(None)
        if c == "stop":
            return launch.stop(cfg)
        return launch.up(cfg, fresh=a.fresh, detached=a.detached, claude_args=a.claude_args, dry_run=a.dry_run)
    if c == "run":
        from . import worker
        cfg = project_cfg(a.project)
        task = Path(a.task_file).read_text() if a.task_file else a.task
        return worker.run(cfg, a.name, task, cont=a.cont, timeout=a.timeout, readonly=a.readonly)
    if c == "say":
        cfg = project_cfg(a.project)
        me = cfg.architect.name
        Bus(cfg.root).post(me, a.dst, "message", a.text)
        print(f"{me}{' → ' + a.dst if a.dst else ''}: {a.text}")
        return 0
    if c == "verify":
        cfg = project_cfg(a.project)
        cfg.worker(a.name)
        me = cfg.architect.name
        Bus(cfg.root).post(me, a.name, "verify", a.text, verdict=a.verdict)
        print(f"{me} → {a.name}: {a.verdict.upper()} {a.text}".rstrip())
        return 0
    if c == "post":
        cfg = project_cfg(a.project)
        try:
            data = json.loads(a.data) if a.data else {}
        except json.JSONDecodeError as e:
            raise ConfigError(f"--data is not valid JSON: {e}") from None
        if not isinstance(data, dict):
            raise ConfigError("--data must be a JSON object")
        print(json.dumps(Bus(cfg.root).post(a.src, a.dst, a.kind, a.text, **data), ensure_ascii=False))
        return 0
    if c == "tail":
        cfg = project_cfg(a.project)
        f = Follower(Bus(cfg.root).events)
        for ev in f.poll()[-a.n:] if a.n > 0 else []:
            print(fmt(ev))
        if a.f:
            try:
                while True:
                    for ev in f.poll():
                        print(fmt(ev), flush=True)
                    time.sleep(0.2)
            except KeyboardInterrupt:
                pass
        return 0
    if c == "status":
        from . import worker
        cfg = project_cfg(a.project)
        print(f"{cfg.name}  ({cfg.root})  architect {cfg.architect.name}")
        for line in worker.status_lines(cfg):
            print(line)
        return 0
    if c == "dashboard":
        from . import dashboard
        cfg = project_cfg(a.project) if not a.events else optional_cfg(a.project)
        if cfg:
            dashboard.configure(cfg)
        events = a.events or str(Bus(cfg.root).events)
        return dashboard.run(events=events, layout=a.layout)
    if c == "replay":
        from . import dashboard
        if not Path(a.file).is_file():
            raise ConfigError(f"no such file: {a.file}")
        if a.project:
            dashboard.configure(project_cfg(a.project))
        return dashboard.run(replay=a.file, speed=a.speed, layout=a.layout)
    if c == "snapshot":
        from . import dashboard
        if a.replay or a.events:
            path = a.replay or a.events
            cfg = project_cfg(a.project) if a.project else (optional_cfg(None) if a.events else None)
        else:
            cfg = project_cfg(a.project)
            path = str(Bus(cfg.root).events)
        if not Path(path).is_file():
            raise ConfigError(f"no such file: {path}")
        if cfg:
            dashboard.configure(cfg)
        dashboard.snapshot(path, a.out, a.width, a.height, a.focus, a.scroll, layout=a.layout)
        return 0
    if c == "attach":
        from . import attached
        return attached.attach(project_cfg(a.project), a.name)
    if c == "hook":
        from . import hook
        return hook.main([a.event] if a.event else [])
    raise ConfigError(f"unknown command {c}")


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["codex-notify"]:            # Codex's notify hook; not in --help, never fails
        from . import attached
        return attached.notify(argv[1:])
    try:
        if argv and (argv[0] in SUBCOMMANDS or argv[0] in ("-h", "--help", "-V", "--version")):
            return dispatch(parser().parse_args(argv))
        return bring_up(bring_up_parser().parse_args(argv))
    except ConfigError as e:
        print(f"crew: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0
