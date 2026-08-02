import os
import unittest
from unittest.mock import Mock, patch

from app import (
    app,
    call_ai_provider,
    get_ai_provider,
    get_missing_env_vars,
    invoke_ai,
    verify_access_token,
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

    @patch("app.verify_access_token")
    def test_current_user_returns_verified_profile(self, verify_token):
        verify_token.return_value = {
            "sub": "user-123",
            "email": "alex@example.com",
            "name": "Alex",
            "auth_provider": "google",
        }

        response = self.client.get(
            "/api/me", headers={"Authorization": "Bearer token"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["user"]["id"], "user-123")
        self.assertEqual(response.get_json()["user"]["auth_provider"], "google")

    def test_current_user_requires_bearer_token(self):
        response = self.client.get("/api/me")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json(), {"error": "missing_bearer_token"})


class MobileAuthenticationTests(unittest.TestCase):
    @patch("app.requests.post")
    @patch("app.verify_google_id_token", side_effect=ValueError("not google"))
    def test_mobile_token_uses_configured_server_verifier(self, google_verify, post):
        post.return_value.json.return_value = {
            "active": True,
            "user_id": "mobile-42",
            "name": "Mobile User",
        }
        post.return_value.raise_for_status.return_value = None

        with patch.dict(
            os.environ,
            {"MOBILE_AUTH_VERIFY_URL": "https://auth.example/introspect"},
            clear=True,
        ):
            identity = verify_access_token("mobile-token")

        self.assertEqual(identity["sub"], "mobile-42")
        self.assertEqual(identity["auth_provider"], "mobile")
        post.assert_called_once_with(
            "https://auth.example/introspect",
            headers={"Authorization": "Bearer mobile-token"},
            timeout=10,
        )


class SharedMobileAccountTests(unittest.TestCase):
    def setUp(self):
        app.config.update(
            TESTING=True,
            SECRET_KEY="test-session-secret",
            SESSION_COOKIE_SECURE=False,
        )
        self.client = app.test_client()

    @patch("app.mobile_api_request")
    def test_email_login_reads_same_mobile_meals(self, mobile_request):
        login_response = Mock(status_code=200)
        login_response.json.return_value = {
            "user_id": "9c964d41-ff1a-4b51-960a-2341bddf18ec",
            "email": "alex@example.com",
            "name": "Alex",
        }
        login_response.raise_for_status.return_value = None
        history_response = Mock(status_code=200)
        history_response.json.return_value = [
            {
                "id": "meal-1",
                "date_utc": "2026-08-01T12:00:00Z",
                "total_calories": 540,
            }
        ]
        history_response.raise_for_status.return_value = None
        mobile_request.side_effect = [login_response, history_response]

        login = self.client.post(
            "/api/account/email/login",
            json={"email": "alex@example.com", "password": "secret12"},
        )
        history = self.client.get("/api/account/history")

        self.assertEqual(login.status_code, 200)
        self.assertEqual(login.get_json()["user"]["auth_provider"], "email")
        self.assertEqual(history.status_code, 200)
        self.assertEqual(history.get_json()["meals"][0]["id"], "meal-1")
        self.assertEqual(
            mobile_request.call_args_list[1].kwargs["headers"]["X-User-Id"],
            "9c964d41-ff1a-4b51-960a-2341bddf18ec",
        )

    def test_mobile_history_requires_browser_session(self):
        response = self.client.get("/api/account/history")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json(), {"error": "account_session_required"})

    @patch("app.mobile_api_request")
    def test_dashboard_combines_mobile_meals_goals_water_and_points(self, mobile_request):
        payloads = [
            [{"id": "meal-1", "total_calories": 540}],
            {"calories_target": 2100, "protein_g_target": 125, "carbs_g_target": 230},
            {"liters": 1.5},
            {"balance": 80},
        ]
        responses = []
        for payload in payloads:
            response = Mock(status_code=200)
            response.json.return_value = payload
            response.raise_for_status.return_value = None
            responses.append(response)
        mobile_request.side_effect = responses

        with self.client.session_transaction() as browser_session:
            browser_session["mobile_user_id"] = "9c964d41-ff1a-4b51-960a-2341bddf18ec"
            browser_session["mobile_profile"] = {"name": "Alex"}

        response = self.client.get("/api/account/dashboard?days=30")

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["goals"]["calories_target"], 2100)
        self.assertEqual(body["water"]["liters"], 1.5)
        self.assertEqual(body["points"]["balance"], 80)


if __name__ == "__main__":
    unittest.main()
