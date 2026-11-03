"""Codex worker: OpenAI's headless CLI, `codex exec --json`."""
from __future__ import annotations

import json
from pathlib import Path

from ..util import pretty_cmd, rel, short, tail
from . import Ctx, Out

LABEL = "Codex"
NOISE = ("unrecognized configuration settings",)


def build_command(worker, task: str, resume_id: str | None, result_path: Path | str,
                  cwd: Path | str | None = None) -> list[str]:
    cmd = ["codex", "exec", "--skip-git-repo-check", "-s", worker.sandbox,
           "-c", f'model_reasoning_effort="{worker.effort}"', "-C", str(cwd or worker.cwd),
           "--json", "-o", str(result_path)]
    if worker.model:
        cmd += ["-m", worker.model]
    # Global flags must come before the `resume` subcommand.
    cmd += ["resume", resume_id, task] if resume_id else [task]
    return cmd


def map_event(line: str, ctx: Ctx) -> list[Out]:
    try:
        ev = json.loads(line)
    except Exception:
        return []                      # stderr is merged in; plain text lines are not events
    if not isinstance(ev, dict):
        return []
    t = ev.get("type")
    item = ev.get("item") or {}
    it = item.get("type")
    arch = ctx.architect
    if t == "thread.started":
        ctx.thread_id = ev.get("thread_id") or ctx.thread_id
        return [Out("status", f"{'resumed' if ctx.resume else 'new'} thread {short(ctx.thread_id)}",
                    data={"thread_id": ctx.thread_id})]
    if t == "turn.started":
        return [Out("status", "working")]
    if t == "item.started" and it == "command_execution":
        ctx.commands += 1
        return [Out("exec", pretty_cmd(item.get("command", "")), data={"item": item.get("id")})]
    if t == "item.completed":
        if it == "agent_message":
            ctx.last_message = item.get("text", "")
            return [Out("message", ctx.last_message, dst=arch)]
        if it == "command_execution":
            return [Out("exec_done", pretty_cmd(item.get("command", "")),
                        data={"item": item.get("id"), "exit_code": item.get("exit_code"),
                              "status": item.get("status"), "output": tail(item.get("aggregated_output"))})]
        if it == "error":
            msg = item.get("message", "")
            return [] if any(k in msg for k in NOISE) else [Out("error", msg, dst=arch)]
        if it == "reasoning":
            txt = item.get("text") or item.get("summary") or ""
            return [Out("thinking", txt)] if txt else []
        if it == "file_change":
            changes = item.get("changes") or []
            if isinstance(changes, list):
                paths = ", ".join(rel(str(c.get("path", c)) if isinstance(c, dict) else str(c), ctx.cwd)
                                  for c in changes[:6])
            else:
                paths = str(changes)
            return [Out("edit", paths or "files changed", data={"status": item.get("status")})]
        return [Out("note", f"{it}: {json.dumps(item)[:200]}")]
    if t == "turn.completed":
        u = ev.get("usage") or {}
        ctx.tokens += int(u.get("input_tokens") or 0) + int(u.get("output_tokens") or 0)
        return [Out("usage", "", data={k: u.get(k) for k in
                    ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")})]
    if t in ("turn.failed", "error"):
        err = ev.get("error")
        msg = ev.get("message") or (err.get("message") if isinstance(err, dict) else None) or json.dumps(ev)[:300]
        if t == "turn.failed":
            ctx.failed = True
        return [Out("error", msg, dst=arch)]
    return []


def final_message(ctx: Ctx, result_path: Path | str) -> str:
    """Codex writes its last message to the -o file; fall back to the last message in the stream."""
    p = Path(result_path)
    try:
        text = p.read_text().strip() if p.exists() else ""
    except Exception:
        text = ""
    return text or (ctx.last_message or "").strip()


def describe(worker) -> str:
    return f"Codex worker (codex exec, effort {worker.effort}, sandbox {worker.sandbox})"
