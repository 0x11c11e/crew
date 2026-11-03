import io
import os
import tempfile
import unittest

from helpers import SAMPLE, TempProject

from rich.console import Console

from crew import config, dashboard as D
from crew.bus import read_events


def render(m, width=150, height=45):
    c = Console(record=True, width=width, height=height, force_terminal=True, color_system="truecolor",
                file=io.StringIO())
    c.print(D.build(m, c, "test"))
    return c.export_text().splitlines()


def model_with(names):
    m = D.Model()
    for n in names:
        m.apply({"ts": 1, "src": n, "kind": "connect", "text": f"{n} online"})
    return m


class Keys(unittest.TestCase):
    def test_parse_keys(self):
        got = D.parse_keys(b"1\x1b[A\x1b[B\x1bOA\x1b[5~\x1b[6~\x1b[F\x1b[H\x1b[Z\tq\x03")
        self.assertEqual([e[1] for e in got], ["1", "up", "down", "up", "pgup", "pgdn", "end", "home", "btab",
                                               "tab", "q", "ctrl-c"])

    def test_parse_mouse_and_lone_escape(self):
        got = D.parse_keys(b"\x1b[<0;10;7M\x1b[<0;10;7m\x1b[<64;3;4M\x1b")
        self.assertEqual(got, [("mouse", 0, 10, 7, True), ("mouse", 0, 10, 7, False), ("mouse", 64, 3, 4, True),
                               ("key", "esc")])

    def test_handle(self):
        m = model_with(["fable", "worker-1", "worker-2"])
        self.assertTrue(D.handle(m, [("key", "2")], 150))
        self.assertEqual(m.focus, "worker-1")
        D.handle(m, [("key", "2")], 150)
        self.assertIsNone(m.focus)
        D.handle(m, [("key", "tab"), ("key", "tab")], 150)
        self.assertEqual(m.focus, "worker-1")
        D.handle(m, [("key", "esc")], 150)
        self.assertIsNone(m.focus)
        self.assertFalse(D.handle(m, [("key", "q")], 150))


class Geometry(unittest.TestCase):
    def test_cards_are_where_clicks_expect_them(self):
        names = ["fable", "worker-1", "worker-2"]
        m = model_with(names)
        lines = render(m)
        for i, n in enumerate(names):
            row = lines[D.CARD_TOP - 1 + i * D.CARD_H]           # CARD_TOP is 1-based
            self.assertIn(f" {i + 1} ", row)
            self.assertIn(n, row)
            self.assertTrue(row.lstrip("│ ").startswith("╭"), row)

    def test_click_hit_testing(self):
        m = model_with(["fable", "worker-1", "worker-2"])
        aw = D.agents_width(150)
        m.click(5, D.CARD_TOP + D.CARD_H + 1, aw)                 # inside the second card
        self.assertEqual(m.focus, "worker-1")
        m.click(5, D.CARD_TOP + D.CARD_H + 3, aw)                 # its bottom border: toggles off
        self.assertIsNone(m.focus)
        m.click(aw + 2, D.CARD_TOP, aw)                           # in the feed: nothing
        self.assertIsNone(m.focus)
        m.click(5, D.CARD_TOP - 1, aw)                            # above the cards: nothing
        self.assertIsNone(m.focus)
        m.click(5, D.CARD_TOP + 10 * D.CARD_H, aw)                # below the last card: nothing
        self.assertIsNone(m.focus)

    def test_narrow_terminal_renders(self):
        render(model_with(["fable", "worker-1"]), width=100, height=30)


