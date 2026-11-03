"""crew.toml: the agents that work on a project and how they are called.

load(project) reads and validates it; template(...) renders a new one; write() saves it.
Validation problems raise ConfigError, which the CLI prints as one line and exits 2.
"""
from __future__ import annotations

import os
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

FILENAME = "crew.toml"
STATE_DIR = ".crew"
KINDS = ("codex", "claude")
SANDBOXES = ("read-only", "workspace-write", "danger-full-access")
EFFORTS = ("minimal", "low", "medium", "high", "xhigh")
DEFAULT_ALLOWED = "Bash Edit Write MultiEdit Read Glob Grep"


class ConfigError(Exception):
    """A problem with crew.toml or the project directory. Printed as one line, exit 2."""


@dataclass
class Architect:
    name: str = "fable"
    model: str = ""
    notes: str = ""


@dataclass
class Worker:
    name: str
    kind: str = "codex"
    model: str = ""
    effort: str = "medium"
    sandbox: str = "workspace-write"
    permission_mode: str = "acceptEdits"
    allowed_tools: str = DEFAULT_ALLOWED
    cwd: str = "."
    timeout: int = 900

    def describe(self) -> str:
        """One human line, as the architect's protocol lists it."""
        if self.kind == "claude":
            return f"Claude Code ({self.model or 'default model'}), {self.permission_mode}, cwd {self.cwd}"
        return (f"Codex ({self.model or 'default model'}, effort {self.effort}), "
                f"sandbox {self.sandbox}, cwd {self.cwd}")


@dataclass
class Dashboard:
    split: int = 55
    raw: bool = False


@dataclass
class Config:
    root: Path
    name: str
    version: int = 1
    architect: Architect = field(default_factory=Architect)
    workers: list[Worker] = field(default_factory=list)
    dashboard: Dashboard = field(default_factory=Dashboard)

    @property
    def state(self) -> Path:
        return self.root / STATE_DIR

    def worker(self, name: str) -> Worker:
        for w in self.workers:
            if w.name == name:
                return w
        known = ", ".join(w.name for w in self.workers) or "none"
        raise ConfigError(f"no worker named {name!r} in {self.root / FILENAME} (known: {known})")

    def worker_cwd(self, w: Worker) -> Path:
        return (self.root / w.cwd).resolve()


# ----------------------------------------------------------------------------- finding the project

def find_project(start: Path | str | None = None) -> Path | None:
    """The nearest directory at or above start that contains crew.toml."""
    p = Path(start or os.getcwd()).resolve()
    for d in (p, *p.parents):
        if (d / FILENAME).is_file():
            return d
    return None


def resolve_project(explicit: str | None = None, env: str = "CREW_PROJECT") -> Path:
    """--project DIR, then $CREW_PROJECT, then the nearest ancestor with crew.toml."""
    if explicit:
        return Path(explicit).expanduser().resolve()
    if os.environ.get(env):
        return Path(os.environ[env]).expanduser().resolve()
    found = find_project()
    if not found:
        raise ConfigError(f"no {FILENAME} here or in any parent of {os.getcwd()}; run `crew init` or pass --project DIR")
    return found


# ----------------------------------------------------------------------------- loading

def _warn(msg: str) -> None:
    print(f"crew: warning: {msg}", file=sys.stderr)


NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")   # safe as a folder name and on the command line


def _table(raw: dict, key: str) -> dict:
    """A [section] that may be missing, but must be a table when present."""
    val = raw.get(key)
    if val is None:
        return {}
    if not isinstance(val, dict):
        raise ConfigError(f"{key} must be a table ([{key}]), got {val!r}")
    return val


def _check_name(name: str, what: str) -> None:
    if not NAME_RE.match(name) or ".." in name:
        raise ConfigError(f"{what} name {name!r} may only use letters, digits, '-', '_' and '.', "
                          "and must start with a letter or digit")


def _fill(cls, section: dict, where: str, **fixed):
    known = {f for f in cls.__dataclass_fields__}
    kw = dict(fixed)
    for key, val in section.items():
        if key not in known or key in fixed:
            _warn(f"unknown key {where}.{key} ignored")
            continue
        want = type(getattr(cls(**fixed) if fixed else cls(), key))
        if want is int and isinstance(val, bool) or not isinstance(val, want):
            raise ConfigError(f"{where}.{key} must be {want.__name__}, got {val!r}")
        kw[key] = val
    return cls(**kw)


def load(root: Path | str) -> Config:
    root = Path(root).expanduser().resolve()
    path = root / FILENAME
    if not path.is_file():
        raise ConfigError(f"no {FILENAME} in {root}")
    try:
        raw = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from None
    return from_dict(raw, root)


