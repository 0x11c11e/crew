import json
import os
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path

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
            self.assertEqual(len(raw["workers"]), 1)
            self.assertEqual(raw["dashboard"]["layout"], "strip")
            self.assertEqual((root / ".crew" / ".gitignore").read_text(), "*\n")
            cfg = config.load(root)
            self.assertEqual([w.kind for w in cfg.workers], ["codex"])
            self.assertEqual(cfg.layout, "strip")
            self.assertIn("[[workers]]", out)

    def test_init_flags(self):
        with TempProject() as root:
            rc, _, _ = quiet(cli.main, ["init", str(root), "--yes", "--workers", "3", "--kind", "claude",
                                        "--model", "sonnet"])
            self.assertEqual(rc, 0)
            cfg = config.load(root)
            self.assertEqual(cfg.architect.model, "sonnet")
            self.assertEqual([w.kind for w in cfg.workers], ["claude"] * 3)

    def test_init_layout_follows_the_worker_kind(self):
        for kind, layout in (("codex", "strip"), ("claude", "wide")):
            with TempProject() as root:
                rc, _, _ = quiet(cli.main, ["init", "--yes", "--kind", kind])
                self.assertEqual(rc, 0)
                raw = tomllib.loads((root / "crew.toml").read_text())
                self.assertEqual((len(raw["workers"]), raw["dashboard"]["layout"]), (1, layout), kind)

    def test_init_asks_for_the_worker_model(self):
        import builtins
        from unittest import mock
        for kind in ("codex", "claude"):
            with TempProject() as root:
                asked = []

                def answer(q):
                    asked.append(q)
                    return "x" if q.startswith("Worker model") else ""
                with mock.patch.object(builtins, "input", side_effect=answer):
                    cli.do_init(root, yes=False, kind=kind, show=False)
                self.assertTrue(any(q.startswith("Worker model (blank = the agent's default)") for q in asked))
                raw = tomllib.loads((root / "crew.toml").read_text())
                self.assertEqual(raw["workers"][0]["model"], "x", kind)
                self.assertIn('model = "x"', (root / "crew.toml").read_text())
        with TempProject() as root:
            with mock.patch.object(builtins, "input", side_effect=AssertionError("prompted")):
                cli.do_init(root, yes=True, worker_model="m", show=False)
            self.assertEqual(tomllib.loads((root / "crew.toml").read_text())["workers"][0]["model"], "m")
        with TempProject() as root:
            blank = lambda q: '""' if q.startswith("Worker model") else ""     # noqa: E731
            with mock.patch.object(builtins, "input", side_effect=blank):
                cli.do_init(root, yes=False, worker_model="m", show=False)
            self.assertEqual(tomllib.loads((root / "crew.toml").read_text())["workers"][0]["model"], "")

    def test_init_rejects_bad_worker_counts(self):
        with TempProject() as root:
            for bad in ("10", "0", "-1", "two"):
                r = subprocess.run([CREW, "init", str(root), "--yes", "--workers", bad], stdin=subprocess.DEVNULL,
                                   capture_output=True, text=True, timeout=30)
                self.assertEqual(r.returncode, 2, bad)
                self.assertIn("--workers", r.stderr)
            self.assertFalse((root / "crew.toml").exists())

    def test_ask_int_never_loops_on_a_bad_default(self):
        import builtins
        import threading
        from unittest import mock
        got = {}

        def go():
            got["yes"] = quiet(cli.ask_int, "Number of workers", 10, True)
            with mock.patch.object(builtins, "input", side_effect=EOFError):
                got["eof"] = quiet(cli.ask_int, "Number of workers", 0, False)
            with mock.patch.object(builtins, "input", side_effect=["12", "", "3"]):
                got["typed"] = quiet(cli.ask_int, "Number of workers", 2, False)
        t = threading.Thread(target=go, daemon=True)
        t.start()
        t.join(10)
        self.assertFalse(t.is_alive(), "ask_int did not return")
        self.assertEqual(got["yes"][:2], (9, "  number of workers must be from 1 to 9; using 9\n"))
        self.assertEqual(got["eof"][0], 1)
        self.assertEqual(got["typed"][0], 2)            # a typed bad answer is asked again; blank takes 2

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
                         '[dashboard]\nraw = true\nsplit = 60\nlayout = "wide"\n') as root:
            cfg = config.load(root)
            p = launch.pane_commands(cfg, "--verbose")
            self.assertIn("--model opus --verbose", p.architect)
            self.assertTrue(p.architect.startswith(f"cd {root} && PATH="))
            self.assertIn(".crew/workers/*/task-*.raw.jsonl", p.raw)
            self.assertRegex(launch.session_name(cfg), r"^crew-my-proj-[0-9a-f]{6}$")
            # the pane commands are valid shell
            for cmd in (p.architect, p.dashboard, p.raw):
                subprocess.run(["bash", "-n", "-c", cmd], check=True)

    def test_session_names_do_not_collide(self):
        import tempfile
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            dot = config.from_dict({"name": "same.name"}, Path(a))
            colon = config.from_dict({"name": "same:name"}, Path(a))
            clone1 = config.from_dict({"name": "api"}, Path(a))
            clone2 = config.from_dict({"name": "api"}, Path(b))
            self.assertNotEqual(launch.session_name(clone1), launch.session_name(clone2))
            self.assertEqual(launch.session_name(clone1), launch.session_name(config.from_dict({"name": "api"},
                                                                                               Path(a) / ".")))
            self.assertEqual(launch.session_name(dot), launch.session_name(colon))   # same project, same session
            self.assertNotEqual(launch.session_name(dot),
                                launch.session_name(config.from_dict({"name": "same:name"}, Path(b))))
            for c in (dot, clone1, clone2):
                self.assertNotRegex(launch.session_name(c), r"[.:]")

    def test_stop_targets_only_its_own_session(self):
        from unittest import mock
        import tempfile
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            mine = config.from_dict({"name": "api"}, Path(a))
            other = config.from_dict({"name": "api"}, Path(b))
            calls = []
            with mock.patch.object(launch, "has_session", side_effect=lambda n: n == launch.session_name(other)), \
                 mock.patch.object(launch, "tmux", side_effect=lambda *a, **k: calls.append(a)):
                rc, out, _ = quiet(launch.stop, mine)
            self.assertEqual((rc, calls), (0, []))
            self.assertIn("is not running", out)
            with mock.patch.object(launch, "has_session", return_value=True), \
                 mock.patch.object(launch, "tmux", side_effect=lambda *a, **k: calls.append(a)):
                quiet(launch.stop, mine)
            self.assertEqual(calls, [("kill-session", "-t", f"={launch.session_name(mine)}")])

    def up_calls(self, toml: str):
        """launch.up against a recorder instead of tmux; returns the recorded tmux argument lists."""
        from unittest import mock
        calls = []
        with TempProject(toml) as root:
            cfg = config.load(root)
            with mock.patch.object(launch.shutil, "which", return_value="/usr/bin/tmux"), \
                 mock.patch.object(launch, "has_session", return_value=False), \
                 mock.patch.object(launch, "tmux", side_effect=lambda *a, **k: calls.append(a)):
                rc, out, _ = quiet(launch.up, cfg, detached=True)
            self.assertEqual(rc, 0, out)
            self.out = out
            conf = (root / ".crew" / "tmux.conf").read_text()
            pane = root / ".crew" / "workers" / cfg.workers[0].name / "pane" if cfg.workers else None
            self.pane = pane.read_text() if pane and pane.exists() else None
        return cfg, calls, conf

    def test_trio_for_one_codex_worker(self):
        cfg, calls, _ = self.up_calls('[architect]\nmodel = "opus"\n[[workers]]\nname = "worker-1"\n'
                                      'model = "gpt-6-astra"\n')
        name = launch.session_name(cfg)
        titles = [c[-1] for c in calls if c[0] == "select-pane" and "-T" in c]
        self.assertEqual(titles, ["architect · Claude Code · opus", "status", "worker · Codex · gpt-6-astra"])
        splits = [c for c in calls if c[0] == "split-window"]
        self.assertEqual([c[:6] for c in splits], [
            ("split-window", "-h", "-t", f"={name}:main.0", "-l", "60%"),
            ("split-window", "-h", "-t", f"={name}:main.1", "-l", "63%")])
        self.assertIn("--layout strip", splits[0][-1])
        codex = launch.pane_commands(cfg).worker
        self.assertEqual(splits[1][-1], launch.in_bash(codex))
        self.assertIn("codex -s workspace-write -a never -C", codex)
        self.assertIn("-c 'notify=[\"crew\",\"codex-notify\"]'", codex)
        self.assertIn("-m gpt-6-astra", codex)
        self.assertIn(f"PATH={REPO / 'bin'}:", codex)
        self.assertIn("CREW_PROJECT=", codex)
        self.assertIn(("-P", "-F", "#{pane_id}"), [tuple(splits[1][8:11])])
        self.assertEqual(self.pane, f"={name}:main.2\n\n")      # the recorder returns no pane id

    def test_two_workers_or_a_claude_worker_keep_two_panes(self):
        for toml in ('[[workers]]\nname = "a"\n[[workers]]\nname = "b"\n',
                     '[[workers]]\nname = "c"\nkind = "claude"\n'):
            _, calls, _ = self.up_calls(toml)
            self.assertEqual(len([c for c in calls if c[0] == "split-window"]), 1, toml)
            titles = [c[-1] for c in calls if c[0] == "select-pane" and "-T" in c]
            self.assertEqual(titles[1:], ["dashboard · q quits, 1-9 focus an agent"], toml)
            self.assertIsNone(self.pane)

    def test_strip_with_a_claude_worker_falls_back_to_wide(self):
        _, calls, _ = self.up_calls('[[workers]]\nname = "c"\nkind = "claude"\n[dashboard]\nlayout = "strip"\n')
        self.assertIn("layout strip needs a codex worker on the right; using wide", self.out)
        splits = [c for c in calls if c[0] == "split-window"]
        self.assertEqual(len(splits), 1)
        self.assertIn("--layout wide", splits[0][-1])
        self.assertIsNone(self.pane)
        _, _, _ = self.up_calls('[[workers]]\nname = "w"\n[dashboard]\nlayout = "strip"\n')
        self.assertNotIn("using wide", self.out)

    def test_dry_run_prints_three_panes_for_one_worker(self):
        with TempProject() as root:
            quiet(cli.main, ["init", "--yes", "--workers", "1"])
            self.assertIn('layout = "strip"', (root / "crew.toml").read_text())
            rc, out, _ = quiet(cli.main, ["up", "--dry-run"])
            self.assertEqual(rc, 0)
            for needle in ("terminal 1 (left)", "terminal 2 (middle", "terminal 3 (right)", "--layout strip",
                           "codex -s workspace-write -a never", "crew attach worker-1"):
                self.assertIn(needle, out, needle)

    def test_panes_are_labelled(self):
        cfg, calls, conf = self.up_calls('[architect]\nmodel = "opus#(id)"\n[[workers]]\nname = "w"\n'
                                         '[dashboard]\nraw = true\nlayout = "wide"\n')
        name = launch.session_name(cfg)
        titles = [c for c in calls if c[0] == "select-pane" and "-T" in c]
        self.assertEqual(titles, [
            ("select-pane", "-t", f"={name}:main.0", "-T", "architect · Claude Code · opus##(id)"),
            ("select-pane", "-t", f"={name}:main.1", "-T", "dashboard · q quits, 1-9 focus an agent"),
            ("select-pane", "-t", f"={name}:main.2", "-T", "raw worker log")])
        self.assertIn('set -g pane-border-status top\n', conf)
        self.assertIn('set -g pane-border-format " #{pane_title} "\n', conf)
        self.assertIn('set -g status-right " crew · Ctrl-b d detaches · crew stop closes "\n', conf)
        self.assertEqual(calls[-1], ("select-pane", "-t", f"={name}:main.0"))     # architect keeps focus

        _, calls, _ = self.up_calls('[[workers]]\nname = "w"\n[dashboard]\nlayout = "wide"\n')
        titles = [c[-1] for c in calls if c[0] == "select-pane" and "-T" in c]
        self.assertEqual(titles, ["architect · Claude Code", "dashboard · q quits, 1-9 focus an agent"])

    def test_status_left_is_literal(self):
        with TempProject('name = "#(touch pwned) #{pane_pid} 100%"\n[[workers]]\nname = "w"\n') as root:
            cfg = config.load(root)
            self.assertEqual(launch.status_left(cfg), " crew · ##(touch pwned) ##{pane_pid} 100%% ")
            self.assertEqual(launch.tmux_literal("a\tb\x1b[31m"), "a b [31m")

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

    def test_configured_folder_skips_the_question(self):
        import builtins
        from unittest import mock
        with TempProject('[[workers]]\nname = "w"\n') as root:
            asked = []
            with mock.patch.object(cli, "interactive", return_value=True), \
                 mock.patch.object(builtins, "input", side_effect=lambda q: asked.append(q) or ""):
                rc, out, _ = quiet(cli.main, ["--dry-run"])
            self.assertEqual(rc, 0)
            self.assertEqual(asked, [])
            self.assertIn(f"project   {root}", out)


