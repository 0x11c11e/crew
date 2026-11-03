"""Shared test helpers: temp projects and fixture paths."""
from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures"
SAMPLE = REPO / "samples" / "todo-demo.jsonl"
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


class TempProject:
    """A temporary directory, optionally with a crew.toml; chdir into it while active."""

    def __init__(self, toml: str | None = None):
        self.toml = toml

    def __enter__(self) -> Path:
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name).resolve()
        if self.toml is not None:
            (self.root / "crew.toml").write_text(self.toml)
        self._cwd = os.getcwd()
        self._env = os.environ.pop("CREW_PROJECT", None)
        os.chdir(self.root)
        return self.root

    def __exit__(self, *exc):
        os.chdir(self._cwd)
        if self._env is not None:
            os.environ["CREW_PROJECT"] = self._env
        self._td.cleanup()


def quiet(fn, *args, **kw):
    """Call fn with stdout and stderr captured; return (result, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        r = fn(*args, **kw)
    return r, out.getvalue(), err.getvalue()
