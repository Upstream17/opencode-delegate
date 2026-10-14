"""Unit tests for default-model resolution (offline, no OpenCode process).

Covers the precedence between a per-task model, OC_DELEGATE_MODEL and the default model read from
OpenCode's own configuration, plus the `opencode debug config` parsing that feeds it.
"""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import runtime  # noqa: E402


class ConfigSourcesTest(unittest.TestCase):
    def test_reads_normalized_object_and_plain_string(self):
        sources = [
            {"type": "document", "path": "global.jsonc", "info": {"model": "opencode-go/glm-5.3"}},
            {"type": "directory", "path": "global"},
        ]
        self.assertEqual(runtime._model_from_config_sources(sources), "opencode-go/glm-5.3")
        normalized = [{"type": "document", "info": {"model": {"providerID": "opencode-go", "model": "glm-5.3"}}}]
        self.assertEqual(runtime._model_from_config_sources(normalized), "opencode-go/glm-5.3")

    def test_later_source_wins_and_variant_is_kept(self):
        sources = [
            {"type": "document", "info": {"model": "opencode-go/glm-5.3"}},
            {"type": "document",
             "info": {"model": {"providerID": "opencode-go", "model": "deepseek-v4.1-flash",
                                "variant": "high"}}},
        ]
        self.assertEqual(runtime._model_from_config_sources(sources), "opencode-go/deepseek-v4.1-flash#high")

    def test_malformed_sources_are_ignored(self):
        self.assertIsNone(runtime._model_from_config_sources(None))
        self.assertIsNone(runtime._model_from_config_sources({}))
        self.assertIsNone(runtime._model_from_config_sources([{"type": "directory", "path": "x"}]))
        self.assertIsNone(runtime._model_from_config_sources([{"info": {"model": {}}}]))
        self.assertIsNone(runtime._model_from_config_sources([{"info": {"model": {"providerID": "go"}}}]))
        self.assertIsNone(runtime._model_from_config_sources([{"info": {"model": "   "}}]))

    def test_a_source_without_a_model_does_not_erase_an_earlier_one(self):
        sources = [{"info": {"model": "opencode-go/glm-5.3"}}, {"info": {"$schema": "x"}}]
        self.assertEqual(runtime._model_from_config_sources(sources), "opencode-go/glm-5.3")


class OpenCodeDefaultModelTest(unittest.TestCase):
    def setUp(self):
        runtime._CONFIGURED_MODEL.clear()
        self.addCleanup(runtime._CONFIGURED_MODEL.clear)
        prefix = mock.patch.object(runtime.delegate, "command_prefix", return_value=["opencode"])
        prefix.start()
        self.addCleanup(prefix.stop)

    def _blocking(self, payload, code=0, timed_out=False):
        return mock.patch.object(runtime.delegate, "run_blocking",
                                 return_value=(code, payload, "", timed_out))

    def test_reads_the_configured_default(self):
        payload = json.dumps([{"info": {"model": "opencode-go/deepseek-v4.1-flash"}}])
        with self._blocking(payload):
            self.assertEqual(runtime.opencode_default_model("C:/work"), "opencode-go/deepseek-v4.1-flash")

    def test_success_is_cached_but_a_miss_is_retried(self):
        with self._blocking(json.dumps([])) as blocked:
            self.assertIsNone(runtime.opencode_default_model("C:/work"))
            runtime.opencode_default_model("C:/work")
            self.assertEqual(blocked.call_count, 2)
        good = json.dumps([{"info": {"model": "opencode-go/glm-5.3"}}])
        with self._blocking(good) as blocked:
            self.assertEqual(runtime.opencode_default_model("C:/work"), "opencode-go/glm-5.3")
            runtime.opencode_default_model("C:/work")
            self.assertEqual(blocked.call_count, 1)

    def test_failures_and_garbage_yield_no_model(self):
        with self._blocking("", code=1):
            self.assertIsNone(runtime.opencode_default_model("C:/work"))
        with self._blocking("not json"):
            self.assertIsNone(runtime.opencode_default_model("C:/work"))
        with self._blocking("[]", timed_out=True):
            self.assertIsNone(runtime.opencode_default_model("C:/work"))
        with mock.patch.object(runtime.delegate, "command_prefix", side_effect=ValueError("missing")):
            self.assertIsNone(runtime.opencode_default_model("C:/work"))


class ResolveModelTest(unittest.TestCase):
    def setUp(self):
        runtime._CONFIGURED_MODEL.clear()
        self.addCleanup(runtime._CONFIGURED_MODEL.clear)

    def test_requested_model_wins_over_everything(self):
        with mock.patch.object(runtime, "DEFAULT_MODEL", "opencode-go/glm-5.3"):
            with mock.patch.object(runtime, "opencode_default_model", return_value="opencode-go/x"):
                self.assertEqual(runtime.resolve_model("opencode-go/y", "C:/w"),
                                 ("opencode-go/y", "requested"))

    def test_env_wins_over_the_configured_default(self):
        with mock.patch.object(runtime, "DEFAULT_MODEL", "opencode-go/glm-5.3"):
            with mock.patch.object(runtime, "opencode_default_model") as configured:
                self.assertEqual(runtime.resolve_model(None, "C:/w"), ("opencode-go/glm-5.3", "OC_DELEGATE_MODEL"))
                configured.assert_not_called()

    def test_configured_default_is_used_when_the_env_is_unset(self):
        with mock.patch.object(runtime, "DEFAULT_MODEL", None):
            with mock.patch.object(runtime, "opencode_default_model", return_value="opencode-go/glm-5.3"):
                self.assertEqual(runtime.resolve_model(None, "C:/w"), ("opencode-go/glm-5.3", "opencode-config"))

    def test_nothing_configured_leaves_the_choice_to_opencode(self):
        with mock.patch.object(runtime, "DEFAULT_MODEL", None):
            with mock.patch.object(runtime, "opencode_default_model", return_value=None):
                self.assertEqual(runtime.resolve_model(None, "C:/w"), (None, None))


if __name__ == "__main__":
    unittest.main()
