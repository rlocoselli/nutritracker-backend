import os
import unittest
from unittest.mock import Mock, patch

from app import (
    app,
    call_ai_provider,
    get_ai_provider,
    get_missing_env_vars,
    invoke_ai,
)


class AiProviderTests(unittest.TestCase):
    def test_prefers_mistral_when_available(self):
        with patch.dict(os.environ, {"MISTRAL_API_KEY": "mistral-key", "OPENAI_API_KEY": "openai-key"}, clear=True):
            provider = get_ai_provider()

            self.assertIsNotNone(provider)
            self.assertEqual(provider["provider"], "mistral")
            self.assertEqual(provider["api_key"], "mistral-key")
            self.assertEqual(provider["model"], "mistral-small-latest")

    def test_falls_back_to_openai_when_mistral_missing(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "openai-key"}, clear=True):
            provider = get_ai_provider()

            self.assertIsNotNone(provider)
            self.assertEqual(provider["provider"], "openai")
            self.assertEqual(provider["api_key"], "openai-key")
            self.assertEqual(provider["model"], "gpt-4.1-mini")

    def test_reports_missing_provider_keys(self):
        with patch.dict(os.environ, {}, clear=True):
            missing = get_missing_env_vars()

            self.assertEqual(missing, ["OPENAI_API_KEY", "GOOGLE_CLIENT_ID"])

    def test_mistral_alone_satisfies_internal_provider_configuration(self):
        with patch.dict(
            os.environ,
            {"MISTRAL_API_KEY": "mistral-key", "GOOGLE_CLIENT_ID": "client-id"},
            clear=True,
        ):
            self.assertEqual(get_missing_env_vars(), [])

    @patch("app.requests.post")
    def test_provider_request_enforces_json_without_changing_output(self, post):
        response = Mock()
        response.json.return_value = {
            "choices": [{"message": {"content": '{"schema_version":"1.0"}'}}]
        }
        post.return_value = response

        raw = call_ai_provider(
            {
                "api_key": "secret",
                "model": "model",
                "base_url": "https://provider.example/chat",
            },
            [{"role": "user", "content": "test"}],
            0.2,
        )

        self.assertEqual(raw, '{"schema_version":"1.0"}')
        sent_payload = post.call_args.kwargs["json"]
        self.assertEqual(sent_payload["response_format"], {"type": "json_object"})

    @patch("app.call_ai_provider")
    def test_mistral_failure_falls_back_to_openai_transparently(self, call_provider):
        call_provider.side_effect = [RuntimeError("mistral unavailable"), '{"ok":true}']

        with patch.dict(
            os.environ,
            {"MISTRAL_API_KEY": "mistral-key", "OPENAI_API_KEY": "openai-key"},
            clear=True,
        ):
            result = invoke_ai([{"role": "user", "content": "json"}], 0.2)

        self.assertEqual(result, '{"ok":true}')
        self.assertEqual(call_provider.call_count, 2)
        self.assertEqual(call_provider.call_args_list[0].args[0]["provider"], "mistral")
        self.assertEqual(call_provider.call_args_list[1].args[0]["provider"], "openai")


class ApiCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_missing_provider_preserves_legacy_error_payload(self):
        with patch.dict(
            os.environ,
            {"GOOGLE_CLIENT_ID": "client-id"},
            clear=True,
        ):
            response = self.client.post(
                "/api/analyze-meal",
                json={"text": "salad"},
                headers={"Authorization": "Bearer token"},
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.get_json(),
            {
                "error": "server_not_configured",
                "missing_env_vars": ["OPENAI_API_KEY"],
            },
        )

    @patch("app.save_meal_analysis")
    @patch("app.invoke_ai")
    @patch("app.verify_google_id_token")
    def test_analyze_meal_success_contract_is_provider_independent(
        self, verify_token, invoke_ai, save_meal
    ):
        verify_token.return_value = {"sub": "user-123"}
        invoke_ai.return_value = '{"meal":{"items":[],"totals":{}}}'

        with patch.dict(
            os.environ,
            {"MISTRAL_API_KEY": "mistral-key", "GOOGLE_CLIENT_ID": "client-id"},
            clear=True,
        ):
            response = self.client.post(
                "/api/analyze-meal",
                json={"lang": "fr", "text": "salad"},
                headers={"Authorization": "Bearer token"},
            )

        body = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["schema_version"], "1.0")
        self.assertEqual(body["user_id"], "user-123")
        self.assertIn("datetime_utc", body)
        self.assertEqual(body["meal"], {"items": [], "totals": {}})

    @patch("app.invoke_ai", side_effect=RuntimeError("provider-specific secret"))
    @patch("app.verify_google_id_token", return_value={"sub": "user-123"})
    def test_provider_failure_does_not_leak_provider_details(
        self, verify_token, invoke_ai
    ):
        with patch.dict(
            os.environ,
            {"MISTRAL_API_KEY": "mistral-key", "GOOGLE_CLIENT_ID": "client-id"},
            clear=True,
        ):
            response = self.client.post(
                "/api/recommendations",
                json={"goal": "maintenance"},
                headers={"Authorization": "Bearer token"},
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.get_json(), {"error": "model_request_failed"})


if __name__ == "__main__":
    unittest.main()
