import json
import unittest

from core import (
    CONTEXT_NEEDS,
    PlannerConfig,
    PlannerError,
    RoutePlan,
    build_planner_prompt,
    extract_plan,
)


def plan(**overrides):
    value = {
        "route": "reminder",
        "confidence": 0.96,
        "task": "两分钟后提醒我喝水",
        "mode": "read-only",
        "missing_context": [],
    }
    value.update(overrides)
    return value


class ExtractionTests(unittest.TestCase):
    def test_plain_json_and_markdown_fence_and_prose(self):
        payload = json.dumps(plan(), ensure_ascii=False)
        self.assertEqual(extract_plan(payload).route, "reminder")
        self.assertEqual(extract_plan("结果如下：```json\n" + payload + "\n```\n完成").task, "两分钟后提醒我喝水")

    def test_prefix_suffix_and_multiple_objects_fail_closed(self):
        payload = json.dumps(plan())
        self.assertEqual(extract_plan("好的\n" + payload + "\n请执行校验").confidence, 0.96)
        with self.assertRaises(PlannerError) as ctx:
            extract_plan(payload + "\n" + json.dumps(plan(route="chat")))
        self.assertEqual(ctx.exception.code, "invalid_json")

    def test_duplicate_key_is_rejected(self):
        with self.assertRaises(PlannerError) as ctx:
            extract_plan('{"route":"chat","route":"codex","confidence":0.9,"task":"x","mode":"read-only","missing_context":[]}')
        self.assertEqual(ctx.exception.code, "duplicate_key")

    def test_schema_enum_and_confidence_validation(self):
        cases = [
            (plan(route="shell"), "invalid_route"),
            (plan(confidence=1.1), "invalid_confidence"),
            (plan(confidence=True), "invalid_confidence"),
            (plan(mode="execute"), "invalid_mode"),
            (plan(missing_context=["unknown"]), "invalid_context"),
            (plan(extra="nope"), "schema_error"),
            ({k: v for k, v in plan().items() if k != "task"}, "schema_error"),
        ]
        for value, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(PlannerError) as ctx:
                    extract_plan(json.dumps(value))
                self.assertEqual(ctx.exception.code, code)

    def test_timeout_and_non_text_are_safe_failures(self):
        with self.assertRaises(PlannerError) as ctx:
            extract_plan(json.dumps(plan()), timeout=0)
        self.assertEqual(ctx.exception.code, "timeout")
        with self.assertRaises(PlannerError) as ctx:
            extract_plan(None)  # type: ignore[arg-type]
        self.assertEqual(ctx.exception.code, "invalid_response")


class ModelTests(unittest.TestCase):
    def test_round_trip_is_json_safe(self):
        original = RoutePlan.from_mapping(plan(missing_context=["timezone", "sender"]))
        encoded = json.dumps(original.to_mapping(), ensure_ascii=False)
        self.assertEqual(extract_plan(encoded), original)
        mapping = original.to_mapping()
        mapping["missing_context"].append("images")
        self.assertEqual(original.missing_context, ("timezone", "sender"))

    def test_config_defaults_are_safe_and_unknown_keys_rejected(self):
        config = PlannerConfig.from_mapping(None)
        self.assertEqual(config.mode, "regex")
        self.assertEqual(config.model, "gpt-6-astra")
        self.assertEqual(PlannerConfig.from_mapping({"mode": "light", "timeout_seconds": 2.5}).timeout, 2.5)
        with self.assertRaises(PlannerError):
            PlannerConfig.from_mapping({"mode": "light", "reasoning_effort": "low"})
        with self.assertRaises(PlannerError):
            PlannerConfig.from_mapping({"mode": "light", "timeout": 0})
        with self.assertRaises(PlannerError):
            PlannerConfig.from_mapping({"mode": "active"})

    def test_prompt_has_schema_and_never_requests_per_call_reasoning(self):
        prompt = build_planner_prompt("刚才的内容帮我设提醒", ["current_time", "timezone"])
        self.assertIn('"missing_context": array', prompt)
        self.assertNotIn("gpt-6-astra", prompt)  # provider is configured outside the prompt
        self.assertNotIn('"reasoning_effort"', prompt)
        self.assertEqual(set(CONTEXT_NEEDS), {"reminder", "schedule_reminder", "image", "codex", "web_search", "xhs_search", "chat", "clarify"})

    def test_prompt_rejects_untrusted_or_unknown_hints(self):
        with self.assertRaises(PlannerError):
            build_planner_prompt("hello", ["call_tool"])
        with self.assertRaises(PlannerError):
            build_planner_prompt("   ")


if __name__ == "__main__":
    unittest.main()
