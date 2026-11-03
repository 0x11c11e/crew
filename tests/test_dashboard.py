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
        self.saved = (dict(D.PALETTE), dict(D.ROLES), D.ARCHITECT, D.PROJECT)

    def tearDown(self):
        D.PALETTE.clear(); D.PALETTE.update(self.saved[0])
        D.ROLES.clear(); D.ROLES.update(self.saved[1])
        D.ARCHITECT, D.PROJECT = self.saved[2], self.saved[3]

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


class Replay(unittest.TestCase):
    def test_sample_renders_without_config(self):
        evs = read_events(SAMPLE)
        self.assertGreater(len(evs), 50)
        m = D.Model()
        for ev in evs:
            m.apply(ev)
        self.assertEqual(list(m.agents)[:2], ["fable", "user"])
        self.assertIn("worker-1", m.agents)
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
