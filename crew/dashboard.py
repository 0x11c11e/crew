"""Live terminal dashboard for the crew bus.

  crew dashboard [--project DIR]               follow <project>/.crew/events.jsonl live
  crew replay samples/todo-demo.jsonl --speed 2
  crew snapshot frame.svg [--events FILE | --replay FILE] [--focus NAME] [--width 150] [--height 45]

Roles and colors come from crew.toml when there is one; without it (a replay of a recording)
the default names apply: user, fable (the architect), worker-*.
"""
from __future__ import annotations

import html
import io
import math
import textwrap
import os
import re
import select
import sys
import termios
import time
import tty

from rich import box
from rich.console import Console, Group
from rich.cells import cell_len
from rich.layout import Layout
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.segment import Segment
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from .bus import Follower

ACCENT = "#C084FC"
PALETTE = {
    "user": "#F8FAFC",
    "fable": "#C084FC",      # the architect, Claude Code
    "worker-1": "#34D399",
    "worker-2": "#38BDF8",
    "worker-3": "#FBBF24",
    "worker-4": "#FB923C",
}
EXTRA = ["#F472B6", "#A3E635", "#22D3EE", "#F87171", "#E879F9"]
ROLES = {"user": "you", "fable": "architect · Claude Code"}
KIND = {"fable": "Claude Code"}  # what runs each agent: Claude Code, Codex, Claude
MODELS: dict[str, str] = {}      # the model each agent is configured with ("" = the agent's default)
USER = "user"                    # the human: appears in the feed, never as a card
ARCHITECT = "fable"             # who receives prompts and results when an event names no one
PROJECT = ""                    # shown in the header badge
LAYOUT = "wide"                 # wide: cards and the whole conversation; strip: a narrow status column
WORKER_COLORS = ["#34D399", "#38BDF8", "#FBBF24", "#FB923C"]


def configure(cfg) -> None:
    """Take the agents' names, roles and colors from a loaded crew.toml (crew.config.Config)."""
    global ARCHITECT, PROJECT, LAYOUT
    ARCHITECT = cfg.architect.name
    PROJECT = cfg.name
    LAYOUT = cfg.layout
    PALETTE.clear()
    ROLES.clear()
    KIND.clear()
    MODELS.clear()
    PALETTE.update({"user": "#F8FAFC", ARCHITECT: ACCENT})
    ROLES.update({"user": "you",
                  ARCHITECT: "architect · Claude Code" + (f" · {cfg.architect.model}" if cfg.architect.model else "")})
    KIND[ARCHITECT] = "Claude Code"
    MODELS[ARCHITECT] = cfg.architect.model
    colors = WORKER_COLORS + EXTRA
    for i, w in enumerate(cfg.workers):
        PALETTE[w.name] = colors[i % len(colors)]
        KIND[w.name] = "Claude" if w.kind == "claude" else "Codex"
        MODELS[w.name] = w.model
        ROLES[w.name] = f"worker · {KIND[w.name]}" + (f" · {w.model}" if w.model else "")
OK, BAD, DIM = "#34D399", "#F87171", "grey58"
WAIT = "#FBBF24"                 # yellow: someone is waiting on you, or a worker has nothing to do
FAINT = "grey42"                 # the least important lines: thinking, command output
CODE = "#22D3EE"                 # inline `code`: a color, no background block
THEME = Theme({"markdown.code": f"bold {CODE}"})

# Each kind of boxed event has its own accent, so a task, a result and a verdict differ at a glance.
ACCENTS = {"prompt": "#F8FAFC", "task": "#38BDF8", "result": WAIT, "message": ACCENT, "error": BAD}
# A box with a thick left edge in the accent color, open on the right.
EDGE = box.Box("┎── \n┃   \n┃   \n┃   \n┃   \n┃   \n┃   \n┖── \n")

ANIMATED = {"thinking", "busy", "running", "writing", "accepted", "waiting"}
WORDS = {"accepted": "starting", "busy": "working", "writing": "editing"}   # the header's one-word states


def dot_color(a) -> str:
    """The card dot. green: running a command or writing; violet: thinking; yellow: the architect is waiting
    on you, or a worker is idle; red: failed; grey: offline. An architect waiting on its workers has work
    in flight, so it is green too."""
    if a.state == "offline":
        return DIM
    if a.state == "failed":
        return BAD
    if a.state == "thinking":
        return ACCENT
    if a.state in ("idle", "done"):
        return WAIT
    return OK


def state_word(a) -> str:
    if a.state == "idle" and a.name == ARCHITECT:
        return "waiting for you"
    if a.state == "waiting":
        return "waiting on " + (a.waiting[0] if len(a.waiting) == 1 else f"{len(a.waiting)} workers")
    return WORDS.get(a.state, a.state)

HEADER_H, CARD_H = 4, 5          # rows: the header panel (2 lines), and one agent card (border, 3 lines, border)
CARD_TOP = HEADER_H + 2          # 1-based row of the first card's top border (header, then the agents panel border)
HINT = "1-9 / click: focus an agent  ·  0 / Esc: everyone  ·  Tab: next  ·  ↑↓ wheel: scroll  ·  End: newest  ·  q: quit"
GUIDE = [
    ("How to read this screen", "bold"),
    ("", ""),
    ("Left, the agents: one card each, live state and elapsed time, tasks done, tokens spent.", ""),
    ("Here, the conversation: your prompts, the architect's plan, each task it hands out,", ""),
    ("every command a worker runs with its exit code, each result, and the verdict.", ""),
    ("", ""),
    ("Nothing yet. Ask Claude Code in the left pane for something; this fills as it works.", "italic"),
]


def agents_width(w: int) -> int:
    return min(50, max(36, w * 38 // 100))


def color(name: str | None) -> str:
    if not name:
        return DIM
    return PALETTE.get(name) or EXTRA[sum(map(ord, name)) % len(EXTRA)]


def role(name: str) -> str:
    return ROLES.get(name, "worker · Codex")


def kind_of(name: str) -> str:
    return KIND.get(name) or ("Claude Code" if name == ARCHITECT else "Codex")


def who(name: str | None) -> str:
    return "you" if name == USER else (name or "all")


def short(t) -> str:
    return "…" + (t or "")[-6:] if t else ""


def hms(ts) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts)) if ts else ""


