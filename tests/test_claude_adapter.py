import json
import unittest

from helpers import FIXTURES

from crew.config import Worker
from crew.workers import Ctx, claude

LINES = (FIXTURES / "claude-stream.jsonl").read_text().splitlines()
INIT_CWD = next(json.loads(l)["cwd"] for l in LINES if '"subtype":"init"' in l)
SESSION = "212538b4-ae9e-4251-91e1-abba469ad07b"


def run_fixture(**kw):
    ctx = Ctx(name="worker-2", architect="fable", cwd=INIT_CWD, **kw)
    outs = []
    for line in LINES:
        outs += claude.map_event(line, ctx)
    return outs, ctx


class Mapping(unittest.TestCase):
    def test_sequence(self):
        outs, ctx = run_fixture()
        self.assertEqual([o.kind for o in outs], ["status", "message", "edit", "exec", "exec_done", "message", "usage"])
        st, m1, ed, ex, done, m2, _ = outs
        self.assertEqual(st.text, "new session …9ad07b")
        self.assertEqual(st.data["thread_id"], SESSION)
        self.assertEqual((m1.text, m1.dst), ("CREW", "fable"))
        self.assertEqual(ed.text, "hello.txt")
        self.assertTrue(ex.text.startswith("cat ") and ex.text.endswith("hello.txt"))
        self.assertEqual(done.text, ex.text)
        self.assertEqual(done.data["output"], "hello")
        self.assertEqual(done.data["exit_code"], 0)
        self.assertTrue(m2.text.startswith("CREW: I created hello.txt"))

    def test_result(self):
        _, ctx = run_fixture()
        self.assertEqual(ctx.thread_id, SESSION)
        self.assertFalse(ctx.failed)                       # exit 0
        self.assertTrue(claude.final_message(ctx).startswith("CREW: I created hello.txt"))
        self.assertEqual(ctx.tokens, 6 + 325)
        self.assertAlmostEqual(ctx.cost, 0.1067234)
        self.assertEqual(ctx.turns, 3)
        self.assertEqual(ctx.commands, 1)

    def test_resumed(self):
        outs, _ = run_fixture(resume=True)
        self.assertEqual(outs[0].text, "resumed session …9ad07b")

    def test_ignored_events(self):
        ctx = Ctx()
        for line in LINES:
            ev = json.loads(line)
            if ev["type"] == "rate_limit_event" or (ev["type"] == "system" and ev.get("subtype") != "init"):
                self.assertEqual(claude.map_event(line, ctx), [], line[:80])
        self.assertEqual(claude.map_event("not json", ctx), [])
        self.assertEqual(claude.map_event('{"type":"rate_limit_event","rate_limit_info":{}}', ctx), [])

    def test_fixture_is_clean(self):
        for line in LINES:
            self.assertNotIn("/tmp/", line)
            self.assertNotIn("rate_limit_event", line)

    def test_error_result_and_list_content(self):
        ctx = Ctx()
        claude.map_event(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "false"}},
            {"type": "tool_use", "id": "t2", "name": "Grep", "input": {}}]}}), ctx)
        out = claude.map_event(json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "is_error": True,
             "content": [{"type": "text", "text": "boom"}]},
            {"type": "tool_result", "tool_use_id": "t2", "content": "match"}]}}), ctx)
        self.assertEqual(len(out), 1)
        self.assertEqual((out[0].kind, out[0].data["exit_code"], out[0].data["output"]), ("exec_done", 1, "boom"))
        claude.map_event(json.dumps({"type": "result", "subtype": "error_max_turns", "session_id": "s",
                                     "usage": {"input_tokens": 1, "output_tokens": 2}}), ctx)
        self.assertTrue(ctx.failed)

    def test_other_tool_is_a_status(self):
        out = claude.map_event(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "x", "name": "WebSearch", "input": {}}]}}), Ctx())
        self.assertEqual((out[0].kind, out[0].text), ("status", "uses WebSearch"))


class Command(unittest.TestCase):
    def test_command(self):
        w = Worker(name="worker-2", kind="claude", model="opus")
        cmd = claude.build_command(w, "do it", "sess-1")
        self.assertEqual(cmd[:3], ["claude", "-p", "do it"])
        self.assertEqual(cmd[cmd.index("--output-format") + 1], "stream-json")
        self.assertIn("--verbose", cmd)
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "acceptEdits")
        self.assertEqual(cmd[cmd.index("--allowedTools") + 1], "Bash Edit Write MultiEdit Read Glob Grep")
        self.assertEqual(cmd[cmd.index("--model") + 1], "opus")
        self.assertEqual(cmd[-2:], ["--resume", "sess-1"])

    def test_no_resume_no_model(self):
        cmd = claude.build_command(Worker(name="w", kind="claude"), "t", None)
        self.assertNotIn("--resume", cmd)
        self.assertNotIn("--model", cmd)
        self.assertNotIn("--disallowedTools", cmd)


if __name__ == "__main__":
    unittest.main()
