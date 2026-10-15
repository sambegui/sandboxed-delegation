"""The model allowlist: resolution order, unknown/disabled aliases, and the rendered models.yml."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))


import tempfile
import unittest

from bridge import models
from fakes import Bridge


class TestResolve(unittest.TestCase):
    def test_resolution_order_explicit_then_agent_then_global(self):
        # build real alias objects through the config parser
        with tempfile.TemporaryDirectory() as tmp:
            bridge = Bridge(tmp)
            resolved = models.resolve(bridge.cfg.aliases, "mock", "local", None)
            self.assertEqual(resolved.alias, "mock")            # agent default beats global
            resolved = models.resolve(bridge.cfg.aliases, "mock", "local", "local")
            self.assertEqual(resolved.alias, "local")           # explicit beats everything
            resolved = models.resolve(bridge.cfg.aliases, None, "local", None)
            self.assertEqual(resolved.alias, "local")           # global fallback
            bridge.stop()

    def test_unknown_and_disabled_aliases(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = Bridge(tmp)
            with self.assertRaises(models.ModelError) as caught:
                models.resolve(bridge.cfg.aliases, None, "local", "nope")
            self.assertEqual(caught.exception.code, "unknown_model")
            self.assertIn("local", caught.exception.message)    # valid aliases are listed
            with self.assertRaises(models.ModelError) as caught:
                models.resolve(bridge.cfg.aliases, None, "local", "hosted")   # disabled
            self.assertEqual(caught.exception.code, "unknown_model")
            bridge.stop()


class TestRender(unittest.TestCase):
    def test_only_enabled_aliases_and_no_secret_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = Bridge(tmp)
            text = models.render_models_yml(bridge.cfg.aliases)
            self.assertIn('baseUrl: "http://host.openshell.internal:18770/v1"', text)
            self.assertIn("apiKey: LOCAL_RELAY_CLIENT_TOKEN", text)   # the NAME, never a value
            self.assertIn("api: openai-completions", text)
            self.assertNotIn("hosted/", text)                            # disabled alias is absent
            self.assertNotIn("token-", text)                           # no token values anywhere
            bridge.stop()

    def test_disabled_alias_api_renders_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = Bridge(tmp)
            hosted = bridge.cfg.aliases["hosted"]
            enabled = {**bridge.cfg.aliases, "hosted": hosted.__class__(**{**hosted.__dict__, "enabled": True})}
            text = models.render_models_yml(enabled)
            self.assertIn("api: anthropic-messages", text)
            self.assertIn('baseUrl: "http://host.openshell.internal:18765/v1"', text)
            bridge.stop()

    def test_reupload_only_on_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = Bridge(tmp)
            bridge.engine._ensure_ready()
            calls = [line for line in (bridge.fake / "calls.jsonl").read_text().splitlines()
                     if "upload" in line]
            self.assertEqual(len(calls), 2)     # models.yml + rpcd.sh
            bridge.engine._ensure_ready()       # same hashes: no new uploads
            calls = [line for line in (bridge.fake / "calls.jsonl").read_text().splitlines()
                     if "upload" in line]
            self.assertEqual(len(calls), 2)
            bridge.stop()


if __name__ == "__main__":
    unittest.main()
