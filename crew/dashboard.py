"""Live terminal dashboard for the crew bus.

  crew dashboard [--project DIR]               follow <project>/.crew/events.jsonl live
  crew replay samples/todo-demo.jsonl --speed 2
  crew snapshot frame.svg [--events FILE | --replay FILE] [--focus NAME] [--width 150] [--height 45]

Roles and colors come from crew.toml when there is one; without it (a replay of a recording)
the default names apply: user, fable (the architect), worker-*.
"""
from __future__ import annotations

import io
import math
import os
import re
import select
import sys
import termios
import time
import tty

from rich import box
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.segment import Segment
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

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
ARCHITECT = "fable"             # who receives prompts and results when an event names no one
PROJECT = ""                    # shown in the header badge
WORKER_COLORS = ["#34D399", "#38BDF8", "#FBBF24", "#FB923C"]


def configure(cfg) -> None:
    """Take the agents' names, roles and colors from a loaded crew.toml (crew.config.Config)."""
    global ARCHITECT, PROJECT
    ARCHITECT = cfg.architect.name
    PROJECT = cfg.name
    PALETTE.clear()
    ROLES.clear()
    PALETTE.update({"user": "#F8FAFC", ARCHITECT: ACCENT})
    ROLES.update({"user": "you",
                  ARCHITECT: "architect · Claude Code" + (f" · {cfg.architect.model}" if cfg.architect.model else "")})
    colors = WORKER_COLORS + EXTRA
    for i, w in enumerate(cfg.workers):
        PALETTE[w.name] = colors[i % len(colors)]
        ROLES[w.name] = f"worker · {'Claude' if w.kind == 'claude' else 'Codex'}" + (f" · {w.model}" if w.model else "")
OK, BAD, DIM = "#34D399", "#F87171", "grey58"

STATE_STYLE = {
    "offline": ("○", DIM), "idle": ("●", OK), "accepted": ("◎", "#38BDF8"), "waiting": ("◌", "#FBBF24"),
    "thinking": ("◐", "#FBBF24"), "busy": ("◉", "#38BDF8"), "running": ("▶", "#38BDF8"),
    "done": ("✔", OK), "failed": ("✖", BAD),
}
ANIMATED = {"thinking", "busy", "running", "waiting"}

HEADER_H, CARD_H = 3, 4          # rows: the header panel, and one agent card (border, 2 lines, border)
CARD_TOP = HEADER_H + 2          # 1-based row of the first card's top border (header, then the agents panel border)
HINT = "1-9 / click: focus an agent  ·  0 / Esc: everyone  ·  Tab: next  ·  ↑↓ wheel: scroll  ·  End: newest  ·  q: quit"


def agents_width(w: int) -> int:
    return 48 if w >= 130 else max(34, w // 3)


def color(name: str | None) -> str:
    if not name:
        return DIM
    return PALETTE.get(name) or EXTRA[sum(map(ord, name)) % len(EXTRA)]


def role(name: str) -> str:
    return ROLES.get(name, "worker · Codex")


def short(t) -> str:
    return "…" + (t or "")[-6:] if t else ""


def hms(ts) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts)) if ts else ""


def ago(s: float) -> str:
    s = max(0, int(s))
    return f"{s}s" if s < 90 else f"{s // 60}m{s % 60:02d}s"


