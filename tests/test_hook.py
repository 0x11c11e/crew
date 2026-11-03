import json
import os
import subprocess
import sys
import unittest

from helpers import REPO, TempProject

from crew.bus import Bus, read_events

HOOK = str(REPO / "crew" / "hook.py")


def fire(root, payload, event="", **env):
    e = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "CREW_WORKER", "CLAUDE_PROJECT_DIR")}
    e.update(env)
    r = subprocess.run([sys.executable, HOOK, event], input=payload if isinstance(payload, str) else json.dumps(payload),
                       capture_output=True, text=True, env=e, cwd="/", timeout=20)
    return r


class Hook(unittest.TestCase):
    def test_events_as_a_script(self):
        with TempProject('[architect]\nname = "boss"\n') as root:
            env = {"CLAUDE_PROJECT_DIR": str(root)}
            for p in [
                {"hook_event_name": "SessionStart"},
                {"hook_event_name": "UserPromptSubmit", "prompt": "build it"},
                {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"}},
                {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "crew run w --task x"}},
                {"hook_event_name": "PreToolUse", "tool_name": "Edit", "tool_input": {"file_path": f"{root}/a.py"}},
                {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"},
                 "tool_response": {"stdout": "a.py", "stderr": "", "exit_code": 0}},
                {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": "crew say hi"},
                 "tool_response": {"stdout": ""}},
                {"hook_event_name": "Stop", "last_assistant_message": "all done"},
            ]:
                r = fire(root, p, **env)
                self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
            evs = read_events(Bus(root).events)
            self.assertEqual([(e["src"], e["kind"], e["text"]) for e in evs], [
                ("boss", "connect", "boss online · architect (Claude Code)"),
                ("user", "prompt", "build it"),
                ("boss", "exec", "ls"),
                ("boss", "edit", "a.py"),
                ("boss", "exec_done", "ls"),
                ("boss", "message", "all done"),
                ("boss", "status", "waiting for you"),
            ])

    def test_task_notification_is_not_a_prompt(self):
        wake = ("<task-notification>\n<task-id>bmedfl2mg</task-id>\n<tool-use-id>toolu_01</tool-use-id>\n"
                "<output-file>/tmp/x/tasks/bmedfl2mg.output</output-file>\n<status>completed</status>\n"
                '<summary>Background command "Dispatch Codex worker-1 to review the directory" completed '
                "(exit code 0)</summary>\n</task-notification>")
        with TempProject() as root:
            fire(root, {"hook_event_name": "UserPromptSubmit", "prompt": wake}, CLAUDE_PROJECT_DIR=str(root))
            fire(root, {"hook_event_name": "UserPromptSubmit", "prompt": "<b>real</b> xml from me"},
                 CLAUDE_PROJECT_DIR=str(root))
            evs = read_events(Bus(root).events)
            self.assertEqual([(e["src"], e["kind"], e["text"]) for e in evs], [
                ("fable", "status", 'woken up: Background command "Dispatch Codex worker-1 to review the directory" '
                                    "completed (exit code 0)"),
                ("user", "prompt", "<b>real</b> xml from me"),
            ])

    def test_payload_cwd_walks_up(self):
        with TempProject() as root:
            (root / "crew.toml").write_text("")
            (root / "sub").mkdir()
            fire(root, {"hook_event_name": "UserPromptSubmit", "prompt": "x", "cwd": str(root / "sub")})
            self.assertEqual(read_events(Bus(root).events)[0]["dst"], "fable")

    def test_worker_never_posts(self):
        with TempProject("") as root:
            r = fire(root, {"hook_event_name": "Stop", "last_assistant_message": "x"},
                     CLAUDE_PROJECT_DIR=str(root), CREW_WORKER="1")
            self.assertEqual(r.returncode, 0)
            self.assertFalse(Bus(root).events.exists())

    def test_garbage_is_harmless(self):
        with TempProject("") as root:
            for payload in ("not json", "[1,2]", ""):
                r = fire(root, payload, "Stop", CLAUDE_PROJECT_DIR=str(root))
                self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
            r = fire(root, {"hook_event_name": "Stop"}, CLAUDE_PROJECT_DIR="/nonexistent/dir")
            self.assertEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
