"""Claude worker: Claude Code headless, `claude -p --output-format stream-json --verbose`.

Newer than the Codex adapter: the mapping is tested against a real tool-using recording
(tests/fixtures/claude-stream.jsonl); a live run and a resumed session were verified with a trivial task.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..util import pretty_cmd, rel, short, tail
from . import Ctx, Out

LABEL = "Claude"
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


def build_command(worker, task: str, resume_id: str | None, result_path: Path | str | None = None,
                  cwd: Path | str | None = None) -> list[str]:
    # The task stays right after -p: --allowedTools and --disallowedTools take a variable number of values.
    cmd = ["claude", "-p", task, "--output-format", "stream-json", "--verbose",
           "--permission-mode", worker.permission_mode]
    if worker.allowed_tools.strip():
        cmd += ["--allowedTools", worker.allowed_tools.strip()]
    if worker.disallowed_tools.strip():
        cmd += ["--disallowedTools", worker.disallowed_tools.strip()]
    if worker.model:
        cmd += ["--model", worker.model]
    if resume_id:
        cmd += ["--resume", resume_id]
    return cmd


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text", "")) for b in content if isinstance(b, dict) and b.get("type") == "text")
    return "" if content is None else str(content)


def map_event(line: str, ctx: Ctx) -> list[Out]:
    try:
        ev = json.loads(line)
    except Exception:
        return []
    if not isinstance(ev, dict):
        return []
    t = ev.get("type")
    arch = ctx.architect
    if t == "system":
        if ev.get("subtype") == "init":
            ctx.thread_id = ev.get("session_id") or ctx.thread_id
            word = "resumed" if ctx.resume else "new"
            return [Out("status", f"{word} session {short(ctx.thread_id)}",
                        data={"thread_id": ctx.thread_id, "model": ev.get("model")})]
        return []                       # hook_started, hook_response, …
    if t == "assistant":
        out: list[Out] = []
        for b in (ev.get("message") or {}).get("content") or []:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text":
                txt = (b.get("text") or "").strip()
                if txt:
                    ctx.last_message = txt
                    out.append(Out("message", txt, dst=arch))
            elif bt == "thinking":
                txt = (b.get("thinking") or "").strip()
                if txt:
                    out.append(Out("thinking", txt))
            elif bt == "tool_use":
                name = b.get("name") or "?"
                ti = b.get("input") or {}
                if name == "Bash":
                    cmd = pretty_cmd(ti.get("command") or "")
                    ctx.tools[b.get("id")] = cmd
                    ctx.commands += 1
                    out.append(Out("exec", cmd, data={"item": b.get("id"), "desc": ti.get("description")}))
                elif name in EDIT_TOOLS:
                    out.append(Out("edit", rel(ti.get("file_path") or ti.get("notebook_path") or "", ctx.cwd)))
                elif name == "Read":
                    out.append(Out("read", rel(ti.get("file_path") or "", ctx.cwd)))
                else:
                    out.append(Out("status", f"uses {name}"))
        return out
    if t == "user":
        out = []
        content = (ev.get("message") or {}).get("content")
        for b in content if isinstance(content, list) else []:
            if not isinstance(b, dict) or b.get("type") != "tool_result":
                continue
            cmd = ctx.tools.pop(b.get("tool_use_id"), None)
            if cmd is None:
                continue                # only Bash results become exec_done
            rc = 1 if b.get("is_error") else 0
            out.append(Out("exec_done", cmd, data={"item": b.get("tool_use_id"), "exit_code": rc,
                                                   "status": "failed" if rc else "completed",
                                                   "output": tail(_text_of(b.get("content")))}))
        return out
    if t == "result":
        ctx.thread_id = ev.get("session_id") or ctx.thread_id
        ctx.final = ev.get("result") if isinstance(ev.get("result"), str) else ctx.last_message
        ctx.failed = ev.get("subtype") != "success" or bool(ev.get("is_error"))
        ctx.cost = ev.get("total_cost_usd")
        ctx.turns = ev.get("num_turns")
        u = ev.get("usage") or {}
        ctx.tokens += int(u.get("input_tokens") or 0) + int(u.get("output_tokens") or 0)
        return [Out("usage", "", data={k: u.get(k) for k in
                    ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")})]
    return []                           # rate_limit_event and anything new


def final_message(ctx: Ctx, result_path: Path | str | None = None) -> str:
    return (ctx.final if ctx.final is not None else ctx.last_message or "").strip()


def describe(worker) -> str:
    return f"Claude worker (claude -p, {worker.model or 'default model'}, {worker.permission_mode})"