class Feed(unittest.TestCase):
    def test_focus_filter(self):
        m = model_with(["fable", "worker-1", "worker-2"])
        m.apply({"ts": 2, "src": "fable", "dst": "worker-1", "kind": "task", "text": "a"})
        m.apply({"ts": 3, "src": "fable", "dst": "worker-2", "kind": "task", "text": "b"})
        m.apply({"ts": 4, "src": "worker-2", "kind": "exec", "text": "ls"})
        m.set_focus("worker-2")
        self.assertEqual([e["text"] for e in m.visible()], ["worker-2 online", "b", "ls"])
        m.set_focus(None)
        self.assertEqual(len(m.visible()), 6)

    def test_scroll_bounds(self):
        m = model_with(["a", "b"])
        m.scroll_by(100)
        self.assertEqual(m.scroll, 1)
        m.scroll_by(-100)
        self.assertEqual(m.scroll, 0)

    def test_result_replaces_identical_message(self):
        m = model_with(["fable", "worker-1"])
        m.apply({"ts": 2, "src": "worker-1", "dst": "fable", "kind": "message", "text": "done"})
        m.apply({"ts": 3, "src": "worker-1", "dst": "fable", "kind": "result", "text": "done",
                 "data": {"exit_code": 0, "n": 1, "seconds": 3}})
        self.assertEqual([e["kind"] for e in m.feed][-1:], ["result"])
        self.assertEqual(sum(1 for e in m.feed if e["kind"] == "message"), 0)
        self.assertEqual(m.agents["worker-1"].state, "done")
        self.assertEqual(m.agents["fable"].state, "thinking")


class Cards(unittest.TestCase):
    """What a stranger reads off the screen: one row per command, real elapsed times, the task and
    the model on each card, and totals in the header."""

    def test_finished_command_replaces_its_started_row(self):
        m = model_with(["fable", "worker-1"])
        m.apply({"ts": 2, "src": "worker-1", "kind": "exec", "text": "ls", "data": {"item": "i1"}})
        m.apply({"ts": 3, "src": "worker-1", "kind": "exec", "text": "pwd", "data": {"item": "i2"}})
        m.apply({"ts": 4, "src": "worker-1", "kind": "exec_done", "text": "ls", "data": {"item": "i1", "exit_code": 0}})
        self.assertEqual([(e["kind"], e["text"]) for e in m.feed[-2:]], [("exec", "pwd"), ("exec_done", "ls")])
        m.apply({"ts": 5, "src": "fable", "kind": "exec", "text": "git status"})              # the hook: no item ids
        m.apply({"ts": 6, "src": "fable", "kind": "exec_done", "text": "git status", "data": {"exit_code": 1}})
        self.assertEqual([e["kind"] for e in m.feed if e["src"] == "fable"][-1:], ["exec_done"])
        self.assertEqual(sum(1 for e in m.feed if e["kind"] == "exec"), 1)

    def test_elapsed_comes_from_event_time(self):
        m = D.Model()
        m.apply({"ts": 1000, "src": "fable", "kind": "connect", "text": "fable online"})
        m.apply({"ts": 1000, "src": "fable", "dst": "user", "kind": "status", "text": "waiting for you"})
        c = Console(record=True, width=150, height=45, force_terminal=True, color_system="truecolor", file=io.StringIO())
        c.print(D.build(m, c, "test", now=1000 + 4 * 60 + 12))
        text = c.export_text()
        self.assertIn("4m12s", text)
        self.assertIn("waiting for you", text)

    def test_user_is_not_a_card_and_numbers_stay_contiguous(self):
        m = D.Model()
        m.apply({"ts": 1, "src": "fable", "kind": "connect", "text": "fable online"})
        m.apply({"ts": 2, "src": "user", "dst": "fable", "kind": "prompt", "text": "add a --high flag"})
        m.apply({"ts": 3, "src": "worker-1", "kind": "connect", "text": "worker-1 online"})
        self.assertEqual([(a.name, a.num) for a in m.order()], [("fable", 1), ("worker-1", 2)])
        self.assertEqual(m.agents["fable"].state, "thinking")
        lines = render(m)
        self.assertTrue(any("you ▶ fable" in ln for ln in lines))
        self.assertFalse(any(" 3 " in ln and "user" in ln for ln in lines))

    def test_card_shows_task_model_and_verdict(self):
        m = model_with(["fable", "worker-1"])
        m.apply({"ts": 2, "src": "fable", "dst": "worker-1", "kind": "task", "text": "# Add priorities\n\nbody",
                 "data": {"n": 3, "kind": "codex", "model": "gpt-6-astra", "read_only": True}})
        m.apply({"ts": 3, "src": "fable", "dst": "user", "kind": "status", "text": "waiting for you",
                 "data": {"model": "claude-opus-5-5"}})
        m.apply({"ts": 4, "src": "worker-1", "kind": "usage", "data": {"input_tokens": 2_000_000, "output_tokens": 400_000}})
        m.apply({"ts": 5, "src": "worker-1", "dst": "fable", "kind": "result", "text": "done",
                 "data": {"exit_code": 0, "n": 3, "seconds": 95, "cost_usd": 0.5}})
        m.apply({"ts": 6, "src": "fable", "dst": "worker-1", "kind": "verify", "text": "ok", "data": {"verdict": "pass"}})
        w, f = m.agents["worker-1"], m.agents["fable"]
        self.assertEqual((w.task, w.task_n, w.model, w.kind, w.verdict), ("Add priorities", 3, "gpt-6-astra", "Codex", "pass"))
        self.assertEqual(f.model, "claude-opus-5-5")
        self.assertEqual(f.waiting, [])
        self.assertEqual((m.tasks, m.results, m.passes, m.fails, m.tokens, m.cost), (1, 1, 1, 0, 2_400_000, 0.5))
        text = "\n".join(render(m))
        for needle in ("task 3  Add priorities", "Codex · gpt-6-astra", "Claude Code · claude-opus-5-5",
                       "2.4M tokens", "PASS", "pass 1", "$0.50", "read-only", "done in 1m35s"):
            self.assertIn(needle, text, needle)

    def test_k(self):
        self.assertEqual([D.k(n) for n in (0, 999, 1000, 9_999, 10_000, 206_400, 2_292_000)],
                         ["0", "999", "1.0k", "10.0k", "10k", "206k", "2.3M"])

    def test_empty_feed_explains_the_screen(self):
        text = "\n".join(render(D.Model()))
        self.assertIn("How to read this screen", text)
        self.assertIn("nobody online yet", text)


