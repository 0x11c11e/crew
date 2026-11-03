"""The worker runner, driven by fake `codex` and `claude` scripts that replay the fixtures."""
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from helpers import FIXTURES, TempProject, quiet

from crew import cli, config
from crew.bus import Bus, read_events
from crew.worker import load_state

FAKE_CODEX = """#!/usr/bin/env bash
out=""; prev=""; for a in "$@"; do [ "$prev" = "-o" ] && out="$a"; prev="$a"; done
echo "$@" > "$FAKE_LOG"
echo "plain text on stderr" >&2
cat "$FIXTURE"
[ -n "$FAKE_SLEEP" ] && sleep "$FAKE_SLEEP"
printf 'pong\\n' > "$out"
[ -n "$CREW_WORKER" ] || exit 9
"""
FAKE_CLAUDE = """#!/usr/bin/env bash
echo "$@" > "$FAKE_LOG"
cat "$FIXTURE"
"""


class Runner(unittest.TestCase):
    def setUp(self):
        self.bin = tempfile.TemporaryDirectory()
        b = Path(self.bin.name)
        for name, body in (("codex", FAKE_CODEX), ("claude", FAKE_CLAUDE)):
            (b / name).write_text(body)
            (b / name).chmod(0o755)
        self.log = b / "args"
        self.env = {"PATH": f"{b}:{os.environ['PATH']}", "FAKE_LOG": str(self.log)}
        self.saved = {k: os.environ.get(k) for k in ("PATH", "FAKE_LOG", "FIXTURE", "FAKE_SLEEP")}
        os.environ.update(self.env)

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.bin.cleanup()

    def test_codex_run_and_continue(self):
        os.environ["FIXTURE"] = str(FIXTURES / "codex-stream.jsonl")
        with TempProject('[[workers]]\nname = "worker-1"\n') as root:
            rc, out, _ = quiet(cli.main, ["run", "worker-1", "--task", "reply with the word pong"])
            self.assertEqual(rc, 0, out)
            wdir = root / ".crew" / "workers" / "worker-1"
            self.assertIn(f"result file: {wdir / 'task-01.result.md'}", out)
            self.assertTrue(out.rstrip().endswith("pong"))
            self.assertEqual((wdir / "task-01.md").read_text(), "reply with the word pong")
            self.assertEqual((wdir / "task-01.result.md").read_text(), "pong\n")
            self.assertIn("plain text on stderr", (wdir / "task-01.raw.jsonl").read_text())
            st = load_state(config.load(root), "worker-1")
            self.assertEqual((st["tasks"], st["kind"], st["last_exit"]), (1, "codex", 0))
            self.assertEqual(st["thread_id"], "01a19eeb-7959-79b3-9d4e-5bd46f1e9ed3")
            evs = read_events(Bus(root).events)
            kinds = [e["kind"] for e in evs]
            self.assertEqual(kinds[:3], ["connect", "task", "status"])
            self.assertEqual(kinds[-2:], ["result", "status"])
            task = evs[1]
            self.assertEqual((task["src"], task["dst"]), ("fable", "worker-1"))
            self.assertEqual(task["data"]["kind"], "codex")
            self.assertFalse(task["data"]["resume"])
            res = evs[-2]
            self.assertEqual(res["text"], "pong")
            self.assertEqual((res["data"]["exit_code"], res["data"]["commands"], res["data"]["n"]), (0, 3, 1))
            self.assertEqual(res["data"]["tokens"], 72390)
            self.assertEqual(evs[-1]["text"], "idle")

            rc, out, _ = quiet(cli.main, ["run", "worker-1", "--continue", "--task", "again"])
            self.assertEqual(rc, 0)
            args = self.log.read_text().split()
            self.assertEqual(args[-3:], ["resume", "01a19eeb-7959-79b3-9d4e-5bd46f1e9ed3", "again"])
            self.assertTrue((wdir / "task-02.result.md").exists())
            evs = read_events(Bus(root).events)
            self.assertEqual(sum(e["kind"] == "connect" for e in evs), 1)
            self.assertTrue([e for e in evs if e["kind"] == "task"][-1]["data"]["resume"])

    def test_timeout(self):
        os.environ["FIXTURE"] = "/dev/null"
        os.environ["FAKE_SLEEP"] = "20"
        with TempProject('[[workers]]\nname = "w"\ntimeout = 1\n') as root:
            rc, out, _ = quiet(cli.main, ["run", "w", "--task", "slow"])
            self.assertEqual(rc, 124)
            evs = read_events(Bus(root).events)
            self.assertTrue(any(e["kind"] == "error" and "timed out" in e["text"] for e in evs))
            self.assertTrue(evs[-1]["text"].startswith("failed"))

    def test_claude_run(self):
        os.environ["FIXTURE"] = str(FIXTURES / "claude-stream.jsonl")
        with TempProject('[[workers]]\nname = "w2"\nkind = "claude"\nmodel = "opus"\n') as root:
            rc, out, _ = quiet(cli.main, ["run", "w2", "--task", "say CREW"])
            self.assertEqual(rc, 0, out)
            args = self.log.read_text()
            self.assertTrue(args.startswith("-p say CREW --output-format stream-json --verbose"))
            evs = read_events(Bus(root).events)
            res = [e for e in evs if e["kind"] == "result"][0]
            self.assertEqual(res["data"]["thread_id"], "212538b4-ae9e-4251-91e1-abba469ad07b")
            self.assertEqual(res["data"]["exit_code"], 0)
            self.assertIn("cost_usd", res["data"])
            self.assertTrue(res["text"].startswith("CREW: I created hello.txt"))

    def test_unknown_worker_and_empty_task(self):
        with TempProject('[[workers]]\nname = "w"\n'):
            self.assertEqual(quiet(cli.main, ["run", "nope", "--task", "x"])[0], 2)
            self.assertEqual(quiet(cli.main, ["run", "w", "--task", "  "])[0], 2)

    def test_missing_binary(self):
        os.environ["PATH"] = "/nonexistent"
        with TempProject('[[workers]]\nname = "w"\n') as root:
            rc, _, _ = quiet(cli.main, ["run", "w", "--task", "x"])
            self.assertEqual(rc, 127)
            self.assertEqual(read_events(Bus(root).events)[-2]["kind"], "error")


if __name__ == "__main__":
    unittest.main()