def from_dict(raw: dict, root: Path) -> Config:
    for key in raw:
        if key not in ("name", "version", "architect", "workers", "dashboard"):
            _warn(f"unknown key {key!r} ignored")
    name = raw.get("name") or root.name
    if not isinstance(name, str):
        raise ConfigError(f"name must be a string, got {name!r}")
    version = raw.get("version", 1)
    if isinstance(version, bool) or not isinstance(version, int):
        raise ConfigError(f"version must be an integer, got {version!r}")
    arch = _fill(Architect, _table(raw, "architect"), "architect")
    _check_name(arch.name, "architect")
    dash = _fill(Dashboard, _table(raw, "dashboard"), "dashboard")
    if not 10 <= dash.split <= 90:
        raise ConfigError(f"dashboard.split must be between 10 and 90, got {dash.split}")
    workers: list[Worker] = []
    seen: set[str] = set()
    raw_workers = raw.get("workers") or []
    if not isinstance(raw_workers, list):
        raise ConfigError("workers must be an array of tables: [[workers]]")
    for i, sec in enumerate(raw_workers, 1):
        if not isinstance(sec, dict):
            raise ConfigError(f"workers #{i} must be a table ([[workers]]), got {sec!r}")
        wname = sec.get("name")
        if not wname or not isinstance(wname, str):
            raise ConfigError(f"workers #{i} has no name")
        _check_name(wname, "worker")
        if wname in seen:
            raise ConfigError(f"duplicate worker name {wname!r}")
        if wname == arch.name or wname == "user":
            raise ConfigError(f"worker name {wname!r} is taken by the architect or the user")
        seen.add(wname)
        rest = {k: v for k, v in sec.items() if k != "name"}
        w = _fill(Worker, rest, f"workers.{wname}", name=wname)
        if w.kind not in KINDS:
            raise ConfigError(f"worker {wname!r}: unknown kind {w.kind!r} (use codex or claude)")
        if w.kind == "codex" and w.sandbox not in SANDBOXES:
            raise ConfigError(f"worker {wname!r}: unknown sandbox {w.sandbox!r} ({', '.join(SANDBOXES)})")
        if w.kind == "codex" and w.effort not in EFFORTS:
            raise ConfigError(f"worker {wname!r}: unknown effort {w.effort!r} ({', '.join(EFFORTS)})")
        if w.timeout <= 0:
            raise ConfigError(f"worker {wname!r}: timeout must be positive")
        if not (root / w.cwd).is_dir():
            raise ConfigError(f"worker {wname!r}: cwd {w.cwd!r} does not exist under {root}")
        workers.append(w)
    return Config(root=root, name=name, version=version, architect=arch,
                  workers=workers, dashboard=dash)


# ----------------------------------------------------------------------------- writing

def _q(s: str) -> str:
    """A TOML basic string."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _kv(key: str, value: str, comment: str = "") -> str:
    line = f"{key} = {value}"
    return f"{line:<28} # {comment}" if comment else line


def template(name: str, architect_model: str = "", workers: int = 2, kind: str = "codex",
             effort: str = "medium", model: str = "", architect_name: str = "fable") -> str:
    out = [
        "# crew.toml — the agents that work on this project and how they are called.",
        "# `crew up` re-reads it every time; edit freely.",
        _kv("name", _q(name), "shown on the dashboard"),
        "version = 1",
        "",
        "[architect]",
        _kv("name", _q(architect_name), "its name on the bus and the dashboard"),
        _kv("model", _q(architect_model), '`claude --model`; "" = your Claude Code default'),
        "# notes are appended verbatim to the architect's protocol, e.g. how to run the tests",
        'notes = """',
        '"""',
    ]
    for i in range(1, workers + 1):
        out += ["", "[[workers]]", _kv("name", f'"worker-{i}"', "unique; used as `crew run worker-%d`" % i),
                _kv("kind", _q(kind), "codex | claude"),
                _kv("model", _q(model), "\"\" = the agent's own default")]
        if kind == "codex":
            out += [_kv("effort", _q(effort), "minimal | low | medium | high | xhigh"),
                    _kv("sandbox", '"workspace-write"', "read-only | workspace-write | danger-full-access")]
        else:
            out += [_kv("permission_mode", '"acceptEdits"', "claude --permission-mode"),
                    _kv("allowed_tools", _q(DEFAULT_ALLOWED), "claude --allowedTools")]
        out += [_kv("cwd", '"."', "relative to the project root"), _kv("timeout", "900", "seconds")]
    out += ["", "[dashboard]", _kv("split", "55", "percent of the width given to the dashboard pane"),
            _kv("raw", "false", "add a pane tailing the newest raw worker log"), ""]
    return "\n".join(out)


def ensure_state(root: Path | str) -> Path:
    """Create .crew/ with a .gitignore that ignores everything in it."""
    d = Path(root) / STATE_DIR
    d.mkdir(parents=True, exist_ok=True)
    gi = d / ".gitignore"
    if not gi.exists():
        gi.write_text("*\n")
    return d


def write(root: Path | str, text: str) -> Path:
    path = Path(root) / FILENAME
    tomllib.loads(text)                     # never write a file we cannot read back
    path.write_text(text)
    ensure_state(root)
    return path