class BottomEdge(unittest.TestCase):
    """The newest event must always be visible on the feed's last row (regression: tall Markdown
    items were under-estimated, so the panel cropped the newest events off the bottom)."""

    TALL = "Report:\n\n" + "\n".join(f"- point {i}: " + "word " * 18 for i in range(12)) + \
        "\n\n## Heading\n\n```\ncode\n```\n"
    # Medium items whose headings render taller than a line-count estimate: each fits on its own,
    # so the old estimate packed in too many and the panel cut the newest rows off.
    HEADINGS = "## One\n\nfirst\n\n## Two\n\nsecond\n\n## Three\n\nthird"

    def feed_rows(self, m, width=105, height=40):
        lines = render(m, width, height)
        off = D.agents_width(width)
        body = [ln[off:] for ln in lines[D.HEADER_H + 1: height - 2]]   # inside the feed panel
        return [ln.strip().strip("│").strip() for ln in body]

    def test_newest_on_bottom_row(self):
        m = model_with(["fable", "worker-1"])
        for i in range(4):
            m.apply({"ts": 10 + i, "src": "fable", "dst": "user", "kind": "message", "text": self.TALL})
        m.apply({"ts": 20, "src": "fable", "dst": "user", "kind": "status", "text": "waiting for you"})
        rows = self.feed_rows(m)
        self.assertIn("waiting for you", rows[-1])
        self.assertIn("earlier lines above", rows[0])         # the older tall item is trimmed at the top

    def test_headings_do_not_push_the_newest_off_screen(self):
        m = model_with(["fable"])
        for i in range(3):
            m.apply({"ts": 10 + i, "src": "fable", "kind": "message", "text": self.HEADINGS})
        m.apply({"ts": 20, "src": "fable", "kind": "status", "text": "the newest event"})
        rows = self.feed_rows(m)
        self.assertIn("the newest event", rows[-1])

    def test_newest_item_taller_than_the_feed_shows_its_end(self):
        m = model_with(["fable"])
        m.apply({"ts": 10, "src": "fable", "kind": "message", "text": self.TALL * 3 + "\nthe last line"})
        rows = self.feed_rows(m, height=24)
        self.assertIn("earlier lines above", rows[0])
        self.assertTrue(any("the last line" in r for r in rows[-3:]), rows[-3:])

    def test_scrolled_back_still_fills_to_the_bottom(self):
        m = model_with(["fable"])
        for i in range(6):
            m.apply({"ts": 10 + i, "src": "fable", "kind": "message", "text": self.TALL + f"\nend {i}"})
        m.scroll_by(2)
        rows = self.feed_rows(m)
        self.assertTrue(rows[-1], "blank row under the newest visible item")

    def test_wheel_scrolls_back_and_forth(self):
        m = model_with(["a", "b", "c", "d"])
        D.handle(m, [("mouse", 64, 80, 20, True)], 150)       # wheel up: older
        self.assertEqual(m.scroll, 3)
        D.handle(m, [("mouse", 65, 80, 20, True)], 150)       # wheel down: back to newest
        self.assertEqual(m.scroll, 0)
        D.handle(m, [("key", "up"), ("key", "up"), ("key", "end")], 150)
        self.assertEqual(m.scroll, 0)


