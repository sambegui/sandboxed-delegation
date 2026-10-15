"""The sqlite store: revisions, rounds, checks, per-agent defaults, restart recovery."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))


import tempfile
import unittest

from fakes import Bridge


class TestStore(unittest.TestCase):
    def test_revision_bumps_on_every_state_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = Bridge(tmp)
            task_id = bridge.store.create_task("t1", "alpha", "demo", "spec", {"checks": []}, "batch", "main",
                                               "omp/x", "/wt", "abc", 6, "local")
            bridge.store.bump(task_id, "queued")
            first = bridge.store.get_task(task_id)["revision"]
            bridge.store.bump(task_id, "running")
            second = bridge.store.get_task(task_id)["revision"]
            self.assertEqual(second, first + 1)
            bridge.stop()

    def test_agent_scoping(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = Bridge(tmp)
            task_id = bridge.store.create_task("t1", "alpha", "demo", "spec", {"checks": []}, "batch", "main",
                                               "omp/x", "/wt", "abc", 6, "local")
            self.assertIsNone(bridge.store.get_task(task_id, agent="charlie"))
            self.assertIsNotNone(bridge.store.get_task(task_id, agent="alpha"))
            self.assertEqual(bridge.store.list_tasks("charlie"), [])
            self.assertEqual(len(bridge.store.list_tasks("alpha")), 1)
            bridge.stop()

    def test_rounds_checks_and_agent_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = Bridge(tmp)
            store = bridge.store
            pk = store.start_round("t1", 1, "local", None, 12)
            store.end_round(pk, "done", "stop", "hello", session_file="/sess/file")
            store.put_checks("t1", 1, [{"name": "verify", "ok": True, "exit_code": 0, "output_tail": ""}])
            row = store.last_round("t1")
            self.assertEqual(row["n"], 1)
            self.assertEqual(row["session_file"], "/sess/file")
            self.assertEqual(store.get_checks("t1", 1)[0]["name"], "verify")
            store.set_agent_default_model("alpha", "mock")
            self.assertEqual(store.agent_default_model("alpha"), "mock")
            self.assertIsNone(store.agent_default_model("charlie"))   # per-agent isolation
            bridge.stop()

    def test_live_tasks_for_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = Bridge(tmp)
            store = bridge.store
            live = store.create_task("ta", "alpha", "demo", "s", {"checks": []}, "batch", "main", "omp/a", "/w", "c",
                                     6, "local")
            store.bump(live, "running")
            done = store.create_task("tb", "alpha", "demo", "s", {"checks": []}, "batch", "main", "omp/b", "/w", "c",
                                     6, "local")
            store.bump(done, "closed")
            self.assertEqual([t["task_id"] for t in store.live_tasks()], [live])
            bridge.stop()


if __name__ == "__main__":
    unittest.main()
