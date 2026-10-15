"""Config loading: tables, limits, the model allowlist and the tokens file."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))


import pathlib
import tempfile
import unittest

from bridge import config
from fakes import ALIASES, AGENTS, write_tokens, write_toml


class TestConfig(unittest.TestCase):
    def test_minimal_valid_config_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config.load(str(write_toml(pathlib.Path(tmp) / "bridge.toml")))
            self.assertEqual(cfg.listen_port, 18780)
            self.assertEqual(cfg.model_default, "local")
            self.assertEqual(cfg.max_rounds, 6)
            self.assertEqual(cfg.round_max_time_s, 2700)
            self.assertEqual(cfg.max_active_tasks, 2)
            self.assertEqual(cfg.wait_timeout_cap_s, 3300)
            self.assertFalse(cfg.remove_merged_worktrees)

    def test_custom_limits_and_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            toml = write_toml(pathlib.Path(tmp) / "bridge.toml",
                              limits={"max_rounds": 3, "round_max_time_s": 60}, cleanup=True)
            cfg = config.load(str(toml))
            self.assertEqual(cfg.max_rounds, 3)
            self.assertEqual(cfg.round_max_time_s, 60)
            self.assertTrue(cfg.remove_merged_worktrees)

    def test_model_must_be_provider_slash_modelid(self):
        with tempfile.TemporaryDirectory() as tmp:
            aliases = {**ALIASES, "bad": {**ALIASES["mock"], "model": "bare-id"}}
            toml = write_toml(pathlib.Path(tmp) / "bridge.toml", aliases)
            with self.assertRaises(config.ConfigError):
                config.load(str(toml))

    def test_api_must_be_an_allowed_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            aliases = {**ALIASES, "bad": {**ALIASES["mock"], "api": "not-an-api"}}
            toml = write_toml(pathlib.Path(tmp) / "bridge.toml", aliases)
            with self.assertRaises(config.ConfigError):
                config.load(str(toml))

    def test_default_alias_must_exist_and_be_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(config.ConfigError):
                config.load(str(write_toml(pathlib.Path(tmp) / "bridge.toml", default="hosted")))

    def test_duplicate_provider_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            aliases = {**ALIASES, "twin": {**ALIASES["mock"], "model": "mock/m-b"}}
            toml = write_toml(pathlib.Path(tmp) / "bridge.toml", aliases)
            with self.assertRaises(config.ConfigError):
                config.load(str(toml))

    def test_missing_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "bridge.toml"
            path.write_text("[bridge]\nlisten_host = \"127.0.0.1\"\n")
            with self.assertRaises(config.ConfigError):
                config.load(str(path))


class TestTokens(unittest.TestCase):
    def test_all_five_agents_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            tokens = config.load_tokens(str(write_tokens(pathlib.Path(tmp) / "tokens")), AGENTS)
            self.assertEqual(sorted(tokens), sorted(AGENTS))
            self.assertTrue(all(len(digest) == 64 for digest in tokens.values()))

    def test_unknown_agent_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "tokens"
            write_tokens(path)
            with path.open("a") as handle:
                handle.write("mallory sha256=" + "0" * 64 + "\n")
            with self.assertRaises(config.ConfigError):
                config.load_tokens(str(path), AGENTS)

    def test_bad_digest_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "tokens"
            write_tokens(path)
            with path.open("a") as handle:
                handle.write("alpha sha256=xyz\n")
            with self.assertRaises(config.ConfigError):
                config.load_tokens(str(path), AGENTS)

    def test_an_agent_without_a_token_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "tokens"
            write_tokens(path)
            path.write_text("".join(line for line in path.read_text().splitlines(True) if not line.startswith("echo ")))
            with self.assertRaises(config.ConfigError):
                config.load_tokens(str(path), AGENTS)


if __name__ == "__main__":
    unittest.main()
