import json
import os
import subprocess
import sys
import tomllib
import unittest

from helpers import REPO, SAMPLE, TempProject, quiet

from crew import cli, config, launch
from crew.bus import Bus, read_events

CREW = str(REPO / "bin" / "crew")


class Init(unittest.TestCase):
    def test_init_yes(self):
        with TempProject() as root:
            rc, out, _ = quiet(cli.main, ["init", "--yes"])
            self.assertEqual(rc, 0)
            raw = tomllib.loads((root / "crew.toml").read_text())
            self.assertEqual(raw["name"], root.name)
            self.assertEqual(len(raw["workers"]), 2)
            self.assertEqual((root / ".crew" / ".gitignore").read_text(), "*\n")
            cfg = config.load(root)
            self.assertEqual([w.kind for w in cfg.workers], ["codex", "codex"])
            self.assertIn("[[workers]]", out)

    def test_init_flags(self):
        with TempProject() as root:
            rc, _, _ = quiet(cli.main, ["init", str(root), "--yes", "--workers", "3", "--kind", "claude",
                                        "--model", "sonnet"])
            self.assertEqual(rc, 0)
            cfg = config.load(root)
            self.assertEqual(cfg.architect.model, "sonnet")
            self.assertEqual([w.kind for w in cfg.workers], ["claude"] * 3)

    def test_init_refuses_to_overwrite(self):
        with TempProject('[[workers]]\nname = "w"\n'):
            rc, _, err = quiet(cli.main, ["init", "--yes"])
            self.assertEqual(rc, 2)
            self.assertIn("already exists", err)

    def test_project_gains_only_two_things(self):
        with TempProject() as root:
            quiet(cli.main, ["init", "--yes"])
            quiet(cli.main, ["up", "--dry-run"])
            self.assertEqual(sorted(os.listdir(root)), [".crew", "crew.toml"])


class Up(unittest.TestCase):
    def test_dry_run_prints_both_panes(self):
        with TempProject() as root:
            quiet(cli.main, ["init", "--yes"])
            Bus(root).post("x", None, "note", "old")
            rc, out, _ = quiet(cli.main, ["up", "--dry-run", "--claude-args", "--model sonnet"])
            self.assertEqual(rc, 0)
            self.assertIn("claude --settings .crew/claude-settings.json --append-system-prompt-file "
                          ".crew/ARCHITECT.md --model sonnet", out)
            self.assertIn(f"PATH={REPO / 'bin'}:\"$PATH\"", out)
            self.assertIn("-m crew dashboard --project", out)
            self.assertIn(f"PYTHONPATH={REPO}", out)
            self.assertTrue((root / ".crew" / "ARCHITECT.md").exists())
            self.assertTrue((root / ".crew" / "claude-settings.json").exists())
            self.assertEqual(len(read_events(Bus(root).events)), 1)      # dry run never resets the bus

    def test_pane_commands(self):
        with TempProject('name = "my.proj"\n[architect]\nmodel = "opus"\n[[workers]]\nname = "w"\n'
                         '[dashboard]\nraw = true\nsplit = 60\n') as root:
            cfg = config.load(root)
            p = launch.pane_commands(cfg, "--verbose")
            self.assertIn("--model opus --verbose", p.architect)
            self.assertTrue(p.architect.startswith(f"cd {root} && PATH="))
            self.assertIn(".crew/workers/*/task-*.raw.jsonl", p.raw)
            self.assertEqual(launch.session_name(cfg), "crew-my-proj")
            # the pane commands are valid shell
            for cmd in (p.architect, p.dashboard, p.raw):
                subprocess.run(["bash", "-n", "-c", cmd], check=True)

    def test_archive(self):
        with TempProject('[[workers]]\nname = "w"\n') as root:
            cfg = config.load(root)
            Bus(root).post("x", None, "note", "old")
            (cfg.state / "workers" / "w").mkdir(parents=True)
            (cfg.state / "workers" / "w" / "state.json").write_text("{}")
            notes = launch.archive(cfg, fresh=True)
            self.assertEqual(len(notes), 2)
            self.assertEqual(Bus(root).events.stat().st_size, 0)
            self.assertFalse((cfg.state / "workers").exists())
            self.assertEqual(len(list((cfg.state / "archive").glob("workers-*"))), 1)

    def test_up_without_crew_toml(self):
        with TempProject():
            rc, _, err = quiet(cli.main, ["up", "--dry-run"])
            self.assertEqual(rc, 2)
            self.assertIn("crew.toml", err)


class BringUp(unittest.TestCase):
    def test_no_tty_generates_and_dry_runs(self):
        with TempProject() as root:
            r = subprocess.run([CREW, str(root), "--dry-run"], stdin=subprocess.DEVNULL, capture_output=True,
                               text=True, timeout=30)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue((root / "crew.toml").exists())
            self.assertIn("terminal 1", r.stdout)


class BusCommands(unittest.TestCase):
    def test_say_verify_post_tail_status(self):
        with TempProject('[architect]\nname = "boss"\n[[workers]]\nname = "w1"\n') as root:
            sub = root / "deep"
            sub.mkdir()
            os.chdir(sub)                                   # resolves by walking up
            self.assertEqual(quiet(cli.main, ["say", "Plan: x"])[0], 0)
            self.assertEqual(quiet(cli.main, ["say", "hi", "--to", "w1"])[0], 0)
            self.assertEqual(quiet(cli.main, ["verify", "w1", "pass", "green"])[0], 0)
            self.assertEqual(quiet(cli.main, ["verify", "nobody", "pass"])[0], 2)
            self.assertEqual(quiet(cli.main, ["post", "--from", "a", "--kind", "note", "--data", '{"k": 1}'])[0], 0)
            self.assertEqual(quiet(cli.main, ["post", "--from", "a", "--kind", "note", "--data", "nope"])[0], 2)
            evs = read_events(Bus(root).events)
            self.assertEqual([(e["src"], e.get("dst"), e["kind"]) for e in evs],
                             [("boss", None, "message"), ("boss", "w1", "message"), ("boss", "w1", "verify"),
                              ("a", None, "note")])
            self.assertEqual(evs[2]["data"]["verdict"], "pass")
            rc, out, _ = quiet(cli.main, ["tail", "-n", "2"])
            self.assertEqual(len(out.strip().splitlines()), 2)
            rc, out, _ = quiet(cli.main, ["status"])
            self.assertIn("w1", out)

    def test_project_flag_and_env(self):
        with TempProject('[[workers]]\nname = "w1"\n') as root, TempProject():
            self.assertEqual(quiet(cli.main, ["say", "x", "--project", str(root)])[0], 0)
            os.environ["CREW_PROJECT"] = str(root)
            try:
                self.assertEqual(quiet(cli.main, ["say", "y"])[0], 0)
            finally:
                del os.environ["CREW_PROJECT"]
            self.assertEqual(len(read_events(Bus(root).events)), 2)


class Wrapper(unittest.TestCase):
    def test_bin_crew_runs_uninstalled(self):
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        r = subprocess.run([CREW, "--version"], capture_output=True, text=True, env=env, cwd="/")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("crew 0.1.0", r.stdout)

    def test_snapshot_command(self):
        with TempProject() as root:
            r = subprocess.run([CREW, "snapshot", "out.svg", "--replay", str(SAMPLE), "--focus", "worker-1"],
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue((root / "out.svg").read_text().lstrip().startswith("<svg"))


if __name__ == "__main__":
    unittest.main()