def ago(s: float) -> str:
    s = max(0, int(s))
    if s < 90:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{s % 3600 // 60:02d}m"


def k(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1e6:.1f}M"
    if n >= 10_000:
        return f"{n // 1000}k"
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def inline(text: str, style: str = "") -> Text:
    """One line of text with its `code` spans in the code color and the backticks dropped."""
    t = Text(no_wrap=True, overflow="ellipsis")
    for i, part in enumerate((text or "").split("`")):
        t.append(part, style=f"{style} {CODE}".strip() if i % 2 else style)
    return t


def first_line(text: str, n: int = 90) -> str:
    line = next((ln.strip() for ln in (text or "").splitlines() if ln.strip()), "")
    line = line.lstrip("#").strip()
    return line if len(line) <= n else line[: n - 1] + "…"


class Agent:
    def __init__(self, name: str):
        self.name = name
        self.state = "offline"
        self.detail = ""
        self.since = time.time()
        self.thread = None
        self.tasks = 0
        self.tokens = 0
        self.cost = 0.0
        self.verdict = None
        self.model = MODELS.get(name, "")
        self.kind = kind_of(name)
        self.task = ""                      # the task it is on (or finished last): its heading or first line
        self.task_n = None
        self.said = ""                      # first line of the last thing it said
        self.waiting: list[str] = []        # the architect: workers it has dispatched and not yet verified
        self.sent = 0                       # the architect: tasks dispatched
        self.verdicts = 0                   # the architect: verdicts posted
        self.num = 0                        # hotkey, assigned in order of first appearance
        self.spinner = Spinner("dots", style=color(name))

    def set(self, state: str, detail: str, now: float) -> None:
        if state != self.state or detail != self.detail:
            self.since = now
        self.state, self.detail = state, detail


class Model:
    def __init__(self):
        self.agents: dict[str, Agent] = {}
        self.feed: list[dict] = []
        self.count = 0
        self.prompts = 0
        self.tasks = 0
        self.results = 0
        self.passes = 0
        self.fails = 0
        self.tokens = 0
        self.cost = 0.0
        self.t_first = None
        self.t_last = None
        self.focus: str | None = None       # an agent name, or None for everyone
        self.scroll = 0                     # how many of the newest feed items are hidden (scrolled back)
        self.heights: dict = {}             # (event, width, focus) -> rendered rows, so each item is measured once
        self.ledger: dict[tuple, dict] = {}  # (worker, n) -> one row of the TASKS panel, oldest first
        self.current: dict[str, tuple] = {}  # worker -> the ledger key of its latest task

    def agent(self, name: str) -> Agent:
        if name not in self.agents:
            a = Agent(name)
            a.num = len(self.agents) + 1
            self.agents[name] = a
        return self.agents[name]

    # -- selection and scrolling
    def order(self) -> list[Agent]:
        return list(self.agents.values())   # cards keep the order agents first appeared in, so numbers never move

    def set_focus(self, name: str | None) -> None:
        self.focus = name if name in self.agents else None
        self.scroll = 0

    def toggle(self, name: str) -> None:
        self.set_focus(None if name == self.focus else name)

    def focus_num(self, n: int) -> None:
        a = next((a for a in self.agents.values() if a.num == n), None)
        if n == 0:
            self.set_focus(None)
        elif a:
            self.toggle(a.name)

    def cycle(self, step: int = 1) -> None:
        seq = [None] + [a.name for a in self.order()]
        i = seq.index(self.focus) if self.focus in seq else 0
        self.set_focus(seq[(i + step) % len(seq)])

    def click(self, x: int, y: int, agents_w: int) -> None:
        """A click on an agent card (1-based terminal coordinates) toggles focus on that agent."""
        if x > agents_w or y < CARD_TOP:
            return
        i = (y - CARD_TOP) // CARD_H
        order = self.order()
        if i < len(order):
            self.toggle(order[i].name)

    def visible(self) -> list[dict]:
        if not self.focus:
            return self.feed
        return [ev for ev in self.feed if ev.get("src") == self.focus or ev.get("dst") == self.focus]

    def scroll_by(self, n: int) -> None:
        self.scroll = max(0, min(max(0, len(self.visible()) - 1), self.scroll + n))

    def collapse_exec(self, done: dict) -> None:
        """A finished command replaces its own 'started' row, so each command is one line in the feed."""
        src, text = done.get("src"), done.get("text")
        item = (done.get("data") or {}).get("item")
        for i in range(len(self.feed) - 1, max(-1, len(self.feed) - 60), -1):
            ev = self.feed[i]
            if ev.get("kind") == "exec" and ev.get("src") == src and \
                    ((item and (ev.get("data") or {}).get("item") == item) or ev.get("text") == text):
                del self.feed[i]
                return

    def track(self, kind: str, src: str, dst: str | None, d: dict, text: str) -> None:
        """Keep the ledger: queued when dispatched, running once the worker speaks, done or failed with its
        result, then pass or fail with the verdict."""
        if kind == "task":
            key = (dst or "?", d.get("n") or sum(1 for w, _ in self.ledger if w == dst) + 1)
            heading, _ = split_heading(text)
            self.ledger.pop(key, None)                  # a re-sent task moves to the bottom
            self.ledger[key] = {"n": key[1], "worker": key[0], "title": heading or first_line(text),
                                "state": "queued", "seconds": None, "tokens": None}
            self.current[key[0]] = key
            return
        if kind == "verify":
            row = self.ledger.get(self.current.get(dst or "?"))
            if row and d.get("verdict") in ("pass", "fail"):
                row["state"] = d["verdict"]
            return
        row = self.ledger.get(self.current.get(src))
        if row is None:
            return
        if kind == "result":
            row.update(state="done" if d.get("exit_code", 0) == 0 else "fail", seconds=d.get("seconds"),
                       tokens=d.get("tokens"))
        elif kind == "status" and text.lower().startswith(("failed", "stopped")) and row["state"] == "running":
            row["state"] = "fail"
        elif row["state"] == "queued" and kind in ("status", "exec", "exec_done", "edit", "read", "message",
                                                   "thinking"):
            row["state"] = "running"

    def apply(self, ev: dict) -> None:
        kind = ev.get("kind")
        src = ev.get("src") or "?"
        dst = ev.get("dst")
        d = ev.get("data") or {}
        ts = ev.get("ts") or time.time()
        text = ev.get("text") or ""
        self.count += 1
        self.t_first = self.t_first or ts
        self.t_last = ts
        self.track(kind, src, dst, d, text)
        # The human gets no card: anything they post is a prompt in the feed, not an agent state.
        a = self.agent(src) if src != USER else Agent(src)
        if a.state == "offline":
            a.set("idle", "online", ts)
        if kind == "connect":
            if d.get("model"):
                a.model = d["model"]
            a.set("idle", "online", ts)
        elif kind == "prompt":
            self.prompts += 1
            self.agent(dst or ARCHITECT).set("thinking", "reading your request", ts)
        elif kind == "task":
            self.tasks += 1
            w = self.agent(dst or "?")
            heading, _ = split_heading(text)
            w.task, w.task_n = heading or first_line(text), d.get("n")
            if d.get("model"):
                w.model = d["model"]
            if d.get("kind"):
                w.kind = "Claude" if d["kind"] == "claude" else "Codex"
            w.set("accepted", f"task {d.get('n', '')} received".replace("  ", " ")
                  + (" · read-only" if d.get("read_only") else ""), ts)
            w.verdict = None
            a.sent += 1
            a.waiting = [x for x in a.waiting if x != dst] + [dst or "?"]
            a.set("waiting", "waiting on " + ", ".join(a.waiting), ts)
        elif kind == "status":
            if d.get("thread_id"):
                a.thread = d["thread_id"]
            if d.get("model"):
                a.model = d["model"]
            low = text.lower()
            if low.startswith(("idle", "waiting", "online")):
                state = "idle"
            elif low.startswith(("failed", "stopped")):
                state = "failed"
            elif low.startswith("woken"):
                state = "thinking"
            else:
                state = "busy"
            a.set(state, text, ts)
            # "starting codex…", "new thread", "working": one line, the latest, not three.
            if self.feed and self.feed[-1].get("kind") == "status" and self.feed[-1].get("src") == src:
                self.feed.pop()
        elif kind == "exec":
            a.set("running", text, ts)
        elif kind == "exec_done":
            rc = d.get("exit_code")
            a.set("busy", "command finished" + (f" (exit {rc})" if rc not in (None, 0) else ""), ts)
            self.collapse_exec(ev)
        elif kind == "edit":
            a.set("writing", f"editing {text}", ts)
        elif kind == "read":
            a.set("busy", f"reading {text}", ts)
        elif kind == "message":
            a.set("busy", "replied", ts)
            a.said = first_line(text)
        elif kind == "thinking":
            a.set("thinking", "thinking", ts)
        elif kind == "usage":
            n = int(d.get("input_tokens") or 0) + int(d.get("output_tokens") or 0)
            a.tokens += n
            self.tokens += n
        elif kind == "result":
            rc = d.get("exit_code", 0)
            a.tasks += 1
            self.results += 1
            if d.get("cost_usd"):
                a.cost += float(d["cost_usd"])
                self.cost += float(d["cost_usd"])
            # Codex's final message arrives first as a plain message, then again as the result: keep one.
            if self.feed and self.feed[-1].get("kind") == "message" and self.feed[-1].get("src") == src \
                    and (self.feed[-1].get("text") or "").strip() == text.strip():
                self.feed.pop()
            if d.get("thread_id"):
                a.thread = d["thread_id"]
            a.said = first_line(text)
            a.set("done" if rc == 0 else "failed",
                  f"task {d.get('n', a.tasks)} {'done' if rc == 0 else 'failed'} in {ago(d.get('seconds') or 0)}", ts)
            arch = self.agent(dst or ARCHITECT)
            arch.waiting = [x for x in arch.waiting if x != src]
            arch.set("thinking", f"verifying {src}", ts)
        elif kind == "verify":
            w = self.agent(dst or "?")
            w.verdict = d.get("verdict")
            if w.verdict == "pass":
                self.passes += 1
            elif w.verdict == "fail":
                self.fails += 1
            a.verdicts += 1
            a.waiting = [x for x in a.waiting if x != dst]
            a.set("idle", f"{d.get('verdict', '?')} for {dst}", ts)
        elif kind == "error":
            a.set("failed", first_line(text, 60), ts)
        if kind != "usage":
            self.feed.append(ev)


# ----------------------------------------------------------------------------- rendering

def totals(m: Model) -> Text:
    """The header's second half: tasks 3 · done 3 · pass 3 · fail 0 · 278k tokens, and the cost when known."""
    t = Text(no_wrap=True, overflow="ellipsis")
    parts = [("tasks", m.tasks, ""), ("done", m.results, ""), ("pass", m.passes, OK if m.passes else ""),
             ("fail", m.fails, BAD if m.fails else "")]
    for i, (label, n, style) in enumerate(parts):
        if i:
            t.append("  ·  ", style=DIM)
        t.append(label + " ", style=style or DIM)
        t.append(str(n), style=f"bold {style}".strip())
    t.append("  ·  ", style=DIM)
    t.append(k(m.tokens), style="bold")
    t.append(" tokens", style=DIM)
    if m.cost:
        t.append("  ·  ", style=DIM)
        t.append(f"${m.cost:.2f}", style="bold")
    return t


def chips(m: Model, words: bool = True) -> Text:
    """One chip per agent: ● name state, the dot in its state color. Narrow: ● name, the dot alone."""
    t = Text(no_wrap=True, overflow="ellipsis")
    for i, a in enumerate(m.order()):
        if i:
            t.append("    ")
        t.append("● ", style=dot_color(a))
        t.append(a.name, style=f"bold {color(a.name)}")
        if words:
            t.append(" " + state_word(a), style="")
    if not m.agents:
        t.append("nobody online yet", style=f"italic {DIM}")
    return t


def render_header(m: Model, label: str, now: float, width: int = 200):
    left = Text.assemble(("◆ ", f"bold {ACCENT}"), ("CREW", "bold"), no_wrap=True, overflow="ellipsis")
    if PROJECT:
        left.append("  ·  ", style=DIM)
        left.append(PROJECT, style=f"bold {ACCENT}")
    span = ago(m.t_last - m.t_first) if m.t_first and m.t_last else "0s"
    clock = time.strftime("%H:%M:%S", time.localtime(now))
    right = Text.assemble((span, "bold"), (" elapsed", DIM), ("  ·  ", DIM), (str(m.count), "bold"),
                          (" events", DIM), ("  ·  ", DIM), (clock, "bold"))
    if label:
        right.append("  ")
        right.append(f" {label} ", style=f"bold black on {ACCENT}")
    g = Table.grid(expand=True, padding=(0, 2))
    g.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    g.add_column(justify="right", no_wrap=True)
    g.add_row(left, right)
    g.add_row(chips(m, words=width >= 130), totals(m))
    return Panel(g, box=box.HEAVY, border_style=ACCENT, padding=(0, 1))


def agent_card(a: Agent, now: float, focused: bool = False):
    c = color(a.name)
    dot = dot_color(a)
    a.spinner.style = dot
    icon = a.spinner if a.state in ANIMATED else Text("●", style=dot)
    detail = Text(no_wrap=True, overflow="ellipsis")
    if a.state == "running":
        detail.append("$ ", style=DIM)
        detail.append(a.detail, style="bold")
    else:
        detail.append(a.detail or a.state, style=BAD if a.state == "failed" else "")
    row = Table.grid(padding=(0, 1), expand=True)
    row.add_column(width=1)
    row.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    row.add_column(justify="right", width=6)
    row.add_row(icon, detail, Text(ago(now - a.since), style=DIM))
    # Line two: the task a worker is on; the last thing the architect said.
    about = Text(no_wrap=True, overflow="ellipsis")
    if a.name == ARCHITECT:
        if a.said:
            about.append("“" + a.said + "”", style=f"italic {DIM}")
        else:
            about.append("plans, dispatches, verifies", style=f"italic {DIM}")
    elif a.task:
        if a.task_n:
            about.append(f"task {a.task_n}  ", style=DIM)
        about.append_text(inline(a.task))
    else:
        about.append("no task yet", style=f"italic {DIM}")
    stats = Text(no_wrap=True, overflow="ellipsis")
    if a.verdict:                           # first, so a narrow card never hides it
        stats.append(f" {str(a.verdict).upper()} ", style=f"bold black on {OK if a.verdict == 'pass' else BAD}")
        stats.append("  ")
    if a.name == ARCHITECT:                 # its hooks report no token usage; what it did is the better line
        stats.append(f"sent {a.sent} task" + ("s" if a.sent != 1 else ""), style=DIM)
        stats.append("  ·  ", style=DIM)
        stats.append(f"verified {a.verdicts}", style=DIM)
    else:
        stats.append(f"tasks {a.tasks}", style=DIM)
        stats.append("  ·  ", style=DIM)
        stats.append(f"{k(a.tokens)} tokens", style=DIM)
        if a.cost:
            stats.append("  ·  ", style=DIM)
            stats.append(f"${a.cost:.2f}", style=DIM)
        if a.thread:
            stats.append("  ·  ", style=DIM)
            stats.append(short(a.thread), style=DIM)
    title = Text.assemble((f" {a.num} ", f"bold black on {c}"), (" ", ""), (a.name, f"bold {c}"), ("  ", ""),
                          ("architect" if a.name == ARCHITECT else "worker", DIM))
    sub = Text()
    if focused:
        sub.append(" focused ", style=f"bold black on {c}")
        sub.append(" ")
    sub.append(a.kind + (f" · {a.model}" if a.model else ""), style=DIM)
    return Panel(Group(row, about, stats), title=title, title_align="left", subtitle=sub, subtitle_align="right",
                 border_style=c, box=box.HEAVY if focused else box.ROUNDED, padding=(0, 1))


LEDGER_STYLE = {"queued": DIM, "running": OK, "done": WAIT, "pass": f"bold {OK}", "fail": f"bold {BAD}"}
LEDGER_MIN_H = 35                # below this terminal height the TASKS panel is left out, not squeezed


def render_tasks(m: Model, rows: int):
    """The ledger: one row per task, numbered in dispatch order (a worker's own task number is in the feed's
    TASK and RESULT titles), newest at the bottom; the oldest go when there is no room."""
    g = Table.grid(padding=(0, 1), expand=True)
    g.add_column(no_wrap=True, style=DIM)
    g.add_column(no_wrap=True)
    g.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    g.add_column(no_wrap=True)
    g.add_column(no_wrap=True, justify="right", style=DIM)
    g.add_column(no_wrap=True, justify="right", style=DIM)
    numbered = list(enumerate(m.ledger.values(), 1))
    for i, r in numbered[-max(1, rows):]:
        st = r["state"]
        g.add_row(f"#{i}", Text(r["worker"], style=color(r["worker"])), inline(r["title"]),
                  Text(st.upper() if st in ("pass", "fail") else st, style=LEDGER_STYLE.get(st, DIM)),
                  ago(r["seconds"]) if r["seconds"] is not None else "",
                  k(int(r["tokens"])) if r["tokens"] else "")
    return Panel(g, title=Text("tasks", style="bold"), title_align="left", border_style=DIM, box=box.ROUNDED,
                 padding=(0, 1))


def render_agents(m: Model, now: float, height: int = 0):
    cards = [agent_card(a, now, a.name == m.focus) for a in m.order()] \
        or [Text(" waiting for agents to connect…", style=f"italic {DIM}")]
    sub = Text("press a number or click a card", style=f"italic {DIM}") if len(cards) > 1 else None
    panel = Panel(Group(*cards), title=Text("agents", style="bold"), subtitle=sub, subtitle_align="right",
                  border_style=DIM, box=box.ROUNDED, padding=(0, 0))
    cards_h = 2 + len(m.order()) * CARD_H
    room = height - cards_h - 2                 # rows left for ledger lines, inside the TASKS panel's border
    if not m.ledger or height + HEADER_H + 1 < LEDGER_MIN_H or room < 1:
        return panel
    col = Layout()
    col.split_column(Layout(panel, size=cards_h), Layout(render_tasks(m, room), ratio=1))
    return col


def arrow_title(src: str, dst: str | None, label: str | None = None) -> Text:
    t = Text()
    t.append(who(src), style=f"bold {color(src)}")
    t.append(f" ──{label}──▶ " if label else " ▶ ", style=DIM)
    t.append(who(dst), style=f"bold {color(dst)}" if dst else DIM)
    return t


def kind_title(label: str, accent: str, arrow: str, name: str | None, extra: str = "") -> Text:
    """A boxed event's title: TASK 2 ▶ worker-1 · heading, in the kind's accent."""
    t = Text(no_wrap=True, overflow="ellipsis")
    t.append(f" {label} ", style=f"bold {accent}")
    t.append(f"{arrow} ", style=DIM)
    t.append(who(name), style=f"bold {color(name)}" if name else DIM)
    if extra:
        t.append("  ·  ", style=DIM)
        t.append_text(inline(extra, "bold"))
    t.append(" ")
    return t


def boxed(body, title: Text, accent: str, subtitle: Text | None = None) -> Panel:
    return Panel(body, title=title, title_align="left", subtitle=subtitle, subtitle_align="right",
                 border_style=accent, box=EDGE, padding=(0, 1))


def md(text: str):
    try:
        return Markdown(text)
    except Exception:
        return Text(text)


def split_heading(text: str):
    """A brief that starts with '# Title' gives the panel its title and keeps the body."""
    lines = text.lstrip().split("\n", 1)
    if lines and lines[0].startswith("# "):
        heading = re.sub(r"(?i)^task\s*:\s*", "", lines[0][2:].strip())     # the box already says TASK
        return heading, (lines[1] if len(lines) > 1 else "").strip()
    return None, text


def est(text: str, width: int) -> int:
    w = max(20, width - 6)
    return sum(max(1, math.ceil(len(p) / w)) for p in (text or " ").split("\n"))


NAME_W = 10                      # the name column of one-line events, so their glyphs line up
RULE_LINE = re.compile(r"^[\s=\-_─━]+$")


def trim_output(out: str, keep: int = 3) -> tuple[list[str], int]:
    """The tail of a command's output worth showing: no blank lines, no lines of only dashes or equals
    signs. Returns (the last `keep` such lines, how many more there were)."""
    lines = [ln.rstrip() for ln in (out or "").splitlines() if ln.strip() and not RULE_LINE.match(ln)]
    return lines[-keep:] if keep else [], max(0, len(lines) - keep)


def line_item(src: str, body: Text):
    """A one-line event: the agent's name in its color, then what it did."""
    t = Text(no_wrap=True, overflow="ellipsis")
    t.append(f"{src:<{NAME_W}}", style=color(src))
    t.append(" ")
    t.append_text(body)
    return t


def exec_done_item(ev: dict, focus: bool):
    src, text, d = ev.get("src") or "?", ev.get("text") or "", ev.get("data") or {}
    rc = d.get("exit_code")
    ok = rc in (0, None)
    head = Text()
    if ok:
        head.append("✔ ", style=OK)
        head.append("$ " + text, style=DIM)
    else:
        head.append(f"✘ exit {rc} ", style=f"bold {BAD}")
        head.append("$ " + text, style="")
    tail, more = trim_output(d.get("output") or "", 12 if focus else 3)
    lines = [line_item(src, head)]
    pad = " " * (NAME_W + 1)
    for i, ln in enumerate(tail):
        t = Text(no_wrap=True, overflow="ellipsis")
        t.append(pad + "│ ", style=FAINT)
        t.append(ln, style=FAINT if ok else "")
        if more and i == len(tail) - 1:
            t.append(f"   (+{more} lines)", style=f"italic {FAINT}")
        lines.append(t)
    return Group(*lines), len(lines)


def feed_item(ev: dict, width: int, focus: bool = False):
    """Return (renderable, estimated_height) for one event.

    Tier 1, boxed with the kind's accent on a thick left edge: prompts, tasks, results, verdicts, the
    architect's messages to you, errors. Other messages: the text under a name line, no box. Tier 2, one dim
    line: status, commands, edits, reads, thinking. Tier 3: a command's last output lines, fainter still,
    except when it failed. In focus mode a command shows 12 output lines instead of 3."""
    kind = ev.get("kind")
    src = ev.get("src") or "?"
    dst = ev.get("dst")
    d = ev.get("data") or {}
    text = ev.get("text") or ""
    c = color(src)
    if kind == "connect":
        return Rule(Text(f" {text} ", style=c), style=DIM, characters="·"), 1
    if kind == "prompt":
        t = Text(no_wrap=True, overflow="ellipsis")
        t.append(" " + who(src), style=f"bold {ACCENTS['prompt']}")
        t.append(" ▶ ", style=DIM)
        t.append(who(dst or ARCHITECT) + " ", style=f"bold {color(dst or ARCHITECT)}")
        return boxed(Text(text), t, ACCENTS["prompt"]), est(text, width) + 2
    if kind == "task":
        heading, body = split_heading(text)
        flags = [f for f, on in (("continues the thread", d.get("resume")), ("read-only", d.get("read_only")))
                 if on]
        sub = Text(" " + "  ·  ".join(flags) + " ", style=DIM) if flags else None
        title = kind_title(f"TASK {d.get('n', '')}".strip(), ACCENTS["task"], "▶", dst, heading or "")
        return boxed(md(body), title, ACCENTS["task"], sub), est(body, width) + 2
    if kind == "message":
        if src == ARCHITECT and dst in (None, USER):
            t = Text(no_wrap=True, overflow="ellipsis")
            t.append(" " + who(src), style=f"bold {ACCENTS['message']}")
            t.append(" ▶ ", style=DIM)
            t.append(who(dst) + " ", style="bold" if dst else DIM)
            return boxed(md(text), t, ACCENTS["message"]), est(text, width) + 2
        head = Text()
        head.append("▶ ", style=DIM)
        head.append(who(dst), style=color(dst) if dst else DIM)
        g = Table.grid(padding=(0, 1))
        g.add_column(width=NAME_W)
        g.add_column(ratio=1)
        g.add_row(Text(src, style=f"bold {c}", no_wrap=True, overflow="ellipsis"), Group(head, md(text)))
        return g, est(text, width - NAME_W) + 1
    if kind == "exec":
        return line_item(src, Text.assemble(("$ ", DIM), (text, DIM), ("  running…", f"italic {DIM}"))), 1
    if kind == "exec_done":
        return exec_done_item(ev, focus)
    if kind == "edit":
        return line_item(src, Text.assemble(("✎ ", DIM), ("edits ", DIM), (text, DIM))), 1
    if kind == "read":
        return line_item(src, Text.assemble(("  reads ", DIM), (text, DIM))), 1
    if kind == "result":
        rc = d.get("exit_code", 0)
        accent = ACCENTS["result"] if rc == 0 else BAD
        sub = Text(f" exit {rc} · {d.get('seconds', '?')}s · {d.get('commands', 0)} commands · "
                   f"{k(int(d.get('tokens') or 0))} tokens · thread {short(d.get('thread_id'))} ", style=DIM)
        title = kind_title(f"RESULT {d.get('n', '')}".strip(), accent, "◀", src)
        return boxed(md(text), title, accent, sub), est(text, width) + 2
    if kind == "verify":
        v = str(d.get("verdict") or "?").upper()
        accent = OK if v == "PASS" else BAD
        return boxed(Text(text), kind_title(v, accent, "▶", dst), accent), est(text, width) + 2
    if kind == "status":
        return line_item(src, Text("· " + text, style=DIM)), 1
    if kind == "thinking":
        return line_item(src, Text("… " + first_line(text, 200), style=f"italic {FAINT}")), 1
    if kind == "error":
        return boxed(Text(text, style=BAD), kind_title("ERROR", BAD, "◀", src), BAD), est(text, width) + 2
    return line_item(src, Text(f"· {kind}: {text}", style=DIM)), 1


FEED_CHROME = 14             # panel borders + padding (4), time column (8), the gaps around it (2)


def measure(console: Console | None, ev: dict, r, guess: int, width: int, focus: bool, cache: dict) -> int:
    """Rows an item really takes at this width (estimates undercount Markdown and wrapping)."""
    if console is None:
        return guess
    key = (id(ev), width, focus)
    if key not in cache:
        try:
            cache[key] = len(console.render_lines(r, console.options.update(width=max(10, width), height=None, overflow="ellipsis"),
                                                  pad=False))
        except Exception:
            cache[key] = guess
    return cache[key]


class Tail:
    """The last `rows` lines of a renderable, for an item that only partly fits at the top of the feed."""

    def __init__(self, renderable, rows: int, width: int):
        self.r, self.rows, self.width = renderable, rows, width

    def __rich_console__(self, console, options):
        lines = console.render_lines(self.r, options.update(width=self.width, height=None, overflow="ellipsis"),
                                     pad=False)
        keep = lines[-(self.rows - 1):] if self.rows > 1 else []
        yield Text("⋮ earlier lines above", style=f"italic {DIM}", no_wrap=True)
        for line in keep:
            yield from line
            yield Segment.line()


def render_feed(m: Model, budget: int, width: int, console: Console | None = None):
    items = m.visible()
    shown = items[: len(items) - m.scroll] if m.scroll else items
    rows = []
    used = 0
    item_w = width - FEED_CHROME
    focus = bool(m.focus)
    for ev in reversed(shown):
        r, guess = feed_item(ev, item_w, focus=focus)
        h = measure(console, ev, r, guess, item_w, focus, m.heights)
        if used + h > budget:
            left = budget - used
            if not rows and console is not None:        # the newest item alone is taller than the feed: its end matters most
                rows.append((ev, Tail(r, budget, item_w)))
            elif console is not None and left >= 2:      # fill the gap with the bottom of the older item
                rows.append((ev, Tail(r, left, item_w)))
            elif console is not None and left == 1:    # keep the newest event on the bottom edge
                rows.append(({}, Text("")))
            elif not rows:
                rows.append((ev, r))
            break
        rows.append((ev, r))
        used += h
    rows.reverse()
    g = Table.grid(padding=(0, 1), expand=True)
    g.add_column(width=8, style=DIM)
    g.add_column(ratio=1)
    for ev, r in rows:
        g.add_row(hms(ev.get("ts")), r)
    if m.focus:
        a = m.agents[m.focus]
        c = color(a.name)
        title = Text.assemble((a.name, f"bold {c}"), ("  ·  ", DIM), (role(a.name), DIM), ("  ·  ", DIM),
                              (f"{len(items)} of {len(m.feed)} events", DIM),
                              ("  ·  sent or received", DIM))
        border = c
        if not rows:
            g.add_row("", Text(f"nothing from {a.name} yet.", style=f"italic {DIM}"))
    else:
        title, border = Text("conversation", style="bold"), DIM
        if not rows:
            for line, style in GUIDE:
                g.add_row("", Text(line, style=f"{style} {DIM}".strip()))
    sub = Text(f" ↑ scrolled back · {m.scroll} newer below · End returns ", style=f"bold black on {ACCENT}") \
        if m.scroll else None
    return Panel(g, title=title, title_align="left", subtitle=sub, subtitle_align="right",
                 border_style=border, box=box.ROUNDED, padding=(0, 1))


def render_footer(m: Model, label: str):
    t = Text(no_wrap=True, overflow="ellipsis")
    t.append("  ")
    for a in m.order():
        c = color(a.name)
        t.append(f" {a.num} ", style=f"bold black on {c}" if a.name == m.focus else f"bold {c}")
        t.append(f" {a.name}   ", style=DIM)
    t.append("·   " + HINT, style=DIM)
    return t


def build(m: Model, console: Console, label: str = "", now: float | None = None, layout: str | None = None):
    """The whole screen. `now` is the wall clock, or the last event's time for a snapshot of a recording."""
    now = now or time.time()
    if (layout or LAYOUT) == "strip":
        return build_strip(m, console, now)
    w, h = console.size.width, console.size.height
    if m.focus:
        label = f"{label} · {m.focus}" if label else m.focus
    layout = Layout()
    layout.split_column(Layout(name="header", size=HEADER_H), Layout(name="body", ratio=1), Layout(name="footer", size=1))
    agents_w = agents_width(w)
    layout["body"].split_row(Layout(name="agents", size=agents_w), Layout(name="feed", ratio=1))
    layout["header"].update(render_header(m, label, now, w))
    layout["agents"].update(render_agents(m, now, h - HEADER_H - 1))
    layout["feed"].update(render_feed(m, max(5, h - HEADER_H - 1 - 2), w - agents_w, console))
    layout["footer"].update(render_footer(m, label))
    return layout


# ----------------------------------------------------------------------------- strip layout
# A narrow column between the architect's pane and the worker's log: only the exchange between them
# and their states. What each side says and does is in its own pane, so it is not repeated here.

STRIP_STATUS = ("working", "idle", "failed", "stopped")
STRIP_HINT = "q quit  ·  ↑↓ wheel scroll  ·  End newest"


def strip_word(a: Agent) -> str:
    if a.name == ARCHITECT:
        return "waiting for you" if a.state == "idle" else "working"
    if a.state in ("idle", "done"):
        return "idle"
    return a.state if a.state in ("failed", "offline") else "working"


def strip_card(a: Agent, now: float):
    c = color(a.name)
    dot = dot_color(a)
    a.spinner.style = dot
    line = Table.grid(padding=(0, 1), expand=True)
    line.add_column(width=1)
    line.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    line.add_column(justify="right", no_wrap=True)
    if a.state == "running" and a.name != ARCHITECT:
        what = Text.assemble(("$ ", DIM), (a.detail, "bold"))
    else:
        what = Text(strip_word(a), style=f"bold {dot}" if a.state in ("idle", "failed") else "")
    icon = a.spinner if a.state in ANIMATED else Text("●", style=dot)
    line.add_row(icon, what, Text(ago(now - a.since), style=DIM))
    title = Text.assemble((a.name, f"bold {c}"), ("  " + (a.model or "default model"), DIM), no_wrap=True,
                          overflow="ellipsis")
    return Panel(line, title=title, title_align="left", border_style=c, box=box.ROUNDED, padding=(0, 1))


def strip_lines(ev: dict, width: int) -> list[Text] | None:
    """One exchange entry as at most 3 lines, or None for an event the strip leaves out."""
    kind, src, dst = ev.get("kind"), ev.get("src") or "?", ev.get("dst")
    d, text = ev.get("data") or {}, (ev.get("text") or "").replace("`", "")

    def body(s: str, lines: int = 2, style: str = "") -> list[Text]:
        return [Text(ln, style=style, no_wrap=True, overflow="ellipsis")
                for ln in textwrap.wrap(s, width, max_lines=lines, placeholder=" …")]

    def head(label: str, accent: str, arrow: str, name: str, extra: str = "") -> Text:
        t = Text(no_wrap=True, overflow="ellipsis")
        t.append(label, style=f"bold {accent}")
        t.append(f" {arrow} ", style=DIM)
        t.append(name, style=f"bold {color(name)}")
        if extra:
            t.append(extra, style=DIM)
        return t

    if kind == "task":
        heading, rest = split_heading(text)
        return [head(f"TASK {d.get('n', '')}".strip(), ACCENTS["task"], "▶", dst or "?")] + \
            body(heading or first_line(rest, 400))
    if kind == "result":
        rc = d.get("exit_code", 0)
        extra = f"  exit {rc} · {ago(d.get('seconds') or 0)}"
        accent = ACCENTS["result"] if rc == 0 else BAD
        return [head(f"RESULT {d.get('n', '')}".strip(), accent, "◀", src, extra)] + body(first_line(text, 400))
    if kind == "verify":
        v = str(d.get("verdict") or "?").upper()
        return [head(v, OK if v == "PASS" else BAD, "▶", dst or "?")] + body(first_line(text, 400))
    if kind == "error":
        return [head("ERROR", BAD, "◀", src)] + body(first_line(text, 400), style=BAD)
    if kind == "status":
        low = text.lower()
        if src == ARCHITECT and low.startswith("waiting for you"):
            return [Text.assemble((src, f"bold {color(src)}"), (" · waiting for you", f"bold {WAIT}"))]
        if src != ARCHITECT and low.startswith(STRIP_STATUS):
            word = text.split(" (")[0] if low.startswith(("failed", "stopped")) else low.split()[0]
            return [Text.assemble((src, color(src)), (f" · {word}", BAD if low.startswith(("failed", "stopped"))
                                                       else DIM), no_wrap=True, overflow="ellipsis")]
    return None


def render_exchange(m: Model, budget: int, width: int):
    """The entries that fit, newest on the bottom row."""
    inner = max(10, width - 4 - 10)                 # the panel's border and padding, the time column and gap
    rows, used = [], 0
    items = m.feed[: len(m.feed) - m.scroll] if m.scroll else m.feed
    for ev in reversed(items):
        lines = strip_lines(ev, inner)
        if not lines:
            continue
        if used + len(lines) > budget:
            break
        rows.append((ev, lines))
        used += len(lines)
    rows.reverse()
    g = Table.grid(padding=(0, 1), expand=True)
    g.add_column(width=8, style=DIM, no_wrap=True)
    g.add_column(ratio=1)
    for ev, lines in rows:
        g.add_row(hms(ev.get("ts")), Group(*lines))
    if not rows:
        g.add_row("", Text("tasks, results and verdicts show here", style=f"italic {DIM}"))
    sub = Text(f" ↑ {m.scroll} newer below ", style=f"bold black on {ACCENT}") if m.scroll else None
    return Panel(g, title=Text("exchange", style="bold"), title_align="left", subtitle=sub, subtitle_align="right",
                 border_style=DIM, box=box.ROUNDED, padding=(0, 1))


def build_strip(m: Model, console: Console, now: float):
    """architect | this | worker: two compact cards, the TASKS ledger, the exchange between them."""
    w, h = console.size.width, console.size.height
    span = ago(m.t_last - m.t_first) if m.t_first and m.t_last else "0s"
    head = Text(no_wrap=True, overflow="ellipsis")
    head.append("◆ ", style=f"bold {ACCENT}")
    head.append(PROJECT or "crew", style=f"bold {ACCENT}")
    head.append("  ·  ", style=DIM)
    head.append(span, style="bold")
    head.append("  ·  ", style=DIM)
    head.append(k(m.tokens), style="bold")
    head.append(" tokens", style=DIM)
    cards = [strip_card(a, now) for a in m.order()]
    cards_h = 3 * len(cards)
    rest = h - 2 - cards_h                          # header and footer lines
    ledger_rows = min(len(m.ledger), max(1, (rest - 2) // 3 - 2)) if m.ledger else 0
    layout = Layout()
    parts = [Layout(head, name="header", size=1)]
    if cards:
        parts.append(Layout(Group(*cards), name="cards", size=cards_h))
    if ledger_rows:
        parts.append(Layout(render_tasks(m, ledger_rows), name="tasks", size=ledger_rows + 2))
    budget = max(3, rest - (ledger_rows + 2 if ledger_rows else 0) - 2)
    parts.append(Layout(render_exchange(m, budget, w), name="exchange", ratio=1))
    hint = Text("  " + STRIP_HINT, style=DIM, no_wrap=True, overflow="ellipsis")
    parts.append(Layout(hint, name="footer", size=1))
    layout.split_column(*parts)
    return layout


# ----------------------------------------------------------------------------- input

SGR = re.compile(rb"\x1b\[<(\d+);(\d+);(\d+)([Mm])")      # mouse report, SGR encoding
CSI = re.compile(rb"\x1b\[([0-9;]*)([A-Za-z~])")             # cursor and function keys
SS3 = re.compile(rb"\x1bO()([A-Z])")                           # the same keys in application mode (ESC O A)
CSI_KEYS = {"A": "up", "B": "down", "5~": "pgup", "6~": "pgdn", "F": "end", "H": "home", "4~": "end", "1~": "home",
            "Z": "btab"}


def parse_keys(buf: bytes) -> list[tuple]:
    """Raw terminal input → ('key', name) and ('mouse', button, x, y, pressed) events."""
    out, i = [], 0
    while i < len(buf):
        if buf[i] == 0x1B:
            m = SGR.match(buf, i)
            if m:
                out.append(("mouse", int(m[1]), int(m[2]), int(m[3]), m[4] == b"M"))
                i = m.end()
                continue
            m = CSI.match(buf, i) or SS3.match(buf, i)
            if m:
                out.append(("key", CSI_KEYS.get((m[1] + m[2]).decode(), "?")))
                i = m.end()
                continue
            out.append(("key", "esc"))
            i += 1
        else:
            ch = chr(buf[i])
            out.append(("key", {"\t": "tab", "\x03": "ctrl-c"}.get(ch, ch)))
            i += 1
    return out


class Keys:
    """Keyboard and mouse from the terminal, no echo. Does nothing when stdin is not a terminal."""
    ON, OFF = "\x1b[?1000h\x1b[?1006h", "\x1b[?1000l\x1b[?1006l"      # click + wheel reports, SGR encoded

    def __init__(self):
        self.fd = sys.stdin.fileno() if sys.stdin.isatty() else None
        self.saved = None

    def __enter__(self):
        if self.fd is not None:
            self.saved = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)                  # no echo, no line buffering, Ctrl-C still works
            sys.stdout.write(self.ON)
            sys.stdout.flush()
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            sys.stdout.write(self.OFF)
            sys.stdout.flush()
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)

    def poll(self) -> list[tuple]:
        out = []
        if self.fd is None:
            return out
        while select.select([self.fd], [], [], 0)[0]:
            chunk = os.read(self.fd, 256)
            if not chunk:
                break
            out += parse_keys(chunk)
        return out


def handle(m: Model, events: list[tuple], width: int, strip: bool = False) -> bool:
    """Apply input events to the model. Returns False when the user asked to quit. The strip has no
    focus: only quitting and scrolling work there."""
    agents_w = agents_width(width)
    for e in events:
        if e[0] == "mouse":
            _, btn, x, y, pressed = e
            if btn == 64:
                m.scroll_by(3)
            elif btn == 65:
                m.scroll_by(-3)
            elif pressed and btn == 0 and not strip:
                m.click(x, y, agents_w)
            continue
        key = e[1]
        if key in ("q", "Q", "ctrl-c"):
            return False
        if strip and (key.isdigit() or key in ("esc", "a", "tab", "btab")):
            continue
        if key.isdigit():
            m.focus_num(int(key))
        elif key in ("esc", "a"):
            m.set_focus(None)
        elif key == "tab":
            m.cycle(1)
        elif key == "btab":
            m.cycle(-1)
        elif key in ("up", "k"):
            m.scroll_by(1)
        elif key in ("down", "j"):
            m.scroll_by(-1)
        elif key == "pgup":
            m.scroll_by(10)
        elif key == "pgdn":
            m.scroll_by(-10)
        elif key in ("end", "G"):
            m.scroll = 0
        elif key == "home":
            m.scroll_by(10 ** 6)
    return True


# ----------------------------------------------------------------------------- sources

class FileSource:
    done = False

    def __init__(self, path: str):
        self.f = Follower(path)

    def poll(self) -> list[dict]:
        return self.f.poll()


class ReplaySource:
    """Replays a recorded events file with its original pacing (sped up), timestamps shifted to now."""

    def __init__(self, path: str, speed: float):
        self.events = Follower(path).poll()
        self.i = 0
        self.speed = max(0.1, speed)
        self.next_at = time.time() + 0.8
        self.done = not self.events

    def poll(self) -> list[dict]:
        out = []
        now = time.time()
        while self.i < len(self.events) and now >= self.next_at:
            ev = dict(self.events[self.i])
            orig_ts = ev.get("ts", 0)
            ev["ts"] = now
            out.append(ev)
            self.i += 1
            if self.i < len(self.events):
                gap = (self.events[self.i].get("ts", 0) - orig_ts) / self.speed
                self.next_at = now + min(2.5, max(0.08, gap))
            else:
                self.done = True
        return out


SVG_RUN = re.compile(r'(<text [^>]*?\bx=")([\d.]+)("[^>]*?textLength=")([\d.]+)("[^>]*>)([^<]*)(</text>)')
NBSP = "&#160;"


def svg_spaces(svg: str) -> str:
    """Move the spaces at either end of each text run into its position and length. Rich writes them as
    &#160;, which librsvg (rsvg-convert, many slide tools) drops before stretching the run over its
    textLength, so "fable" + " waiting" turned into "fablewaiting" on slides. The terminal never had this."""
    def fix(mt):
        body = mt[6]
        n = cell_len(html.unescape(body))
        lead = (len(body) - len(body.lstrip(NBSP[0]))) and len(re.match(f"(?:{NBSP})*", body)[0]) // len(NBSP)
        trail = len(re.search(f"(?:{NBSP})*$", body)[0]) // len(NBSP)
        if not (lead or trail) or lead + trail >= n:
            return mt[0]
        cw = float(mt[4]) / n
        core = body[lead * len(NBSP): len(body) - trail * len(NBSP)]
        return (f"{mt[1]}{float(mt[2]) + lead * cw:g}{mt[3]}{float(mt[4]) - (lead + trail) * cw:g}{mt[5]}"
                f"{core}{mt[7]}")
    return SVG_RUN.sub(fix, svg)


SVG_TEXT = re.compile(r'<text class="([^"]+)" x="([\d.]+)" y="([\d.]+)" textLength="([\d.]+)" '
                      r'clip-path="([^"]+)">([^<]*)</text>')


def svg_merge(svg: str) -> str:
    """Join text runs that touch and share a style: librsvg leaves a hairline seam between runs, which
    shows as a gap in box rules ("──── ─") on slides."""
    out, last, prev = [], 0, None              # prev: [class, x, y, length, clip, text] of the pending run
    for mt in SVG_TEXT.finditer(svg):
        cls, x, y, length, clip, text = mt.groups()
        x, length = float(x), float(length)
        if prev and mt.start() == last and (cls, y, clip) == (prev[0], prev[2], prev[4]) \
                and abs(prev[1] + prev[3] - x) < 0.05:
            prev[3] += length
            prev[5] += text
        else:
            if prev:
                out.append(f'<text class="{prev[0]}" x="{prev[1]:g}" y="{prev[2]}" textLength="{prev[3]:g}" '
                           f'clip-path="{prev[4]}">{prev[5]}</text>')
            out.append(svg[last:mt.start()])
            prev = [cls, x, y, length, clip, text]
        last = mt.end()
    if prev:
        out.append(f'<text class="{prev[0]}" x="{prev[1]:g}" y="{prev[2]}" textLength="{prev[3]:g}" '
                   f'clip-path="{prev[4]}">{prev[5]}</text>')
    out.append(svg[last:])
    return "".join(out)


def snapshot(events_path: str, out: str, width: int, height: int, focus: str | None = None, scroll: int = 0,
             layout: str | None = None) -> None:
    console = Console(record=True, width=width, height=height, force_terminal=True, color_system="truecolor",
                      file=io.StringIO(), theme=THEME)
    m = Model()
    for ev in Follower(events_path).poll():
        m.apply(ev)
    m.set_focus(focus)
    m.scroll_by(scroll)
    console.print(build(m, console, "snapshot", now=m.t_last or time.time(), layout=layout))
    svg = console.export_svg(title=f"crew · {PROJECT}" if PROJECT else "crew")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(svg_spaces(svg_merge(svg)))
    print(f"wrote {out} ({m.count} events)")


def run(events: str | None = None, replay: str | None = None, speed: float = 1.0,
        layout: str | None = None) -> int:
    """Follow an events file live, or replay a recording, until q."""
    label = f"replay ×{speed:g}" if replay else "live"
    source = ReplaySource(replay, speed) if replay else FileSource(events)
    console = Console(theme=THEME)
    m = Model()
    if not replay:
        for ev in source.poll():        # show history that is already in the file
            m.apply(ev)
    strip = (layout or LAYOUT) == "strip"
    with Keys() as keys, Live(build(m, console, label, layout=layout), console=console, screen=True,
                              refresh_per_second=12) as live:
        try:
            while True:
                for ev in source.poll():
                    m.apply(ev)
                if not handle(m, keys.poll(), console.size.width, strip):
                    break
                lbl = label + (" · finished" if source.done and replay else "")
                live.update(build(m, console, lbl, layout=layout))
                time.sleep(0.08)
        except KeyboardInterrupt:
            pass
    return 0
