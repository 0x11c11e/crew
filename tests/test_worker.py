"""The worker runner, driven by fake `codex` and `claude` scripts that replay the fixtures."""
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from helpers import FIXTURES, REPO, TempProject, quiet

from crew import cli, config, worker
from crew.bus import Bus, read_events
from crew.worker import load_state

FAKE_CODEX = """#!/usr/bin/env bash
out=""; prev=""; for a in "$@"; do [ "$prev" = "-o" ] && out="$a"; prev="$a"; done
echo "$@" > "$FAKE_LOG"
echo "plain text on stderr" >&2
[ -n "$FAKE_PRE" ] && printf '%s\n' "$FAKE_PRE"
cat "$FIXTURE"
if [ -n "$FAKE_SLEEP" ]; then
  sleep "$FAKE_SLEEP" & child=$!
  [ -n "$FAKE_PID" ] && echo "$$ $child" > "$FAKE_PID"
  wait $child
fi
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
        self.saved = {k: os.environ.get(k) for k in ("PATH", "FAKE_LOG", "FIXTURE", "FAKE_SLEEP", "FAKE_PRE", "FAKE_PID")}
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

    def wait_for_pids(self, path: Path, proc=None) -> list[int]:
        for _ in range(200):
            if path.exists() and len(path.read_text().split()) == 2:
                return [int(x) for x in path.read_text().split()]
            if proc is not None and proc.poll() is not None:
                break
            time.sleep(0.05)
        self.fail("the fake agent never started")

    def assertGone(self, pids):
        for _ in range(100):
            alive = [p for p in pids if _alive(p)]
            if not alive:
                return
            time.sleep(0.05)
        for p in alive:
            os.kill(p, signal.SIGKILL)
        self.fail(f"still running: {alive}")

    def test_unreadable_event_is_skipped(self):
        os.environ["FIXTURE"] = str(FIXTURES / "codex-stream.jsonl")
        os.environ["FAKE_PRE"] = '{"type": "item.completed", "item": ["bad"]}'
        with TempProject('[[workers]]\nname = "w"\n') as root:
            rc, out, _ = quiet(cli.main, ["run", "w", "--task", "x"])
            self.assertEqual(rc, 0, out)
            evs = read_events(Bus(root).events)
            errors = [e for e in evs if e["kind"] == "error"]
            self.assertEqual(len(errors), 1)
            self.assertIn("could not read", errors[0]["text"])
            res = [e for e in evs if e["kind"] == "result"][0]
            self.assertEqual((res["text"], res["data"]["map_errors"]), ("pong", 1))

    def test_agent_is_stopped_when_the_runner_fails(self):
        os.environ["FIXTURE"] = str(FIXTURES / "codex-stream.jsonl")
        os.environ["FAKE_SLEEP"] = "30"
        with TempProject('[[workers]]\nname = "w"\n') as root:
            pidfile = root / "pids"
            os.environ["FAKE_PID"] = str(pidfile)
            cfg = config.load(root)
            real_save = worker.save_state

            def save_after_start(*a):          # fail mid-stream, once the agent is up
                self.wait_for_pids(pidfile)
                raise RuntimeError("disk full")
            with mock.patch.object(worker, "save_state", side_effect=save_after_start):
                with self.assertRaises(RuntimeError):
                    quiet(worker.run, cfg, "w", "x")
            self.assertGone(self.wait_for_pids(pidfile))
            self.assertIs(worker.save_state, real_save)

    def test_hangup_stops_the_agent(self):
        os.environ["FIXTURE"] = "/dev/null"
        os.environ["FAKE_SLEEP"] = "30"
        with TempProject('[[workers]]\nname = "w"\n') as root:
            pidfile = root / "pids"
            env = dict(os.environ, FAKE_PID=str(pidfile), PYTHONPATH=str(REPO))
            p = subprocess.Popen([sys.executable, "-m", "crew", "run", "w", "--task", "x"], cwd=root, env=env,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                pids = self.wait_for_pids(pidfile, p)
                p.send_signal(signal.SIGHUP)
                self.assertEqual(p.wait(timeout=15), 130)
                self.assertGone(pids)
            finally:
                if p.poll() is None:
                    p.kill()
                p.communicate()
            evs = read_events(Bus(root).events)
            self.assertEqual([e["text"] for e in evs[-2:]], ["stopped: the terminal closed", "stopped"])

    def test_busy_worker_refuses_a_second_run(self):
        import fcntl
        with TempProject('[[workers]]\nname = "worker-1"\n') as root:
            wdir = root / ".crew" / "workers" / "worker-1"
            wdir.mkdir(parents=True)
            with open(wdir / ".lock", "w") as held:
                fcntl.flock(held, fcntl.LOCK_EX)
                held.write("3\n")
                held.flush()
                rc, out, err = quiet(cli.main, ["run", "worker-1", "--task", "x"])
            self.assertEqual((rc, out, err), (1, "", "worker-1 is already running (task 3)\n"))
            self.assertFalse((wdir / "task-01.md").exists())
            ev = read_events(Bus(root).events)[-1]
            self.assertEqual((ev["src"], ev["dst"], ev["kind"]), ("worker-1", "fable", "error"))
            self.assertIn("already running (task 3)", ev["text"])

    def test_overlapping_runs(self):
        os.environ["FIXTURE"] = str(FIXTURES / "codex-stream.jsonl")
        os.environ["FAKE_SLEEP"] = "30"
        with TempProject('[[workers]]\nname = "worker-1"\n') as root:
            pidfile = root / "pids"
            env = dict(os.environ, FAKE_PID=str(pidfile), PYTHONPATH=str(REPO))
            first = subprocess.Popen([sys.executable, "-m", "crew", "run", "worker-1", "--task", "a"], cwd=root,
                                     env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE)
            try:
                pids = self.wait_for_pids(pidfile, first)
                rc, _, err = quiet(cli.main, ["run", "worker-1", "--task", "b"])
                self.assertEqual((rc, err), (1, "worker-1 is already running (task 1)\n"))
                first.terminate()
                self.assertEqual(first.wait(timeout=15), 130)
                self.assertGone(pids)
            finally:
                if first.poll() is None:
                    first.kill()
                first.communicate()
            wdir = root / ".crew" / "workers" / "worker-1"
            self.assertEqual((wdir / "task-01.md").read_text(), "a")
            self.assertFalse((wdir / "task-02.md").exists())
            os.environ.pop("FAKE_SLEEP")
            self.assertEqual(quiet(cli.main, ["run", "worker-1", "--task", "c"])[0], 0)   # free again
            self.assertEqual((wdir / "task-02.md").read_text(), "c")

    def test_read_only_codex(self):
        os.environ["FIXTURE"] = str(FIXTURES / "codex-stream.jsonl")
        with TempProject('[[workers]]\nname = "w"\nsandbox = "danger-full-access"\n') as root:
            self.assertEqual(quiet(cli.main, ["run", "w", "--read-only", "--task", "review it"])[0], 0)
            args = self.log.read_text().split()
            self.assertEqual(args[args.index("-s") + 1], "read-only")
            self.assertNotIn("danger-full-access", args)
            evs = read_events(Bus(root).events)
            task = [e for e in evs if e["kind"] == "task"][0]
            self.assertIs(task["data"]["read_only"], True)
            self.assertIn("sandbox read-only", evs[0]["text"])
            self.assertEqual(config.load(root).worker("w").sandbox, "danger-full-access")
            self.assertEqual(quiet(cli.main, ["run", "w", "--continue", "--task", "now fix it"])[0], 0)
            args = self.log.read_text().split()
            self.assertEqual(args[args.index("-s") + 1], "danger-full-access")
            self.assertNotIn("read_only", [e for e in read_events(Bus(root).events) if e["kind"] == "task"][-1]["data"])

    def test_read_only_claude(self):
        os.environ["FIXTURE"] = str(FIXTURES / "claude-stream.jsonl")
        with TempProject('[[workers]]\nname = "w2"\nkind = "claude"\n') as root:
            self.assertEqual(quiet(cli.main, ["run", "w2", "--read-only", "--task", "what does x do"])[0], 0)
            args = self.log.read_text().split()
            self.assertEqual(args[args.index("--permission-mode") + 1], "default")
            i = args.index("--allowedTools")
            self.assertEqual(args[i + 1:i + 5], ["Read", "Glob", "Grep", "--disallowedTools"])
            self.assertEqual(args[i + 5:i + 10], ["Bash", "Edit", "Write", "MultiEdit", "NotebookEdit"])
            self.assertNotIn("acceptEdits", args)
            task = [e for e in read_events(Bus(root).events) if e["kind"] == "task"][0]
            self.assertIs(task["data"]["read_only"], True)
            self.assertEqual(config.load(root).worker("w2").disallowed_tools, "")
            self.assertEqual(quiet(cli.main, ["run", "w2", "--task", "now fix it"])[0], 0)
            self.assertNotIn("--disallowedTools", self.log.read_text().split())

    def test_attached_pane_gone_falls_back_to_headless(self):
        from crew import attached
        os.environ["FIXTURE"] = str(FIXTURES / "codex-stream.jsonl")
        with TempProject('[[workers]]\nname = "w"\n') as root:
            attached.write_pane(config.load(root), "w", "%9")
            dead = subprocess.CompletedProcess([], 1, "", "can't find pane")
            with mock.patch.object(attached, "tmux", return_value=dead):
                rc, out, err = quiet(cli.main, ["run", "w", "--task", "x"])
            self.assertEqual(rc, 0, out)
            self.assertIn("w: attached pane is gone; running headless", err)
            self.assertTrue((root / ".crew" / "workers" / "w" / "task-01.raw.jsonl").exists())

    def test_run_installs_the_self_ignore(self):
        os.environ["FIXTURE"] = str(FIXTURES / "codex-stream.jsonl")
        with TempProject('[[workers]]\nname = "w"\n') as root:          # hand-written: no crew init
            self.assertEqual(quiet(cli.main, ["run", "w", "--task", "x"])[0], 0)
            self.assertTrue((root / ".crew" / "workers" / "w" / "task-01.raw.jsonl").exists())
            self.assertEqual((root / ".crew" / ".gitignore").read_text(), "*\n")

    def test_worker_files_are_private(self):
        os.environ["FIXTURE"] = str(FIXTURES / "codex-stream.jsonl")
        with TempProject('[[workers]]\nname = "w"\n') as root:
            self.assertEqual(quiet(cli.main, ["run", "w", "--task", "x"])[0], 0)
            wdir = root / ".crew" / "workers" / "w"
            for f in ("task-01.raw.jsonl", "task-01.md", "task-01.result.md", "state.json", ".lock"):
                self.assertEqual((wdir / f).stat().st_mode & 0o777, 0o600, f)


class Attached(unittest.TestCase):
    """The worker is an interactive Codex in a tmux pane: crew run types the task in, the notify hook
    brings the result back. No tmux runs here: attached.tmux is replaced."""

    PAYLOAD = {"type": "agent-turn-complete", "thread-id": "th-42", "turn-id": "t1",
               "input-messages": ["Read the brief"], "last-assistant-message": "Changed x; tests green."}

    def setUp(self):
        from crew import attached
        self.attached = attached
        self.calls = []
        self.saved = (attached.POLL, attached.TYPE_PAUSE, os.environ.get("CREW_PROJECT"))
        attached.POLL, attached.TYPE_PAUSE = 0.02, 0

    def tearDown(self):
        self.attached.POLL, self.attached.TYPE_PAUSE = self.saved[0], self.saved[1]
        if self.saved[2] is None:
            os.environ.pop("CREW_PROJECT", None)
        else:
            os.environ["CREW_PROJECT"] = self.saved[2]

    def fake_tmux(self, *args, socket=""):
        self.calls.append(args)
        return subprocess.CompletedProcess(args, 0, "%9\n", "")

    def answer_when_open(self, root, payload):
        import threading
        cur = root / ".crew" / "workers" / "w" / "current"

        def go():
            for _ in range(500):
                if cur.exists():
                    break
                time.sleep(0.01)
            self.attached.notify([json.dumps(payload)])
        t = threading.Thread(target=go)
        t.start()
        return t

    def test_run_types_the_task_and_waits_for_the_result(self):
        with TempProject('[[workers]]\nname = "w"\n') as root:
            os.environ["CREW_PROJECT"] = str(root)
            self.attached.write_pane(config.load(root), "w", "%9")
            with mock.patch.object(self.attached, "tmux", side_effect=self.fake_tmux):
                t = self.answer_when_open(root, self.PAYLOAD)
                rc, out, err = quiet(cli.main, ["run", "w", "--task", "# Add x\n\ndo it"])
                t.join()
            self.assertEqual(rc, 0, out + err)
            wdir = root / ".crew" / "workers" / "w"
            typed = [c for c in self.calls if c[0] == "send-keys"]
            self.assertEqual(typed[0][:4], ("send-keys", "-t", "%9", "-l"))
            self.assertIn(str((wdir / "task-01.md").resolve()), typed[0][4])
            self.assertEqual(typed[1], ("send-keys", "-t", "%9", "Enter"))
            evs = read_events(Bus(root).events)
            self.assertEqual([(e["kind"], e["text"] if e["kind"] == "status" else "") for e in evs],
                             [("connect", ""), ("task", ""), ("status", "working"), ("result", ""),
                              ("status", "idle")])
            self.assertIs(evs[1]["data"]["attached"], True)
            self.assertEqual((evs[3]["text"], evs[3]["data"]["n"], evs[3]["data"]["attached"]),
                             ("Changed x; tests green.", 1, True))
            st = load_state(config.load(root), "w")
            self.assertEqual((st["tasks"], st["thread_id"], st["last_exit"]), (1, "th-42", 0))
            self.assertIn("Changed x; tests green.", out)
            self.assertFalse((wdir / "current").exists())

    def test_read_only_is_refused(self):
        with TempProject('[[workers]]\nname = "w"\n') as root:
            self.attached.write_pane(config.load(root), "w", "%9")
            with mock.patch.object(self.attached, "tmux", side_effect=self.fake_tmux):
                rc, _, err = quiet(cli.main, ["run", "w", "--read-only", "--task", "look"])
            self.assertEqual(rc, 2)
            self.assertIn("--read-only cannot apply", err)
            self.assertFalse([c for c in self.calls if c[0] == "send-keys"])

    def test_timeout(self):
        with TempProject('[[workers]]\nname = "w"\n') as root:
            self.attached.write_pane(config.load(root), "w", "%9")
            with mock.patch.object(self.attached, "tmux", side_effect=self.fake_tmux):
                rc, _, _ = quiet(cli.main, ["run", "w", "--timeout", "1", "--task", "x"])
            self.assertEqual(rc, 124)
            self.assertEqual(read_events(Bus(root).events)[-1]["text"], "failed (timeout)")

    def test_notify_without_an_open_task_does_nothing(self):
        with TempProject('[[workers]]\nname = "w"\n') as root:
            os.environ["CREW_PROJECT"] = str(root)
            self.assertEqual(cli.main(["codex-notify", json.dumps(self.PAYLOAD)]), 0)
            self.assertEqual(cli.main(["codex-notify", "not json"]), 0)
            self.assertFalse(Bus(root).events.exists())

    def test_notify_never_raises(self):
        with TempProject() as root:                                     # no crew.toml at all
            os.environ["CREW_PROJECT"] = str(root)
            rc, _, err = quiet(cli.main, ["codex-notify", "{}"])
            self.assertEqual(rc, 0)
            self.assertIn("crew codex-notify:", err)

    def test_attach_outside_tmux(self):
        with TempProject('[[workers]]\nname = "w"\n') as root:
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("TMUX", None)
                os.environ.pop("TMUX_PANE", None)
                rc, _, err = quiet(cli.main, ["attach", "w"])
            self.assertEqual(rc, 2)
            self.assertIn("not inside tmux", err)
            with mock.patch.dict(os.environ, {"TMUX": "/tmp/tmux-1000/default,1,0", "TMUX_PANE": "%4"}):
                self.assertEqual(quiet(cli.main, ["attach", "w"])[0], 0)
            pane = (root / ".crew" / "workers" / "w" / "pane").read_text()
            self.assertEqual(pane, "%4\n/tmp/tmux-1000/default\n")


def _alive(pid: int) -> bool:
    """True for a running process; a zombie waiting to be reaped counts as stopped."""
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False
    except OSError:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True


if __name__ == "__main__":
    unittest.main()
