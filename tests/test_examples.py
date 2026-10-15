"""The shipped examples must stay loadable: an operator copies examples/bridge.toml.example and edits it, so a drifted example is a bug."""
import hashlib
import pathlib
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from bridge import config  # noqa: E402

EXAMPLE = HERE.parent / "examples" / "bridge.toml.example"


class TestExampleConfig(unittest.TestCase):
    def test_example_loads_and_defines_agents_and_a_default_model(self):
        cfg = config.load(str(EXAMPLE))
        self.assertEqual(sorted(cfg.forgejo_identities), ["alpha", "bravo"])
        self.assertIn(cfg.model_default, cfg.aliases)
        self.assertTrue(cfg.aliases[cfg.model_default].enabled)

    def test_example_agents_match_a_tokens_file(self):
        cfg = config.load(str(EXAMPLE))
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "tokens"
            path.write_text("".join(f"{agent} sha256={hashlib.sha256(agent.encode()).hexdigest()}\n"
                                    for agent in cfg.forgejo_identities))
            self.assertEqual(sorted(config.load_tokens(str(path), cfg.forgejo_identities)), ["alpha", "bravo"])


if __name__ == "__main__":
    unittest.main()
