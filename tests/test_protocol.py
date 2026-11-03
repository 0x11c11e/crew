import json
import os
import shlex
import unittest

from helpers import TempProject

from crew import config, protocol

TOML = '''
name = "deskfish"
[architect]
name = "fable"
notes = "Run `npm test` before any verdict."
[[workers]]
name = "worker-1"
kind = "codex"
model = "gpt-6-astra"
[[workers]]
name = "worker-2"
kind = "claude"
model = "opus"
[[workers]]
name = "builder"
'''


class ArchitectMd(unittest.TestCase):
    def test_lists_every_worker_and_run_line(self):
        with TempProject(TOML) as root:
            cfg = config.load(root)
            md = protocol.render_architect(cfg)
            self.assertIn("# You are the architect (fable)", md)
            self.assertIn("Project: deskfish.", md)
            self.assertIn("- worker-1 — Codex (gpt-6-astra, effort medium), sandbox workspace-write, cwd .", md)
            self.assertIn("- worker-2 — Claude Code (opus), acceptEdits, cwd .", md)
            self.assertIn("- builder — Codex (default model, effort medium)", md)
            for w in cfg.workers:
                self.assertIn(f"{protocol.CMD} run {w.name} --task", md)
            self.assertIn(f'{protocol.CMD} say "Plan: ..."', md)
            self.assertIn(f"{protocol.CMD} verify worker-1 pass", md)
            self.assertTrue(md.rstrip().endswith("Run `npm test` before any verdict."))

    def test_write_all(self):
        with TempProject(TOML) as root:
            md, st = protocol.write_all(config.load(root))
            self.assertEqual(md, root / ".crew" / "ARCHITECT.md")
            self.assertEqual(st, root / ".crew" / "claude-settings.json")
            json.loads(st.read_text())


class Settings(unittest.TestCase):
    def setUp(self):
        self.s = protocol.render_settings()

    def test_round_trips_as_json(self):
        self.assertEqual(json.loads(json.dumps(self.s)), self.s)

    def test_allow_patterns_use_the_protocol_command(self):
        allow = self.s["permissions"]["allow"]
        self.assertEqual(len(allow), len(protocol.ALLOWED_SUBCOMMANDS))
        for pat in allow:
            self.assertTrue(pat.startswith(f"Bash({protocol.CMD} "), pat)
        for sub in ("say", "run", "verify"):
            self.assertIn(f"Bash({protocol.command(sub)}:*)", allow)
            # the string the architect is told to type is the string the allowlist matches
            with TempProject(TOML) as root:
                self.assertIn(protocol.command(sub) + " ", protocol.render_architect(config.load(root)))

    def test_hooks_are_absolute_existing_paths(self):
        events = {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"}
        self.assertEqual(set(self.s["hooks"]), events)
        for event, entries in self.s["hooks"].items():
            for entry in entries:
                for h in entry["hooks"]:
                    argv = shlex.split(h["command"])
                    self.assertEqual(len(argv), 3)
                    py, script, ev = argv
                    self.assertTrue(os.path.isabs(py) and os.path.exists(py), py)
                    self.assertTrue(os.path.isabs(script) and os.path.isfile(script), script)
                    self.assertTrue(script.endswith("crew/hook.py"))
                    self.assertEqual(ev, event)
                    self.assertEqual(h["timeout"], 5)
        self.assertEqual(self.s["hooks"]["PreToolUse"][0]["matcher"], "Bash|Edit|Write|MultiEdit|Read|Agent")
        self.assertEqual(self.s["hooks"]["PostToolUse"][0]["matcher"], "Bash")


if __name__ == "__main__":
    unittest.main()
