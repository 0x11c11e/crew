import os
import tempfile
import unittest
from pathlib import Path

from helpers import TempProject, quiet

from crew import cli, config
from crew.config import ConfigError


class TemplateRoundTrip(unittest.TestCase):
    def test_codex_template_loads(self):
        with TempProject() as root:
            config.write(root, config.template("deskfish", architect_model="fable", workers=3, effort="high"))
            cfg = config.load(root)
            self.assertEqual(cfg.name, "deskfish")
            self.assertEqual(cfg.architect.name, "fable")
            self.assertEqual(cfg.architect.model, "fable")
            self.assertEqual([w.name for w in cfg.workers], ["worker-1", "worker-2", "worker-3"])
            self.assertTrue(all(w.kind == "codex" and w.effort == "high" for w in cfg.workers))
            self.assertEqual(cfg.dashboard.split, 55)
            self.assertFalse(cfg.dashboard.raw)
            self.assertEqual((root / ".crew" / ".gitignore").read_text(), "*\n")

    def test_claude_template_loads(self):
        with TempProject() as root:
            config.write(root, config.template("p", workers=1, kind="claude", model="opus"))
            w = config.load(root).workers[0]
            self.assertEqual((w.kind, w.model, w.permission_mode), ("claude", "opus", "acceptEdits"))
            self.assertEqual(w.allowed_tools, config.DEFAULT_ALLOWED)

    def test_full_example_loads(self):
        toml = '''
name = "deskfish"
version = 1
[architect]
name = "fable"
model = "fable"
notes = """
Run `npm test` before any verdict.
"""
[[workers]]
name = "worker-1"
kind = "codex"
model = ""
effort = "medium"
sandbox = "workspace-write"
cwd = "."
timeout = 900
[[workers]]
name = "worker-2"
kind = "claude"
model = "opus"
permission_mode = "acceptEdits"
allowed_tools = "Bash Edit Write MultiEdit Read Glob Grep"
cwd = "."
timeout = 900
[dashboard]
split = 55
raw = false
'''
        with TempProject(toml) as root:
            cfg = config.load(root)
            self.assertIn("npm test", cfg.architect.notes)
            self.assertEqual(cfg.workers[1].kind, "claude")


class Defaults(unittest.TestCase):
    def test_minimal_file_gets_defaults(self):
        with TempProject('[[workers]]\nname = "w"\n') as root:
            cfg = config.load(root)
            self.assertEqual(cfg.name, root.name)
            self.assertEqual(cfg.architect.name, "fable")
            self.assertEqual(cfg.architect.model, "")
            w = cfg.workers[0]
            self.assertEqual((w.kind, w.effort, w.sandbox, w.cwd, w.timeout), ("codex", "medium", "workspace-write", ".", 900))
            self.assertEqual((w.permission_mode, w.allowed_tools), ("acceptEdits", config.DEFAULT_ALLOWED))
            self.assertEqual((cfg.dashboard.split, cfg.dashboard.raw), (55, False))

    def test_unknown_keys_warn_but_load(self):
        with TempProject('colour = "red"\n[[workers]]\nname = "w"\nflavour = 1\n') as root:
            cfg, _, err = quiet(config.load, root)
            self.assertEqual(cfg.workers[0].name, "w")
            self.assertIn("colour", err)
            self.assertIn("flavour", err)


class Validation(unittest.TestCase):
    def check_exit2(self, toml, needle):
        with TempProject(toml):
            with self.assertRaises(ConfigError) as cm:
                config.load(Path.cwd())
            self.assertIn(needle, str(cm.exception))
            rc, out, err = quiet(cli.main, ["status"])
            self.assertEqual(rc, 2)
            self.assertEqual(len(err.strip().splitlines()), 1, err)      # one clear line

    def test_duplicate_names(self):
        self.check_exit2('[[workers]]\nname = "a"\n[[workers]]\nname = "a"\n', "duplicate")

    def test_unknown_kind(self):
        self.check_exit2('[[workers]]\nname = "a"\nkind = "gemini"\n', "unknown kind")

    def test_missing_cwd(self):
        self.check_exit2('[[workers]]\nname = "a"\ncwd = "nope"\n', "does not exist")

    def test_wrong_type(self):
        self.check_exit2('[[workers]]\nname = "a"\ntimeout = "soon"\n', "timeout")

    def test_architect_not_a_table(self):
        self.check_exit2('architect = "x"\n', "architect must be a table")

    def test_dashboard_not_a_table(self):
        self.check_exit2('dashboard = 3\n', "dashboard must be a table")

    def test_worker_not_a_table(self):
        self.check_exit2('workers = ["a"]\n', "must be a table")

    def test_version_not_an_integer(self):
        self.check_exit2('version = "abc"\n', "version must be an integer")

    def test_unsafe_worker_names(self):
        for bad in ("../x", "a/b", "..", ".hidden", "-x", "has space"):
            self.check_exit2(f'[[workers]]\nname = "{bad}"\n', "may only use")

    def test_unsafe_architect_name(self):
        self.check_exit2('[architect]\nname = "../boss"\n', "may only use")

    def test_ordinary_names_pass(self):
        with TempProject('[architect]\nname = "fable"\n[[workers]]\nname = "worker-1"\n'
                         '[[workers]]\nname = "codex_2.b"\n') as root:
            self.assertEqual(len(config.load(root).workers), 2)

    def test_bad_toml(self):
        self.check_exit2('name = \n', "crew.toml")


class FindProject(unittest.TestCase):
    def test_walks_up(self):
        with TempProject('[[workers]]\nname = "w"\n') as root:
            deep = root / "a" / "b" / "c"
            deep.mkdir(parents=True)
            self.assertEqual(config.find_project(deep), root)
            os.chdir(deep)
            self.assertEqual(config.resolve_project(), root)

    def test_none_without_file(self):
        with TempProject() as root:
            self.assertIsNone(config.find_project(root))
            with self.assertRaises(ConfigError):
                config.resolve_project()

    def test_explicit_then_env(self):
        with TempProject('[[workers]]\nname = "w"\n') as root, TempProject() as other:
            self.assertEqual(config.resolve_project(str(root)), root)
            os.environ["CREW_PROJECT"] = str(root)
            try:
                self.assertEqual(config.resolve_project(), root)
                self.assertEqual(config.resolve_project(str(other)), other)
            finally:
                del os.environ["CREW_PROJECT"]



class Layout(unittest.TestCase):
    def test_default_follows_the_worker_count(self):
        with tempfile.TemporaryDirectory() as td:
            one = config.from_dict({"workers": [{"name": "w"}]}, Path(td))
            two = config.from_dict({"workers": [{"name": "a"}, {"name": "b"}]}, Path(td))
            self.assertEqual((one.layout, two.layout), ("strip", "wide"))
            wide = config.from_dict({"workers": [{"name": "w"}], "dashboard": {"layout": "wide"}}, Path(td))
            self.assertEqual(wide.layout, "wide")
            with self.assertRaises(config.ConfigError):
                config.from_dict({"dashboard": {"layout": "tall"}}, Path(td))

    def test_template_writes_it(self):
        self.assertIn('layout = "strip"', config.template("p", workers=1))
        self.assertIn('layout = "wide"', config.template("p", workers=2))
        self.assertIn("wide | strip", config.template("p"))


if __name__ == "__main__":
    unittest.main()
