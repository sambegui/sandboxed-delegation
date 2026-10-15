"""The engine end to end against the fake worker: submit, rounds, checks, feedback, hard stops, cancel,
close, restart recovery, per-agent isolation, concurrent tasks on one project, wait semantics."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))


import json
import tempfile
import threading
import time
import unittest

from bridge.engine import EngineError, parse_status_block
from fakes import Bridge, git


def final_task(bridge, agent, task_id):
    return bridge.engine.status(agent, task_id)


class TestBatchRounds(unittest.TestCase):
    def test_happy_path_done_with_passing_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = Bridge(tmp)
            bridge.make_project("demo")
            bridge.set_omp(action="write:out.txt:hello")
            view = bridge.submit()
            task = bridge.wait_status(view["task_id"], {"awaiting_review"})
            self.assertEqual(task["worker_status"], "done")
            self.assertTrue(all(c["ok"] for c in bridge.engine.status("alpha", view["task_id"])["checks"]))
            # the deterministic action landed in the task worktree
            worktree = bridge.mapped(task["worktree"])
            self.assertEqual((worktree / "out.txt").read_text(), "hello")
            # evidence landed in the shared workspace
            evidence = bridge.mapped("/sandbox/workspace/.omp-bridge") / view["task_id"]
            for name in ("spec.md", "status.json", "round-1.jsonl", "round-1-checks.json", "round-1-context.json", "result.md"):
                self.assertTrue((evidence / name).exists(), name)
            # the round records which context file the worker was given and whether it existed at the start
            context = json.loads((evidence / "round-1-context.json").read_text())
            self.assertEqual(context["append_system_prompt"], "/sandbox/workspace/AGENTS.md")
            self.assertFalse(context["file_present_at_round_start"])   # the test workspace has no AGENTS.md
            # worktree is locked and on its own branch
            self.assertTrue((bridge.mapped("/sandbox/workspace/demo/.git/worktrees") / view["task_id"] / "locked").exists())
            bridge.stop()

    def test_needs_info_carries_questions(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = Bridge(tmp)
            bridge.make_project("demo")
            bridge.set_omp(status="needs_info")
            view = bridge.submit()
            task = bridge.wait_status(view["task_id"], {"needs_info"})
            self.assertEqual(task["worker_status"], "needs_info")
            status = bridge.engine.status("alpha", view["task_id"])
            self.assertEqual(status["questions"], ["which file should hold the config?"])
            bridge.stop()

    def test_blocked_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = Bridge(tmp)
            bridge.make_project("demo")
            bridge.set_omp(status="blocked")
            view = bridge.submit()
            task = bridge.wait_status(view["task_id"], {"blocked"})
            self.assertEqual(task["worker_status"], "blocked")
            bridge.stop()

    def test_unclear_when_block_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = Bridge(tmp)
            bridge.make_project("demo")
            bridge.set_omp(status="unclear")
            view = bridge.submit()
            task = bridge.wait_status(view["task_id"], {"awaiting_review"})
            self.assertEqual(task["worker_status"], "unclear")
            bridge.stop()

    def test_failing_checks_then_feedback_next_round(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = Bridge(tmp)
            bridge.make_project("demo")
            bridge.set_omp(action="none")     # the check fails: out.txt never appears
            view = bridge.submit()
            task = bridge.wait_status(view["task_id"], {"awaiting_review"})
            self.assertFalse(all(c["ok"] for c in bridge.engine.status("alpha", view["task_id"])["checks"]))
            bridge.engine.feedback("alpha", view["task_id"],
                                   "FAKE_OMP_ACTION=write:out.txt:fixed\ncreate out.txt", "next_round")
            task = bridge.wait_status(view["task_id"], {"awaiting_review"})
            self.assertEqual(task["round"], 2)
            self.assertTrue(all(c["ok"] for c in bridge.engine.status("alpha", view["task_id"])["checks"]))
            self.assertEqual(json.loads(task["models_used"]), ["local"])
            # batch round 2 continued the same session (--continue) and saw the bigger history
            log = bridge.omp_log()
            self.assertEqual([entry["model"] for entry in log], ["local/model-small"] * 2)
            self.assertEqual(log[1]["n_messages"], 4)
            bridge.stop()


class TestStatusBlockParsing(unittest.TestCase):
    def test_parses_the_last_block(self):
        message = 'work\n```omp-bridge-status\n{"status":"done","summary":"x","questions":[]}\n```\nmore\n' \
                  '```omp-bridge-status\n{"status":"blocked","summary":"y","questions":[]}\n```'
        block = parse_status_block(message)
        self.assertEqual(block["status"], "blocked")

    def test_absent_or_invalid_is_none(self):
        self.assertIsNone(parse_status_block(None))
        self.assertIsNone(parse_status_block("nothing here"))
        self.assertIsNone(parse_status_block('```omp-bridge-status\n{"status":"weird"}\n```'))
        self.assertIsNone(parse_status_block('```omp-bridge-status\nnot json\n```'))


class TestHardStops(unittest.TestCase):
    def test_max_rounds_trips_needs_human(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = Bridge(tmp, limits={"max_rounds": 1})
            bridge.make_project("demo")
            bridge.set_omp(status="blocked")
            view = bridge.submit()
            bridge.wait_status(view["task_id"], {"needs_human"})
            task = bridge.store.get_task(view["task_id"])
            self.assertIn("max_rounds", task["error"])
            self.assertEqual(task["round"], 1)
            bridge.stop()

    def test_round_timeout_fails_resumably(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = Bridge(tmp, limits={"round_max_time_s": 2, "exec_grace_s": 1})
            bridge.make_project("demo")
            bridge.set_omp(action="sleep")
            view = bridge.submit()
            task = bridge.wait_status(view["task_id"], {"failed"}, timeout=60)
            self.assertEqual(task["error"], "round_timeout")
            bridge.stop()

    def test_max_active_tasks_across_agents(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = Bridge(tmp, limits={"max_active_tasks": 1})
            bridge.make_project("demo")
            bridge.set_omp(action="sleep")
            view = bridge.submit(agent="alpha")
            bridge.wait_status(view["task_id"], {"running"})
            with self.assertRaises(EngineError) as caught:
                bridge.submit(agent="charlie")
            self.assertEqual(caught.exception.code, "too_many_active")
            bridge.stop()


class TestCancelClose(unittest.TestCase):
    def test_cancel_kills_the_round(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = Bridge(tmp)
            bridge.make_project("demo")
            bridge.set_omp(action="sleep")
            view = bridge.submit()
            bridge.wait_status(view["task_id"], {"running"})
            bridge.engine.cancel("alpha", view["task_id"])
            task = bridge.wait_status(view["task_id"], {"cancelled"})
            self.assertEqual(task["status"], "cancelled")
            bridge.stop()

    def test_close_marks_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = Bridge(tmp)
            bridge.make_project("demo")
            bridge.set_omp(action="write:out.txt:hi")
            view = bridge.submit()
            bridge.wait_status(view["task_id"], {"awaiting_review"})
            bridge.engine.close("alpha", view["task_id"])
            self.assertEqual(bridge.store.get_task(view["task_id"])["status"], "closed")
            # worktree and branch stay
            task = bridge.store.get_task(view["task_id"])
            self.assertTrue(bridge.mapped(task["worktree"]).exists())
            bridge.stop()

    def test_close_removes_merged_worktree_only_when_opted_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = Bridge(tmp, cleanup=True)
            bridge.make_project("demo")
            view = bridge.submit(spec="FAKE_OMP_ACTION=write:out.txt:hi\nwork")
            task = bridge.wait_status(view["task_id"], {"awaiting_review"})
            worktree = bridge.mapped(task["worktree"])
            # a commit on the task branch, so the branch is genuinely ahead of main (not "merged")
            git(worktree, "add", "-A")
            git(worktree, "commit", "-q", "-m", "worker change")
            self.assertTrue(worktree.exists())
            # not merged yet: close keeps the worktree
            bridge.engine.close("alpha", view["task_id"])
            self.assertTrue(worktree.exists())
            # merge the branch, then a second task on the same project gets cleaned on close
            second = bridge.submit(spec="FAKE_OMP_ACTION=write:more.txt:x\nmore")
            task2 = bridge.wait_status(second["task_id"], {"awaiting_review"})
            git_dir = bridge.mapped("/sandbox/workspace/demo")
            git(git_dir, "merge", "--no-edit", task["branch"])
            bridge.engine.close("alpha", second["task_id"])
            self.assertFalse(bridge.mapped(task2["worktree"]).exists())
            bridge.stop()


class TestWorktreeConcurrency(unittest.TestCase):
    def test_two_tasks_one_project_get_distinct_worktrees_and_branches(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = Bridge(tmp)
            bridge.make_project("demo")
            first = bridge.submit(agent="alpha", spec="FAKE_OMP_ACTION=write:out.txt:a\ndo it")
            second = bridge.submit(agent="charlie", spec="FAKE_OMP_ACTION=write:out.txt:b\ndo it")
            self.assertNotEqual(first["branch"], second["branch"])
            self.assertNotEqual(first["worktree"], second["worktree"])
            t1 = bridge.wait_status(first["task_id"], {"awaiting_review"})
            t2 = bridge.wait_status(second["task_id"], {"awaiting_review"})
            wt1 = bridge.mapped(t1["worktree"]) / "out.txt"
            wt2 = bridge.mapped(t2["worktree"]) / "out.txt"
            self.assertEqual(wt1.read_text(), "a")
            self.assertEqual(wt2.read_text(), "b")
            # the primary checkout was never touched
            repo = bridge.mapped("/sandbox/workspace/demo")
            self.assertFalse((repo / "out.txt").exists())
            bridge.stop()

    def test_submit_race_on_one_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = Bridge(tmp)
            bridge.make_project("demo")
            bridge.set_omp(action="write:out.txt:x")
            results = []

            def submit(agent):
                results.append(bridge.submit(agent=agent))

            threads = [threading.Thread(target=submit, args=(agent,)) for agent in ("alpha", "charlie")]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(len({r["task_id"] for r in results}), 2)
            bridge.wait_status(results[0]["task_id"], {"awaiting_review"})
            bridge.wait_status(results[1]["task_id"], {"awaiting_review"})
            bridge.stop()


class TestRpcMode(unittest.TestCase):
    def test_session_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = Bridge(tmp)
            bridge.make_project("demo")
            bridge.set_omp(action="write:out.txt:rpc")
            view = bridge.submit(mode="rpc")
            task = bridge.wait_status(view["task_id"], {"awaiting_review"})
            self.assertEqual(task["worker_status"], "done")
            self.assertTrue((bridge.mapped(task["worktree"]) / "out.txt").exists())
            # the daemon spoke rpc: get_state recorded a session file for a later --resume
            round_row = bridge.store.last_round(view["task_id"])
            self.assertIn(".omp-sessions/", round_row["session_file"])   # fake maps the path
            bridge.stop()

    def test_steer_and_model_switch_on_feedback(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = Bridge(tmp)
            bridge.make_project("demo")
            view = bridge.submit(mode="rpc", spec="FAKE_OMP_ACTION=write:out.txt:one\nwork")
            bridge.wait_status(view["task_id"], {"awaiting_review"})
            bridge.engine.feedback("alpha", view["task_id"], "FAKE_OMP_ACTION=write:out.txt:two\nsecond round",
                                   "next_round", model="mock")
            task = bridge.wait_status(view["task_id"], {"awaiting_review"})
            self.assertEqual(json.loads(task["models_used"]), ["local", "mock"])
            log = bridge.omp_log()
            self.assertEqual(log[0]["model"], "local/model-small")
            self.assertEqual(log[1]["model"], "mock/m-a")
            bridge.stop()

    def test_steer_invalid_outside_running_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = Bridge(tmp)
            bridge.make_project("demo")
            bridge.set_omp(action="write:out.txt:x")
            view = bridge.submit(mode="rpc")
            task = bridge.wait_status(view["task_id"], {"awaiting_review"})
            with self.assertRaises(EngineError) as caught:
                bridge.engine.feedback("alpha", view["task_id"], "too late", "steer")
            self.assertEqual(caught.exception.code, "steer_unsupported")
            with self.assertRaises(EngineError) as caught:
                bridge.engine.feedback("alpha", view["task_id"], "huh", "steer", model="mock")
            self.assertEqual(caught.exception.code, "steer_unsupported")
            bridge.stop()

    def test_steer_reaches_a_running_session_round(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = Bridge(tmp)
            bridge.make_project("demo")
            # sleep keeps the round running so a steer is valid
            view = bridge.submit(mode="rpc", spec="FAKE_OMP_ACTION=sleep\nwork")
            bridge.wait_status(view["task_id"], {"running"})
            answer = bridge.engine.feedback("alpha", view["task_id"], "hurry up", "steer")
            self.assertEqual(answer["status"], "running")
            bridge.engine.cancel("alpha", view["task_id"])
            bridge.wait_status(view["task_id"], {"cancelled"})
            bridge.stop()


class TestModelsAcrossRounds(unittest.TestCase):
    def test_batch_model_change_on_next_round(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = Bridge(tmp)
            bridge.make_project("demo")
            bridge.set_omp(action="write:out.txt:a")
            view = bridge.submit()
            bridge.wait_status(view["task_id"], {"awaiting_review"})
            bridge.engine.feedback("alpha", view["task_id"], "FAKE_OMP_ACTION=write:out.txt:b\nagain",
                                   "next_round", model="mock")
            task = bridge.wait_status(view["task_id"], {"awaiting_review"})
            self.assertEqual(json.loads(task["models_used"]), ["local", "mock"])
            log = bridge.omp_log()
            self.assertEqual(log[1]["model"], "mock/m-a")
            # the second batch round resumed with --continue on a different model
            calls = [json.loads(line) for line in (bridge.fake / "calls.jsonl").read_text().splitlines()]
            omp_calls = [c for c in calls if c[:1] == ["sandbox"] and "omp" in c and "--continue" in c]
            self.assertTrue(any("mock/m-a" in c for c in omp_calls))
            bridge.stop()

    def test_unknown_model_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = Bridge(tmp)
            bridge.make_project("demo")
            from bridge.models import ModelError
            with self.assertRaises(ModelError) as caught:
                bridge.submit(model="nope")
            self.assertEqual(caught.exception.code, "unknown_model")
            bridge.stop()

    def test_per_agent_default_model_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = Bridge(tmp)
            bridge.make_project("demo")
            bridge.set_omp(action="write:out.txt:a")
            bridge.engine.set_default_model("alpha", "mock")
            self.assertEqual(bridge.engine.list_models("charlie")["your_default"], None)
            self.assertEqual(bridge.engine.list_models("alpha")["your_default"], "mock")
            view = bridge.submit(agent="alpha")    # alpha's default applies
            task = bridge.wait_status(view["task_id"], {"awaiting_review"})
            self.assertEqual(json.loads(task["models_used"]), ["mock"])
            self.assertEqual(bridge.omp_log()[0]["model"], "mock/m-a")
            bridge.stop()


class TestRecoveryAndWait(unittest.TestCase):
    def test_restart_recovery_marks_live_tasks_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = Bridge(tmp)
            bridge.make_project("demo")
            bridge.set_omp(action="write:out.txt:x")
            view = bridge.submit()
            bridge.wait_status(view["task_id"], {"awaiting_review"})
            # simulate a crash: a new engine over the same store, tasks left live by the old one
            bridge.store.bump(view["task_id"], "running")
            store2 = bridge.store
            engine2 = type(bridge.engine)(bridge.cfg, store2, bridge.shell)
            engine2.recover()
            self.assertEqual(store2.get_task(view["task_id"])["status"], "failed")
            self.assertEqual(store2.get_task(view["task_id"])["error"], "service_restart")
            bridge.stop()

    def test_wait_returns_immediately_when_revision_already_advanced(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = Bridge(tmp)
            bridge.make_project("demo")
            bridge.set_omp(action="write:out.txt:x")
            view = bridge.submit()
            task = bridge.wait_status(view["task_id"], {"awaiting_review"})
            result = bridge.engine.wait("alpha", view["task_id"], after=0, timeout=5)
            self.assertEqual(result["status"], "awaiting_review")
            self.assertGreater(result["revision"], 0)
            bridge.stop()

    def test_wait_times_out_and_repolls(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = Bridge(tmp)
            bridge.make_project("demo")
            bridge.set_omp(action="write:out.txt:x")
            view = bridge.submit()
            task = bridge.wait_status(view["task_id"], {"awaiting_review"})
            start = time.monotonic()
            result = bridge.engine.wait("alpha", view["task_id"], after=task["revision"], timeout=1)
            self.assertGreaterEqual(time.monotonic() - start, 1)
            self.assertEqual(result["status"], "awaiting_review")
            self.assertEqual(result["revision"], task["revision"])
            bridge.stop()


if __name__ == "__main__":
    unittest.main()