class Roles(unittest.TestCase):
    def setUp(self):
        self.saved = (dict(D.PALETTE), dict(D.ROLES), D.ARCHITECT, D.PROJECT, dict(D.KIND), dict(D.MODELS))

    def tearDown(self):
        D.PALETTE.clear(); D.PALETTE.update(self.saved[0])
        D.ROLES.clear(); D.ROLES.update(self.saved[1])
        D.ARCHITECT, D.PROJECT = self.saved[2], self.saved[3]
        D.KIND.clear(); D.KIND.update(self.saved[4])
        D.MODELS.clear(); D.MODELS.update(self.saved[5])

    def test_without_config(self):
        self.assertEqual(D.role("fable"), "architect · Claude Code")
        self.assertEqual(D.role("worker-7"), "worker · Codex")
        self.assertEqual(D.role("user"), "you")

    def test_from_config(self):
        toml = ('name = "deskfish"\n[architect]\nname = "boss"\nmodel = "fable"\n'
                '[[workers]]\nname = "worker-1"\nmodel = "gpt-6-astra"\n'
                '[[workers]]\nname = "worker-2"\nkind = "claude"\nmodel = "opus"\n')
        with TempProject(toml) as root:
            D.configure(config.load(root))
        self.assertEqual(D.role("boss"), "architect · Claude Code · fable")
        self.assertEqual(D.role("worker-1"), "worker · Codex · gpt-6-astra")
        self.assertEqual(D.role("worker-2"), "worker · Claude · opus")
        self.assertEqual(D.color("boss"), D.ACCENT)
        self.assertNotEqual(D.color("worker-1"), D.color("worker-2"))
        m = D.Model()
        m.apply({"ts": 1, "src": "user", "dst": None, "kind": "prompt", "text": "hi"})
        self.assertEqual(m.agents["boss"].state, "thinking")       # prompts default to the architect
        lines = render(m)
        self.assertIn("deskfish", lines[1])


def sample_model():
    m = D.Model()
    for ev in read_events(SAMPLE):
        m.apply(ev)
    return m