class BusCommands(unittest.TestCase):
    def test_direct_commands_install_the_self_ignore(self):
        for argv in (["say", "probe"], ["post", "--from", "a", "--kind", "note"], ["verify", "w", "pass"]):
            with TempProject('[[workers]]\nname = "w"\n') as root:          # hand-written: no crew init
                self.assertEqual(quiet(cli.main, argv)[0], 0)
                self.assertTrue(Bus(root).events.exists())
                self.assertEqual((root / ".crew" / ".gitignore").read_text(), "*\n", argv)

    def test_state_is_private(self):
        for older in (False, True):
            with TempProject('[[workers]]\nname = "w"\n') as root:
                if older:                                     # left world-readable by an older crew
                    (root / ".crew").mkdir()
                    (root / ".crew").chmod(0o755)
                self.assertEqual(quiet(cli.main, ["say", "probe"])[0], 0)
                self.assertEqual((root / ".crew").stat().st_mode & 0o777, 0o700, older)
                self.assertEqual(Bus(root).events.stat().st_mode & 0o777, 0o600, older)

    def test_reading_creates_nothing(self):
        with TempProject('[[workers]]\nname = "w"\n') as root:
            self.assertEqual(quiet(cli.main, ["tail"])[0], 0)
            self.assertFalse((root / ".crew").exists())

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


class Docs(unittest.TestCase):
    def test_readme_covers_busy_workers_and_old_sessions(self):
        text = (REPO / "README.md").read_text()
        self.assertIn("is already running", text)
        self.assertIn("kill-session -t crew-<name>", text)
        self.assertIn("mode 700", text)


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
