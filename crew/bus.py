"""The bus: one append-only JSONL file per project (<project>/.crew/events.jsonl).

Every agent writes to it, the dashboard reads it. Event shape (one JSON object per line):
  {"id": "3f2a9c1d", "ts": 1793660000.123, "src": "fable", "dst": "worker-1",
   "kind": "task", "text": "...", "data": {...}}

Kinds:
  connect    an agent came online             status     free-text state ("waiting for you")
  prompt     the human spoke to an agent      task       the architect hands a worker a brief
  message    an agent says something          exec       an agent starts a shell command
  exec_done  a shell command finished         edit       an agent wrote or edited a file
  read       an agent read a file             thinking   an agent is reasoning
  result     a worker returns its final answer
  verify     the architect's verdict on a result (data.verdict = pass | fail)
  usage      token usage for one turn         error      something went wrong
  note       anything else worth a line
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

from .config import ensure_state

MAX_TEXT = 1500    # longest text field stored (a whole task brief fits)
MAX_LINE = 8000    # keep each line short so a single append never interleaves with another writer
KINDS = ("connect", "prompt", "task", "message", "exec", "exec_done", "edit", "read", "thinking",
         "result", "verify", "usage", "error", "status", "note")


def clip(s: object, n: int = MAX_TEXT) -> str:
    s = "" if s is None else str(s)
    s = s.replace("\r", "")
    return s if len(s) <= n else s[: n - 1] + "…"


def encode(src: str, dst: str | None = None, kind: str = "message", text: str = "", /, **data) -> tuple[dict, str]:
    """Build one event and its JSON line, applying the size limits."""
    ev = {"id": uuid.uuid4().hex[:8], "ts": time.time(), "src": src, "dst": dst, "kind": kind, "text": clip(text)}
    clean = {k: (clip(v, 1500) if isinstance(v, str) else v) for k, v in data.items() if v is not None}
    if clean:
        ev["data"] = clean
    line = json.dumps(ev, ensure_ascii=False)
    if len(line) > MAX_LINE:
        ev["text"] = clip(ev["text"], 300)
        ev.pop("data", None)
        line = json.dumps(ev, ensure_ascii=False)
    return ev, line


def append_line(path: Path, line: str) -> None:
    """One os.write with O_APPEND: safe with many writers at once."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, (line + "\n").encode("utf-8"))
    finally:
        os.close(fd)


class Bus:
    """The bus of one project. `root` is the project directory; state lives in root/.crew."""

    def __init__(self, project: Path | str):
        self.project = Path(project).resolve()
        self.dir = self.project / ".crew"
        self.events = self.dir / "events.jsonl"
        self.archive = self.dir / "archive"
        self._ready = False

    def _ensure(self) -> None:
        """Before the first write: .crew/ with its self-ignore, whichever command writes first."""
        if not self._ready:
            ensure_state(self.project)
            self._ready = True

    def post(self, src: str, dst: str | None = None, kind: str = "message", text: str = "", /, **data) -> dict:
        """Append one event. Safe to call from many processes at once."""
        ev, line = encode(src, dst, kind, text, **data)
        self._ensure()
        append_line(self.events, line)
        return ev

    def reset(self) -> Path | None:
        """Archive the current log under .crew/archive and start an empty one."""
        self._ensure()
        if not self.events.exists() or self.events.stat().st_size == 0:
            self.events.touch(0o600)
            return None
        self.archive.mkdir(exist_ok=True)
        dest = unique(self.archive / time.strftime("events-%Y%m%d-%H%M%S.jsonl"))
        self.events.rename(dest)
        self.events.touch(0o600)
        return dest

    def read(self) -> list[dict]:
        return read_events(self.events)


def unique(path: Path) -> Path:
    """path, or path with -2, -3 … before the suffix if it already exists."""
    if not path.exists():
        return path
    i = 2
    while True:
        cand = path.with_name(f"{path.stem}-{i}{path.suffix}")
        if not cand.exists():
            return cand
        i += 1


class Follower:
    """Incremental reader. Survives partial trailing lines, bad lines, and a reset of the file."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.pos = 0
        self.buf = b""
        self.ino = None

    def poll(self) -> list[dict]:
        out: list[dict] = []
        try:
            st = self.path.stat()
        except FileNotFoundError:
            return out
        if st.st_size < self.pos or (self.ino is not None and st.st_ino != self.ino):   # reset underneath us
            self.pos, self.buf = 0, b""
        self.ino = st.st_ino
        if st.st_size == self.pos:
            return out
        with open(self.path, "rb") as fh:
            fh.seek(self.pos)
            chunk = fh.read()
            self.pos = fh.tell()
        lines = (self.buf + chunk).split(b"\n")
        self.buf = lines.pop()         # partial last line, or b""
        for raw in lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                ev = json.loads(raw.decode("utf-8"))
            except Exception:
                continue
            if isinstance(ev, dict):
                out.append(ev)
        return out


def read_events(path: Path | str) -> list[dict]:
    return Follower(path).poll()


def fmt(ev: dict) -> str:
    """One event as one line of text, for `crew tail`."""
    t = time.strftime("%H:%M:%S", time.localtime(ev.get("ts", 0)))
    who = str(ev.get("src")) + (f" → {ev['dst']}" if ev.get("dst") else "")
    d = ev.get("data") or {}
    extra = ""
    if ev.get("kind") == "exec_done" and "exit_code" in d:
        extra = f"  (exit {d['exit_code']})"
    if ev.get("kind") == "verify":
        extra = f"  [{str(d.get('verdict', '?')).upper()}]"
    text = str(ev.get("text", "")).replace("\n", " ⏎ ")
    return f"{t}  {str(ev.get('kind')):9} {who}: {text}{extra}"