def plain(r, width=120):
    c = Console(record=True, width=width, force_terminal=True, color_system="truecolor", file=io.StringIO(),
                theme=D.THEME)
    c.print(r)
    return c.export_text()


class Spacing(unittest.TestCase):
    """Slides showed "fablewaiting" and "test_todo.pyif": librsvg drops the &#160; at either end of a text
    run in Rich's SVG. The terminal text was always right; the snapshot moves those spaces into x."""

    def test_text_keeps_its_spaces(self):
        text = "\n".join(render(sample_model()))
        for needle in ("fable waiting for you", "worker-1 idle", "test_todo.py if it exists", "list format"):
            self.assertIn(needle, text)

    def test_svg_runs_have_no_edge_spaces(self):
        run = '<text class="r1" x="10" y="5" textLength="{}" clip-path="url(#c)">{}</text>'
        svg = run.format(40, "ab&#160;&#160;") + run.format(40, "&#160;cd&#160;")
        got = D.svg_spaces(svg)
        self.assertIn('x="10" y="5" textLength="20" clip-path="url(#c)">ab</text>', got)
        self.assertIn('x="20" y="5" textLength="20" clip-path="url(#c)">cd</text>', got)
        self.assertEqual(D.svg_spaces(run.format(20, "&#160;&#160;")), run.format(20, "&#160;&#160;"))

    def test_touching_runs_of_one_style_merge(self):
        run = '<text class="{}" x="{}" y="5" textLength="20" clip-path="url(#c)">{}</text>'
        svg = run.format("r1", 0, "──") + run.format("r1", 20, "──") + run.format("r2", 40, "ab")
        got = D.svg_merge(svg)
        self.assertEqual(got.count("<text"), 2)
        self.assertIn('textLength="40" clip-path="url(#c)">────</text>', got)

    def test_snapshot_has_no_glued_runs(self):
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "frame.svg")
            with open(os.devnull, "w") as dn:
                import contextlib
                with contextlib.redirect_stdout(dn):
                    D.snapshot(str(SAMPLE), out, 150, 45)
            with open(out) as fh:
                svg = fh.read()
        self.assertNotRegex(svg, r'textLength="[^"]*"[^>]*>&#160;')
        self.assertNotRegex(svg, r'&#160;</text>')


