import importlib.util
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("harness_default_initializer", ROOT / "scripts/initialize_private.py")
initializer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(initializer)


def template(name):
    return json.loads((ROOT / "deploy/examples" / (name + ".json.example")).read_text())


class DefaultConfigurationTests(unittest.TestCase):
    def test_planner_deadline_and_model_defaults(self):
        router = template("intent_router_config")
        provider = template("planner_provider")
        self.assertEqual(router["mode"], "light")
        self.assertEqual(router["timeout_seconds"], 2.0)
        self.assertEqual(router["model"], provider["model"])
        self.assertEqual(provider["custom_extra_body"], {"reasoning_effort": "low", "max_tokens": 512})
        self.assertEqual(provider["key"], ["$MINIMAX_API_KEY"])

    def test_chat_is_disabled_until_private_configuration(self):
        chat = template("chat_provider")
        self.assertFalse(chat["enable"])
        self.assertEqual(chat["key"], ["$MODEL_API_KEY"])
        self.assertEqual(chat["providers"][0]["model"], "gpt-6-astra")
        self.assertEqual(chat["providers"][0]["modalities"], ["text", "image", "tool_use"])
        self.assertEqual(chat["providers"][1]["modalities"], ["text"])
        self.assertTrue(all(not value["enable"] for value in chat["providers"]))

    def test_plugin_examples_do_not_authorize_real_identities(self):
        names = ("codex_bridge", "image_context", "intent_router", "long_term_memory", "reminder", "travel_assistant", "xhs_search")
        for name in names:
            with self.subTest(plugin=name):
                config = template(name + "_config")
                self.assertEqual(config["allowed_umos"], [])
                self.assertEqual(config["allowed_senders"], [])
        codex = template("codex_bridge_config")
        self.assertEqual(codex["write_senders"], [])
        xhs = template("xhs_search_config")
        self.assertEqual(xhs["login_umos"], [])
        self.assertEqual(xhs["login_senders"], [])

    def test_initialized_chat_preserves_functional_defaults(self):
        config = initializer.build_config("synthetic-test-password", "https://api.example.com/v1", "gpt-6-astra", True)
        settings = config["provider_settings"]
        self.assertFalse(settings["enable"])
        self.assertFalse(settings["streaming_response"])
        self.assertFalse(settings["display_reasoning_text"])
        self.assertTrue(settings["datetime_system_prompt"])
        self.assertTrue(settings["web_search"])
        self.assertEqual(settings["computer_use_runtime"], "none")
        self.assertEqual(settings["image_compress_options"]["max_size"], 1280)
        self.assertEqual(settings["max_quoted_fallback_images"], 20)
        self.assertFalse(config["platform"][0]["enable"])
        self.assertEqual(config["platform"][0]["appid"], "")
        self.assertEqual(config["platform"][0]["secret"], "")
        self.assertEqual(config["admins_id"], [])
        self.assertEqual(config["platform_settings"]["id_whitelist"], ["__deny_all_until_configured__"])


if __name__ == "__main__":
    unittest.main()
