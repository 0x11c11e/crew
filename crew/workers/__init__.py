"""Worker adapters. Each one turns a task into a command line and the agent's JSON event stream
into bus events, one line at a time, with no I/O of its own (so it is testable from fixtures).

  build_command(worker, task, resume_id, result_path, cwd) -> list[str]
  map_event(line, ctx) -> list[Out]          ctx: Ctx, mutated to remember ids and totals
  final_message(ctx, result_path) -> str
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Out:
    """One bus event to post as the worker (src is always the worker)."""
    kind: str
    text: str = ""
    dst: str | None = None
    data: dict = field(default_factory=dict)


@dataclass
class Ctx:
    """What an adapter remembers across the lines of one run."""
    name: str = "worker"                 # the worker's name on the bus
    architect: str = "fable"             # who dispatched it; messages and results go there
    cwd: str = "."                       # for relative paths in edit/read events
    resume: bool = False
    thread_id: str | None = None         # Codex thread id or Claude session id
    tools: dict = field(default_factory=dict)     # tool_use id -> command (Claude)
    tokens: int = 0
    commands: int = 0
    last_message: str = ""
    final: str | None = None             # set by an adapter that reports the final answer in-stream
    failed: bool = False                 # the agent reported failure in-stream
    cost: float | None = None
    turns: int | None = None


from . import claude, codex  # noqa: E402

ADAPTERS = {"codex": codex, "claude": claude}