class Tiers(unittest.TestCase):
    def test_trim_output(self):
        out = "first\n\n----------\n=====\nRan 9 tests in 0.9s\n\nOK\nlast\n"
        self.assertEqual(D.trim_output(out), (["Ran 9 tests in 0.9s", "OK", "last"], 1))
        self.assertEqual(D.trim_output("a\n\nb"), (["a", "b"], 0))
        self.assertEqual(D.trim_output(""), ([], 0))

    def test_command_output_is_trimmed_and_counted(self):
        ev = {"src": "worker-1", "kind": "exec_done", "text": "pytest",
              "data": {"exit_code": 0, "output": "\n".join(f"line {i}" for i in range(10)) + "\n-----\n"}}
        r, h = D.feed_item(ev, 100)
        text = plain(r)
        self.assertEqual(h, 4)
        self.assertIn("✔ $ pytest", text)
        self.assertIn("line 9   (+7 lines)", text)
        self.assertNotIn("line 6", text)
        self.assertNotIn("-----", text)

    def test_failed_command_stands_out(self):
        ev = {"src": "worker-1", "kind": "exec_done", "text": "pytest", "data": {"exit_code": 2, "output": "boom"}}
        r, _ = D.feed_item(ev, 100)
        c = Console(record=True, width=100, force_terminal=True, color_system="truecolor", file=io.StringIO())
        c.print(r)
        self.assertIn("✘ exit 2 $ pytest", c.export_text())
        boom = [seg for seg in c.render(r) if seg.text == "boom"]
        self.assertTrue(boom and boom[0].style.color is None, "a failed command's output is at normal weight")

    def test_boxed_kinds_carry_their_label(self):
        evs = [{"src": "user", "dst": "fable", "kind": "prompt", "text": "hi"},
               {"src": "fable", "dst": "worker-1", "kind": "task", "text": "# Task: add `x`\n\nbody",
                "data": {"n": 2}},
               {"src": "worker-1", "dst": "fable", "kind": "result", "text": "ok",
                "data": {"n": 2, "exit_code": 0}},
               {"src": "fable", "dst": "worker-1", "kind": "verify", "text": "good", "data": {"verdict": "fail"}},
               {"src": "fable", "dst": "user", "kind": "message", "text": "done"}]
        texts = [plain(D.feed_item(ev, 100)[0]) for ev in evs]
        for text, needle in zip(texts, ("you ▶ fable", "TASK 2 ▶ worker-1  ·  add x", "RESULT 2 ◀ worker-1",
                                        "FAIL ▶ worker-1", "fable ▶ you")):
            self.assertIn(needle, text)
            self.assertTrue(text.lstrip().startswith("┎"), text)
        self.assertNotIn("┎", plain(D.feed_item({"src": "worker-1", "dst": "fable", "kind": "message",
                                                  "text": "on it"}, 100)[0]))

    def test_inline_code_has_no_background(self):
        c = Console(width=80, force_terminal=True, color_system="truecolor", file=io.StringIO(), theme=D.THEME)
        segs = [seg for seg in c.render(D.md("run `pytest` now")) if seg.text == "pytest"]
        self.assertTrue(segs)
        self.assertIsNone(segs[0].style.bgcolor)
        self.assertEqual(segs[0].style.color.name.lower(), D.CODE.lower())

    def test_feed_shows_as_many_events_as_before(self):
        import re
        lines = render(sample_model())
        off = D.agents_width(150)
        rows = [ln[off:].strip().strip("│").strip() for ln in lines[D.HEADER_H + 1: 45 - 2]]
        self.assertGreaterEqual(sum(1 for r in rows if re.match(r"\d\d:\d\d:\d\d", r)), 12)


class States(unittest.TestCase):
    def test_dot_color(self):
        cases = [("worker-1", "running", D.OK), ("worker-1", "writing", D.OK), ("worker-1", "busy", D.OK),
                 ("fable", "thinking", D.ACCENT), ("fable", "idle", D.WAIT), ("worker-1", "idle", D.WAIT),
                 ("worker-1", "done", D.WAIT), ("worker-1", "failed", D.BAD), ("worker-1", "offline", D.DIM)]
        for name, state, want in cases:
            a = D.Agent(name)
            a.state = state
            self.assertEqual(D.dot_color(a), want, state)

    def test_card_shows_the_running_command(self):
        m = model_with(["fable", "worker-1"])
        m.apply({"ts": 2, "src": "worker-1", "kind": "exec", "text": "python3 -m unittest -v"})
        self.assertIn("$ python3 -m unittest -v", "\n".join(render(m)))
        m.apply({"ts": 3, "src": "worker-1", "kind": "edit", "text": "todo.py"})
        self.assertEqual((m.agents["worker-1"].state, D.dot_color(m.agents["worker-1"])), ("writing", D.OK))


