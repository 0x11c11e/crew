"""Small helpers shared by the worker runner, the adapters and the dashboard."""
from __future__ import annotations

import re
import shlex
from pathlib import Path


def short(t: str | None) -> str:
    """Thread and session ids are long; their tail is enough to tell them apart."""
    return "…" + t[-6:] if t else ""


def tail(s: str | None, n: int = 1200) -> str:
    s = (s or "").rstrip()
    return s if len(s) <= n else "…" + s[-(n - 1):]


def rel(path: str, base: Path | str | None) -> str:
    """Path relative to base when it lies inside it, else unchanged."""
    if not path or not base:
        return path or ""
    try:
        return str(Path(path).resolve().relative_to(Path(base).resolve()))
    except Exception:
        return path


def pretty_cmd(cmd: str) -> str:
    """Turn "/bin/bash -lc 'printf hi > f'" into "printf hi > f" for the feed."""
    m = re.match(r"^(?:/bin/|/usr/bin/)?(?:ba|z)?sh\s+-l?c\s+(.*)$", (cmd or "").strip(), re.S)
    if m:
        inner = m.group(1).strip()
        try:
            parts = shlex.split(inner)
            if len(parts) == 1:
                inner = parts[0]
        except ValueError:
            pass
        cmd = inner
    return (cmd or "").strip().replace("\n", " ; ").strip().rstrip(";").strip()
