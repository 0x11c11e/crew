import unittest
from types import SimpleNamespace

from helpers import FIXTURES

from crew.config import Worker
from crew.util import pretty_cmd
from crew.workers import Ctx, codex

WS = "/home/user/agent-demo/workspace"     # the cwd the fixture was recorded in


def run_fixture(ctx=None):
    ctx = ctx or Ctx(name="worker-1", architect="fable", cwd=WS)
    outs = []
    for line in (FIXTURES / "codex-stream.jsonl").read_text().splitlines(keepends=True):
        outs += codex.map_event(line, ctx)
    return outs, ctx


class Mapping(unittest.TestCase):
    def test_kinds_in_order(self):
        outs, ctx = run_fixture()
        self.assertEqual([o.kind for o in outs], [
            "status", "status", "message",
            "exec", "exec_done", "exec", "exec_done", "edit", "exec", "exec_done",
            "message", "usage"])
        self.assertEqual(outs[0].text, "new thread …1e9ed3")
        self.assertEqual(outs[0].data["thread_id"], "01a19eeb-7959-79b3-9d4e-5bd46f1e9ed3")
        self.assertEqual(outs[1].text, "working")
        self.assertEqual(ctx.thread_id, "01a19eeb-7959-79b3-9d4e-5bd46f1e9ed3")

    def test_noise_is_dropped(self):
        outs, _ = run_fixture()
        self.assertFalse([o for o in outs if o.kind == "error"])

    def test_exec_and_exit_codes(self):
        outs, ctx = run_fixture()
        done = [o for o in outs if o.kind == "exec_done"]
        self.assertEqual([o.data["exit_code"] for o in done], [1, 0, 0])
        self.assertEqual(done[1].text, "ls -la")
        self.assertTrue(done[1].data["output"].startswith("total 8"))
        self.assertIn("added 1: Buy milk", done[2].data["output"])
        self.assertTrue(all(len(o.data["output"]) <= 1200 for o in done))
        self.assertEqual(ctx.commands, 3)

    def test_edit_is_relative(self):
        outs, _ = run_fixture()
        self.assertEqual([o.text for o in outs if o.kind == "edit"], ["todo.py"])

    def test_messages_go_to_the_architect(self):
        outs, ctx = run_fixture(Ctx(name="w", architect="boss", cwd=WS))
        msgs = [o for o in outs if o.kind == "message"]
        self.assertTrue(all(o.dst == "boss" for o in msgs))
        self.assertTrue(ctx.last_message.startswith("Created `todo.py`"))
        self.assertEqual(codex.final_message(ctx, "/nonexistent/result.md"), ctx.last_message.strip())

    def test_usage_and_tokens(self):
        outs, ctx = run_fixture()
        self.assertEqual(outs[-1].data["input_tokens"], 71328)
        self.assertEqual(ctx.tokens, 71328 + 1062)

    def test_resumed_and_failures(self):
        ctx = Ctx(resume=True)
        self.assertEqual(codex.map_event('{"type":"thread.started","thread_id":"abcdef123456"}', ctx)[0].text,
                         "resumed thread …123456")
        self.assertEqual(codex.map_event("Reading prompt from stdin...\n", ctx), [])
        self.assertEqual(codex.map_event("[1,2]", ctx), [])
        out = codex.map_event('{"type":"turn.failed","error":{"message":"quota"}}', ctx)
        self.assertEqual((out[0].kind, out[0].text), ("error", "quota"))
        self.assertTrue(ctx.failed)

    def test_pretty_cmd(self):
        self.assertEqual(pretty_cmd("/bin/bash -lc 'printf hi > f'"), "printf hi > f")
        self.assertEqual(pretty_cmd("ls"), "ls")


class Command(unittest.TestCase):
    def test_flags_precede_resume(self):
        w = Worker(name="worker-1", model="gpt-6-astra", effort="high", sandbox="read-only")
        cmd = codex.build_command(w, "do it", "tid-1", "/r.md", "/proj")
        self.assertEqual(cmd[:2], ["codex", "exec"])
        self.assertEqual(cmd[-3:], ["resume", "tid-1", "do it"])
        for flag in ("-s", "-c", "-C", "--json", "-o", "-m"):
            self.assertLess(cmd.index(flag), cmd.index("resume"))
        self.assertEqual(cmd[cmd.index("-s") + 1], "read-only")
        self.assertEqual(cmd[cmd.index("-c") + 1], 'model_reasoning_effort="high"')
        self.assertEqual(cmd[cmd.index("-o") + 1], "/r.md")
        self.assertEqual(cmd[cmd.index("-C") + 1], "/proj")

    def test_new_thread_has_no_resume_or_model(self):
        cmd = codex.build_command(Worker(name="w"), "task", None, "/r.md", "/p")
        self.assertNotIn("resume", cmd)
        self.assertNotIn("-m", cmd)
        self.assertEqual(cmd[-1], "task")


if __name__ == "__main__":
    unittest.main()
