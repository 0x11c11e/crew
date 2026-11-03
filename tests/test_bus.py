import json
import multiprocessing
import os
import unittest

from helpers import TempProject

from crew.bus import MAX_LINE, Bus, Follower, clip, fmt, read_events


def _writer(root, i):
    b = Bus(root)
    for j in range(50):
        b.post(f"w{i}", None, "message", f"{i}-{j} " + "x" * 300)


class Post(unittest.TestCase):
    def test_shape_and_clipping(self):
        with TempProject() as root:
            b = Bus(root)
            ev = b.post("fable", "worker-1", "task", "y" * 5000, n=1, note="z" * 5000, skip=None)
            self.assertEqual(set(ev), {"id", "ts", "src", "dst", "kind", "text", "data"})
            self.assertEqual(len(ev["text"]), 1500)
            self.assertTrue(ev["text"].endswith("…"))
            self.assertEqual(len(ev["data"]["note"]), 1500)
            self.assertNotIn("skip", ev["data"])
            self.assertEqual(b.events, root / ".crew" / "events.jsonl")
            self.assertEqual(read_events(b.events)[0]["id"], ev["id"])

    def test_data_may_reuse_core_names(self):
        with TempProject() as root:
            ev = Bus(root).post("fable", "w", "task", "t", kind="codex", text="x", src="s")
            self.assertEqual(ev["kind"], "task")
            self.assertEqual(ev["data"]["kind"], "codex")

    def test_line_limit(self):
        with TempProject() as root:
            b = Bus(root)
            b.post("a", None, "note", "t", **{f"k{i}": "v" * 1400 for i in range(10)})
            line = b.events.read_text().splitlines()[0]
            self.assertLessEqual(len(line), MAX_LINE)
            self.assertNotIn("data", json.loads(line))

    def test_clip(self):
        self.assertEqual(clip(None), "")
        self.assertEqual(clip("a\r\nb"), "a\nb")
        self.assertEqual(clip("abcdef", 4), "abc…")

    def test_parallel_writers_never_interleave(self):
        with TempProject() as root:
            ps = [multiprocessing.Process(target=_writer, args=(str(root), i)) for i in range(4)]
            for p in ps:
                p.start()
            for p in ps:
                p.join()
            lines = Bus(root).events.read_text().splitlines()
            self.assertEqual(len(lines), 200)
            for ln in lines:
                json.loads(ln)

    def test_fmt(self):
        line = fmt({"ts": 0, "src": "fable", "dst": "worker-1", "kind": "verify", "text": "ok",
                    "data": {"verdict": "pass"}})
        self.assertIn("fable → worker-1: ok", line)
        self.assertIn("[PASS]", line)


class FollowerTests(unittest.TestCase):
    def test_partial_lines_bad_lines_and_reset(self):
        with TempProject() as root:
            b = Bus(root)
            f = Follower(b.events)
            self.assertEqual(f.poll(), [])                      # no file yet
            b.post("a", None, "message", "one")
            with open(b.events, "a") as fh:
                fh.write('{"src": "b", "kind": "mess')            # a writer mid-line
            got = f.poll()
            self.assertEqual([e["text"] for e in got], ["one"])
            with open(b.events, "a") as fh:
                fh.write('age", "text": "two"}\nnot json\n\n[1, 2]\n')
            got = f.poll()
            self.assertEqual([e.get("text") for e in got], ["two"])
            self.assertEqual(f.poll(), [])
            dest = b.reset()
            self.assertTrue(dest and dest.exists() and dest.parent == root / ".crew" / "archive")
            self.assertEqual(b.events.stat().st_size, 0)
            b.post("c", None, "message", "three")
            self.assertEqual([e["text"] for e in f.poll()], ["three"])

    def test_reset_detected_even_when_new_file_is_longer(self):
        with TempProject() as root:
            b = Bus(root)
            b.post("a", None, "message", "short")
            f = Follower(b.events)
            f.poll()
            b.reset()
            for i in range(5):
                b.post("a", None, "message", f"long message number {i} " * 5)
            self.assertEqual(len(f.poll()), 5)

    def test_reset_of_empty_log(self):
        with TempProject() as root:
            b = Bus(root)
            self.assertIsNone(b.reset())
            self.assertTrue(b.events.exists())
            b.post("a", None, "note", "x")
            first = b.reset()
            b.post("a", None, "note", "y")
            second = b.reset()
            self.assertNotEqual(first, second)                  # same second, still two archives
            self.assertEqual(len(os.listdir(b.archive)), 2)


if __name__ == "__main__":
    unittest.main()
