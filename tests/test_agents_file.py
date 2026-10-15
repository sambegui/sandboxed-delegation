"""The shared workspace AGENTS.md reaches every worker round through --append-system-prompt.
omp only loads AGENTS.md from its working directory and each task runs in its own worktree, so without this flag the
file at the workspace root is never seen (PROBED on omp 18.9.2)."""
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from bridge import config
from bridge.runner_batch import build_argv
from fakes import write_toml

TASK = {"task_id": "t1", "worktree": "/sandbox/workspace/.worktrees/p/t1"}


def load(**kw):
    tmp = tempfile.TemporaryDirectory()
    try:
        return config.load(str(write_toml(pathlib.Path(tmp.name) / "bridge.toml", **kw)))
    finally:
        tmp.cleanup()


class AgentsFileTests(unittest.TestCase):
    def test_default_path_is_appended_to_every_batch_round_before_cwd(self):
        cfg = load()
        alias = cfg.aliases["local"]
        for continuing in (False, True):
            argv = build_argv(cfg, TASK, alias, continuing)
            i = argv.index("--append-system-prompt")
            self.assertEqual(argv[i + 1], "/sandbox/workspace/AGENTS.md")
            self.assertLess(i, argv.index("--cwd"))

    def test_empty_setting_disables_the_flag(self):
        cfg = load(agents_file="")
        self.assertNotIn("--append-system-prompt", build_argv(cfg, TASK, cfg.aliases["local"], False))

    def test_custom_path_is_used(self):
        cfg = load(agents_file="/sandbox/workspace/RULES.md")
        self.assertEqual(cfg.context_flags(), ["--append-system-prompt", "/sandbox/workspace/RULES.md"])

    def test_relative_path_is_rejected(self):
        with self.assertRaises(config.ConfigError):
            load(agents_file="AGENTS.md")


if __name__ == "__main__":
    unittest.main()