class Ledger(unittest.TestCase):
    def test_rows_follow_the_task(self):
        m = model_with(["fable", "worker-1"])
        m.apply({"ts": 2, "src": "fable", "dst": "worker-1", "kind": "task", "text": "# Task: add `--high`",
                 "data": {"n": 2}})
        row = m.ledger[("worker-1", 2)]
        self.assertEqual((row["title"], row["state"]), ("add `--high`", "queued"))
        m.apply({"ts": 3, "src": "worker-1", "kind": "status", "text": "starting codex…"})
        self.assertEqual(row["state"], "running")
        m.apply({"ts": 4, "src": "worker-1", "dst": "fable", "kind": "result", "text": "ok",
                 "data": {"n": 2, "exit_code": 0, "seconds": 23, "tokens": 133964}})
        self.assertEqual((row["state"], row["seconds"], row["tokens"]), ("done", 23, 133964))
        m.apply({"ts": 5, "src": "fable", "dst": "worker-1", "kind": "verify", "text": "ok",
                 "data": {"verdict": "pass"}})
        self.assertEqual(row["state"], "pass")
        m.apply({"ts": 6, "src": "fable", "dst": "worker-1", "kind": "task", "text": "again", "data": {"n": 3}})
        m.apply({"ts": 7, "src": "worker-1", "dst": "fable", "kind": "result", "text": "no",
                 "data": {"n": 3, "exit_code": 1, "seconds": 5}})
        self.assertEqual([r["state"] for r in m.ledger.values()], ["pass", "fail"])

    def test_panel_rows_newest_last(self):
        lines = render(sample_model())
        rows = [ln for ln in lines if ln.lstrip("│ ").startswith("#")]
        self.assertEqual([r.lstrip("│ ").split()[:2] for r in rows],
                         [["#1", "worker-1"], ["#2", "worker-2"], ["#3", "worker-1"]])     # dispatch order
        for needle in ("#3", "worker-1", "Follow-up: add prio", "PASS", "23s", "133k"):
            self.assertIn(needle, rows[-1])
        self.assertIn("tasks", "\n".join(lines))

    def test_panel_left_out_when_short_or_empty(self):
        self.assertFalse(any(ln.lstrip("│ ").startswith("#") for ln in render(sample_model(), 100, 30)))
        self.assertFalse(any("─ tasks ─" in ln for ln in render(model_with(["fable", "worker-1"]))))


class Header(unittest.TestCase):
    def test_line_two(self):
        lines = render(sample_model())
        self.assertNotIn("one architect", "\n".join(lines))
        self.assertIn("70s elapsed  ·  64 events", lines[1])
        for needle in ("● fable waiting for you", "● worker-1 idle", "● worker-2 idle",
                       "tasks 3  ·  done 3  ·  pass 3  ·  fail 0  ·  278k tokens"):
            self.assertIn(needle, lines[2], needle)

    def test_fail_count_is_red(self):
        m = model_with(["fable", "worker-1"])
        m.apply({"ts": 2, "src": "fable", "dst": "worker-1", "kind": "verify", "text": "no",
                 "data": {"verdict": "fail"}})
        t = D.totals(m)
        span = next(sp for sp in t.spans if t.plain[sp.start:sp.end] == "1")
        self.assertIn(D.BAD, str(span.style))
        self.assertNotIn(D.BAD, str(D.totals(model_with(["fable"])).spans))