def k(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


class Agent:
    def __init__(self, name: str):
        self.name = name
        self.state = "offline"
        self.detail = ""
        self.since = time.time()
        self.thread = None
        self.tasks = 0
        self.tokens = 0
        self.verdict = None
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
        self.t_first = None
        self.t_last = None
        self.focus: str | None = None       # an agent name, or None for everyone
        self.scroll = 0                     # how many of the newest feed items are hidden (scrolled back)
        self.heights: dict = {}             # (event, width, focus) -> rendered rows, so each item is measured once

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

    def apply(self, ev: dict) -> None:
        kind = ev.get("kind")
        src = ev.get("src") or "?"
        dst = ev.get("dst")
        d = ev.get("data") or {}
        ts = ev.get("ts") or time.time()
        text = ev.get("text") or ""
        now = time.time()
        self.count += 1
        self.t_first = self.t_first or ts
        self.t_last = ts
        a = self.agent(src)
        if a.state == "offline":
            a.set("idle", "online", now)
        if kind == "connect":
            a.set("idle", "online", now)
        elif kind == "prompt":
            self.agent(dst or ARCHITECT).set("thinking", "reading your request", now)
        elif kind == "task":
            a.set("waiting", f"waiting on {dst}", now)
            w = self.agent(dst or "?")
            w.set("accepted", f"task {d.get('n', '')} received".replace("  ", " "), now)
            w.verdict = None
        elif kind == "status":
            if d.get("thread_id"):
                a.thread = d["thread_id"]
            low = text.lower()
            state = "idle" if low.startswith(("idle", "waiting", "online")) else ("failed" if low.startswith(("failed", "stopped")) else "busy")
            a.set(state, text, now)
        elif kind == "exec":
            a.set("running", text, now)
        elif kind == "exec_done":
            rc = d.get("exit_code")
            a.set("busy", "command finished" + (f" (exit {rc})" if rc not in (None, 0) else ""), now)
        elif kind == "edit":
            a.set("busy", f"editing {text}", now)
        elif kind == "read":
            a.set("busy", f"reading {text}", now)
        elif kind == "message":
            a.set("busy", "replied", now)
        elif kind == "thinking":
            a.set("thinking", "thinking", now)
        elif kind == "usage":
            a.tokens += int(d.get("input_tokens") or 0) + int(d.get("output_tokens") or 0)
        elif kind == "result":
            rc = d.get("exit_code", 0)
            a.tasks += 1
            # Codex's final message arrives first as a plain message, then again as the result: keep one.
            if self.feed and self.feed[-1].get("kind") == "message" and self.feed[-1].get("src") == src \
                    and (self.feed[-1].get("text") or "").strip() == text.strip():
                self.feed.pop()
            if d.get("thread_id"):
                a.thread = d["thread_id"]
            a.set("done" if rc == 0 else "failed",
                  f"task {d.get('n', a.tasks)} {'done' if rc == 0 else 'failed'} in {d.get('seconds', '?')}s", now)
            self.agent(dst or ARCHITECT).set("thinking", f"verifying {src}", now)
        elif kind == "verify":
            w = self.agent(dst or "?")
            w.verdict = d.get("verdict")
            a.set("idle", f"{d.get('verdict', '?')} for {dst}", now)
        elif kind == "error":
            a.set("failed", text[:60], now)
        if kind != "usage":
            self.feed.append(ev)


# ----------------------------------------------------------------------------- rendering

def render_header(m: Model, label: str):
    left = Text.assemble(("◆ ", f"bold {ACCENT}"), ("CREW", "bold"), ("  ·  ", DIM),
                         ("one architect, many workers, every message live", DIM))
    span = ago(m.t_last - m.t_first) if m.t_first and m.t_last else "0s"
    right = Text.assemble((f"{m.count} events", DIM), ("  ·  ", DIM), (span, DIM), ("  ·  ", DIM),
                          (time.strftime("%H:%M:%S"), "bold"))
    if label:
        right.append(f"  {label}  ", style=f"bold black on {ACCENT}")
    g = Table.grid(expand=True)
    g.add_column()
    g.add_column(justify="right")
    g.add_row(left, right)
    return Panel(g, box=box.HEAVY, border_style=ACCENT, padding=(0, 1))


def agent_card(a: Agent, now: float, focused: bool = False):
    c = color(a.name)
    icon_char, icon_style = STATE_STYLE.get(a.state, ("●", DIM))
    icon = a.spinner if a.state in ANIMATED else Text(icon_char, style=icon_style)
    detail = Text(no_wrap=True, overflow="ellipsis")
    if a.state == "running":
        detail.append("$ ", style=DIM)
        detail.append(a.detail, style="bold")
    else:
        detail.append(a.detail or a.state, style=icon_style if a.state in ("done", "failed") else "")
    row = Table.grid(padding=(0, 1), expand=True)
    row.add_column(width=1)
    row.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    row.add_column(justify="right", width=6)
    row.add_row(icon, detail, Text(ago(now - a.since), style=DIM))
    stats = Text(no_wrap=True, overflow="ellipsis")
    stats.append(f"tasks {a.tasks}", style=DIM)
    stats.append("  ·  ", style=DIM)
    stats.append(f"tokens {k(a.tokens)}", style=DIM)
    if a.thread:
        stats.append("  ·  ", style=DIM)
        stats.append(f"thread {short(a.thread)}", style=DIM)
    if a.verdict:
        stats.append("  ")
        stats.append(f" {str(a.verdict).upper()} ", style=f"bold black on {OK if a.verdict == 'pass' else BAD}")
    title = Text.assemble((f" {a.num} ", f"bold black on {c}"), (" ", ""), (a.name, f"bold {c}"), ("  ", ""),
                          (role(a.name), DIM))
    sub = Text(" focused ", style=f"bold black on {c}") if focused else None
    return Panel(Group(row, stats), title=title, title_align="left", subtitle=sub, subtitle_align="right",
                 border_style=c, box=box.HEAVY if focused else box.ROUNDED, padding=(0, 1))


def render_agents(m: Model, now: float):
    cards = [agent_card(a, now, a.name == m.focus) for a in m.order()] \
        or [Text("waiting for agents to connect…", style=f"italic {DIM}")]
    sub = Text("press a number or click a card", style=f"italic {DIM}") if len(cards) > 1 else None
    return Panel(Group(*cards), title=Text("agents", style="bold"), subtitle=sub, subtitle_align="right",
                 border_style=DIM, box=box.ROUNDED, padding=(0, 0))


def arrow_title(src: str, dst: str | None, label: str | None = None) -> Text:
    t = Text()
    t.append(src, style=f"bold {color(src)}")
    t.append(f" ──{label}──▶ " if label else " ▶ ", style=DIM)
    t.append(dst or "all", style=f"bold {color(dst)}" if dst else DIM)
    return t


def md(text: str):
    try:
        return Markdown(text)
    except Exception:
        return Text(text)


def split_heading(text: str):
    """A brief that starts with '# Title' gives the panel its title and keeps the body."""
    lines = text.lstrip().split("\n", 1)
    if lines and lines[0].startswith("# "):
        return lines[0][2:].strip(), (lines[1] if len(lines) > 1 else "").strip()
    return None, text


def est(text: str, width: int) -> int:
    w = max(20, width - 6)
    return sum(max(1, math.ceil(len(p) / w)) for p in (text or " ").split("\n"))


def feed_item(ev: dict, width: int, focus: bool = False):
    """Return (renderable, estimated_height) for one event. In focus mode, command output gets 12 lines, not 4."""
    kind = ev.get("kind")
    src = ev.get("src") or "?"
    dst = ev.get("dst")
    d = ev.get("data") or {}
    text = ev.get("text") or ""
    c = color(src)
    if kind == "connect":
        return Rule(Text(f" {text} ", style=c), style=DIM, characters="·"), 1
    if kind == "prompt":
        return Panel(Text(text), title=arrow_title(src, dst), title_align="left",
                     border_style=color(src), box=box.ROUNDED), est(text, width) + 2
    if kind == "task":
        heading, body = split_heading(text)
        title = arrow_title(src, dst, "task")
        if heading:
            title.append("  ·  ", style=DIM)
            title.append(heading, style="bold")
        sub = Text(f"task {d.get('n', '')}" + ("  · continues the thread" if d.get("resume") else ""), style=DIM)
        return Panel(md(body), title=title, subtitle=sub, subtitle_align="right",
                     title_align="left", border_style=c, box=box.ROUNDED), est(body, width) + 2
    if kind == "message":
        return Panel(md(text), title=arrow_title(src, dst), title_align="left",
                     border_style=c, box=box.ROUNDED), est(text, width) + 2
    if kind == "exec":
        t = Text(no_wrap=True, overflow="ellipsis")
        t.append(src, style=f"bold {c}")
        t.append("  $ ", style=DIM)
        t.append(text, style="bold")
        t.append("  …", style=DIM)
        return t, 1
    if kind == "exec_done":
        rc = d.get("exit_code")
        ok = rc in (0, None)
        t = Text(no_wrap=True, overflow="ellipsis")
        t.append(src, style=f"bold {c}")
        t.append("  ")
        t.append("✔" if ok else "✖", style=OK if ok else BAD)
        t.append(" $ ", style=DIM)
        t.append(text, style=DIM if ok else BAD)
        if rc is not None:
            t.append(f"  exit {rc}", style=DIM if ok else BAD)
        lines = [t]
        out = (d.get("output") or "").rstrip()
        if out:
            for ln in out.splitlines()[-(12 if focus else 4):]:
                lines.append(Text("        │ " + ln, style=DIM, no_wrap=True, overflow="ellipsis"))
        return Group(*lines), len(lines)
    if kind in ("edit", "read"):
        t = Text(no_wrap=True, overflow="ellipsis")
        t.append(src, style=f"bold {c}")
        t.append("  ✎ " if kind == "edit" else "  ⌕ ", style=DIM)
        t.append(("edits " if kind == "edit" else "reads ") + text, style=DIM)
        return t, 1
    if kind == "result":
        rc = d.get("exit_code", 0)
        ok = rc == 0
        sub = Text(f"exit {rc} · {d.get('seconds', '?')}s · {d.get('commands', 0)} commands · "
                   f"{k(int(d.get('tokens') or 0))} tokens · thread {short(d.get('thread_id'))}", style=DIM)
        return Panel(md(text), title=arrow_title(src, dst, "result"), subtitle=sub, subtitle_align="right",
                     title_align="left", border_style=OK if ok else BAD, box=box.HEAVY), est(text, width) + 2
    if kind == "verify":
        v = str(d.get("verdict") or "?").upper()
        ok = v == "PASS"
        body = Text()
        body.append(f" {v} ", style=f"bold black on {OK if ok else BAD}")
        body.append("  " + text)
        return Panel(body, title=arrow_title(src, dst, "verify"), title_align="left",
                     border_style=OK if ok else BAD, box=box.HEAVY), est(text, width) + 2
    if kind == "status":
        t = Text(no_wrap=True, overflow="ellipsis")
        t.append(src, style=f"bold {c}")
        t.append("  · " + text, style=DIM)
        return t, 1
    if kind == "thinking":
        t = Text(no_wrap=True, overflow="ellipsis")
        t.append(src, style=f"bold {c}")
        t.append("  is thinking… ", style=f"italic {DIM}")
        t.append(text[:80], style=f"italic {DIM}")
        return t, 1
    if kind == "error":
        return Panel(Text(text, style=BAD), title=arrow_title(src, dst, "error"), title_align="left",
                     border_style=BAD, box=box.ROUNDED), est(text, width) + 2
    t = Text(no_wrap=True, overflow="ellipsis")
    t.append(src, style=f"bold {c}")
    t.append(f"  · {kind}: {text}", style=DIM)
    return t, 1


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
        if used + h > budget and rows:
            left = budget - used
            if console is not None and left >= 2:      # fill the gap with the bottom of the older item
                rows.append((ev, Tail(r, left, item_w)))
            elif console is not None and left == 1:    # keep the newest event on the bottom edge
                rows.append(({}, Text("")))
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
            g.add_row("", Text("no events yet. Ask Claude Code in the other pane for something.",
                               style=f"italic {DIM}"))
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


def build(m: Model, console: Console, label: str = ""):
    now = time.time()
    w, h = console.size.width, console.size.height
    if PROJECT:
        label = f"{PROJECT} · {label}" if label else PROJECT
    if m.focus:
        label = f"{label} · {m.focus}" if label else m.focus
    layout = Layout()
    layout.split_column(Layout(name="header", size=HEADER_H), Layout(name="body", ratio=1), Layout(name="footer", size=1))
    agents_w = agents_width(w)
    layout["body"].split_row(Layout(name="agents", size=agents_w), Layout(name="feed", ratio=1))
    layout["header"].update(render_header(m, label))
    layout["agents"].update(render_agents(m, now))
    layout["feed"].update(render_feed(m, max(5, h - 3 - 1 - 2), w - agents_w, console))
    layout["footer"].update(render_footer(m, label))
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


def handle(m: Model, events: list[tuple], width: int) -> bool:
    """Apply input events to the model. Returns False when the user asked to quit."""
    agents_w = agents_width(width)
    for e in events:
        if e[0] == "mouse":
            _, btn, x, y, pressed = e
            if btn == 64:
                m.scroll_by(3)
            elif btn == 65:
                m.scroll_by(-3)
            elif pressed and btn == 0:
                m.click(x, y, agents_w)
            continue
        key = e[1]
        if key in ("q", "Q", "ctrl-c"):
            return False
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


def snapshot(events_path: str, out: str, width: int, height: int, focus: str | None = None, scroll: int = 0) -> None:
    console = Console(record=True, width=width, height=height, force_terminal=True, color_system="truecolor",
                      file=io.StringIO())
    m = Model()
    for ev in Follower(events_path).poll():
        m.apply(ev)
    m.set_focus(focus)
    m.scroll_by(scroll)
    console.print(build(m, console, "snapshot"))
    console.save_svg(out, title=f"crew · {PROJECT}" if PROJECT else "crew")
    print(f"wrote {out} ({m.count} events)")


def run(events: str | None = None, replay: str | None = None, speed: float = 1.0) -> int:
    """Follow an events file live, or replay a recording, until q."""
    label = f"replay ×{speed:g}" if replay else "live"
    source = ReplaySource(replay, speed) if replay else FileSource(events)
    console = Console()
    m = Model()
    if not replay:
        for ev in source.poll():        # show history that is already in the file
            m.apply(ev)
    with Keys() as keys, Live(build(m, console, label), console=console, screen=True, refresh_per_second=12) as live:
        try:
            while True:
                for ev in source.poll():
                    m.apply(ev)
                if not handle(m, keys.poll(), console.size.width):
                    break
                lbl = label + (" · finished" if source.done and replay else "")
                live.update(build(m, console, lbl))
                time.sleep(0.08)
        except KeyboardInterrupt:
            pass
    return 0
