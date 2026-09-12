import json
import unittest

from src.openai_research import OpenAIResearchProvider


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class OpenAIResearchProviderTests(unittest.TestCase):
    def test_sends_strict_responses_schema_and_parses_output(self):
        calls = []
        result = {
            "business_overview": [],
            "growth_drivers": [],
            "risks": [],
            "recent_developments": [],
            "unanswered_questions": ["What changes next?"],
        }

        def post(url, **kwargs):
            calls.append((url, kwargs))
            return FakeResponse({
                "output_text": json.dumps(result),
                "usage": {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150},
            })

        provider = OpenAIResearchProvider("test-key", model="test-model", post=post)
        response = provider.generate({"ticker": "TEST"}, "safe prompt")

        self.assertEqual(response, result)
        self.assertEqual(calls[0][0], "https://api.openai.com/v1/responses")
        body = calls[0][1]["json"]
        self.assertEqual(body["model"], "test-model")
        self.assertFalse(body["store"])
        self.assertTrue(body["text"]["format"]["strict"])
        self.assertEqual(body["text"]["format"]["type"], "json_schema")
        user_prompt = body["input"][1]["content"]
        self.assertEqual(user_prompt, "safe prompt")
        self.assertEqual(calls[0][1]["headers"]["Authorization"], "Bearer test-key")
        self.assertEqual(provider.last_usage["total_tokens"], 150)

    def test_extracts_nested_output_and_surfaces_refusal(self):
        provider = OpenAIResearchProvider(
            "test-key",
            post=lambda *args, **kwargs: FakeResponse({
                "output": [{"content": [{"type": "refusal", "refusal": "cannot comply"}]}]
            }),
        )

        with self.assertRaisesRegex(ValueError, "refused synthesis"):
            provider.generate({}, "prompt")

    def test_requires_api_key(self):
        with self.assertRaises(ValueError):
            OpenAIResearchProvider("")

    def test_reports_incomplete_response_reason_before_json_parsing(self):
        provider = OpenAIResearchProvider(
            "test-key",
            post=lambda *args, **kwargs: FakeResponse({
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output_text": "{\"unfinished\":",
            }),
        )

        with self.assertRaisesRegex(ValueError, "max_output_tokens"):
            provider.generate({}, "prompt")


if __name__ == "__main__":
    unittest.main()