class Strip(unittest.TestCase):
    """architect | strip | worker: the strip shows only the exchange between them and their states."""

    def setUp(self):
        self.saved = (dict(D.PALETTE), dict(D.ROLES), D.ARCHITECT, D.PROJECT, dict(D.KIND), dict(D.MODELS),
                      D.LAYOUT)

    def tearDown(self):
        D.PALETTE.clear(); D.PALETTE.update(self.saved[0])
        D.ROLES.clear(); D.ROLES.update(self.saved[1])
        D.ARCHITECT, D.PROJECT = self.saved[2], self.saved[3]
        D.KIND.clear(); D.KIND.update(self.saved[4])
        D.MODELS.clear(); D.MODELS.update(self.saved[5])
        D.LAYOUT = self.saved[6]

    def strip(self, m, width=48, height=45):
        c = Console(record=True, width=width, height=height, force_terminal=True, color_system="truecolor",
                    file=io.StringIO())
        c.print(D.build(m, c, now=m.t_last, layout="strip"))
        return c.export_text()

    def test_sample_in_48_columns(self):
        text = self.strip(sample_model())
        lines = text.splitlines()
        self.assertEqual(len(lines), 45)
        self.assertTrue(all(len(ln) <= 48 for ln in lines))
        for needle in ("fable", "worker-1", "#1", "TASK 2 ▶ worker-1", "RESULT 2 ◀ worker-1", "PASS ▶ worker-1",
                       "278k tokens", "q quit"):
            self.assertIn(needle, text, needle)
        self.assertIn("fable · waiting for you", lines[-3])            # the newest, on the bottom row
        self.assertNotIn("$ ", text)                                   # no commands
        for prompt in ("Build me a small", "Nice. Now add priorities", "Both workers passed", "Plan:"):
            self.assertNotIn(prompt, text)                             # the left pane shows these

    def test_entries_are_at_most_three_lines(self):
        m = model_with(["fable", "worker-1"])
        long = "word " * 80
        m.apply({"ts": 2, "src": "fable", "dst": "worker-1", "kind": "task", "text": long, "data": {"n": 1}})
        m.apply({"ts": 3, "src": "worker-1", "kind": "exec", "text": "make test"})
        m.apply({"ts": 4, "src": "worker-1", "dst": "fable", "kind": "result", "text": long,
                 "data": {"n": 1, "exit_code": 0, "seconds": 5}})
        for ev in m.feed:
            lines = D.strip_lines(ev, 34)
            self.assertTrue(lines is None or len(lines) <= 3, ev["kind"])
        self.assertIsNone(D.strip_lines({"kind": "exec", "src": "worker-1", "text": "ls"}, 34))
        self.assertIsNone(D.strip_lines({"kind": "prompt", "src": "user", "text": "hi"}, 34))

    def test_running_command_on_the_worker_card(self):
        m = model_with(["fable", "worker-1"])
        m.apply({"ts": 2, "src": "worker-1", "kind": "exec", "text": "python3 -m unittest"})
        self.assertIn("$ python3 -m unittest", self.strip(m))

    def test_layout_from_config_and_the_flag_overrides_it(self):
        from crew import cli
        from helpers import quiet
        with TempProject('[[workers]]\nname = "worker-1"\n') as root:
            self.assertEqual(config.load(root).layout, "strip")       # one worker: strip by default
            for flag, want, other in ((None, "exchange", "conversation"), ("wide", "conversation", "exchange")):
                argv = ["snapshot", "f.svg", "--replay", str(SAMPLE), "--project", str(root)]
                rc, _, err = quiet(cli.main, argv + (["--layout", flag] if flag else []))
                self.assertEqual(rc, 0, err)
                svg = (root / "f.svg").read_text()
                self.assertIn(want, svg, flag)
                self.assertNotIn(other, svg, flag)


class Replay(unittest.TestCase):
    def test_sample_renders_without_config(self):
        evs = read_events(SAMPLE)
        self.assertGreater(len(evs), 50)
        m = D.Model()
        for ev in evs:
            m.apply(ev)
        self.assertEqual(list(m.agents)[:2], ["fable", "worker-1"])
        self.assertNotIn("user", m.agents)                        # the human is in the feed, not a card
        m.set_focus("worker-1")
        self.assertTrue(any("worker-1" in ln for ln in render(m)))

    def test_snapshot_writes_svg(self):
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "frame.svg")
            with open(os.devnull, "w") as dn:
                import contextlib
                with contextlib.redirect_stdout(dn):
                    D.snapshot(str(SAMPLE), out, 150, 45, focus="worker-1")
            with open(out) as fh:
                self.assertTrue(fh.read().lstrip().startswith("<svg"))

    def test_replay_source_paces_and_finishes(self):
        src = D.ReplaySource(str(SAMPLE), speed=1000)
        src.next_at = 0
        got = []
        for _ in range(10000):
            got += src.poll()
            if src.done:
                break
            src.next_at = 0
        self.assertTrue(src.done)
        self.assertEqual(len(got), len(read_events(SAMPLE)))


if __name__ == "__main__":
    unittest.main()
