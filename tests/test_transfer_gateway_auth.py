"""API Gateway authentication client, Build 4: configuration, login,
refresh, token lifecycle, concurrency, retries, redaction, and integration
boundaries. Fully offline - fake transports and frozen clocks only; no live
network call exists anywhere in this file."""

import json
import logging
import threading
from pathlib import Path

import pytest

from apps.web.transfer import gateway_auth as ga
from apps.web.transfer.gateway_auth import (
    ApiGatewayAuthClient,
    ApiGatewayAuthConfig,
    ApiGatewayCredentials,
    AuthError,
    TokenCache,
    TransportFailure,
    TransportTimeout,
    redact,
)

ROOT = Path(__file__).resolve().parent.parent

SECRET_PASSWORD = "S3cr3t-P@ss"
ACCESS_1 = "access-token-AAA111"
ACCESS_2 = "access-token-BBB222"
REFRESH_1 = "refresh-token-RRR111"
REFRESH_2 = "refresh-token-SSS222"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("API_GATEWAY_BASE_URL", "API_GATEWAY_LOGIN_PATH",
                 "API_GATEWAY_REFRESH_PATH", "API_GATEWAY_CLIENT_ID",
                 "API_GATEWAY_SUCCESS_CODE", "API_GATEWAY_TIMEOUT_SECONDS",
                 "API_GATEWAY_MAX_AUTH_RETRIES",
                 "API_GATEWAY_EXPIRY_SKEW_SECONDS", "API_GATEWAY_LOCALE",
                 "API_GATEWAY_USER_ID", "API_GATEWAY_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    ga.reset_default_cache()


def ok_envelope(access=ACCESS_1, refresh=REFRESH_1, expire_in=86400,
                code=100000):
    data = {"accessToken": access, "expire_in": expire_in}
    if refresh is not None:
        data["refreshToken"] = refresh
    return {"status": "successful", "code": code,
            "reason": "Operation/Data Retrieval successful!",
            "note": "15759HK|User@HK001", "data": data}


class FakeTransport:
    """Scripted transport: each entry is (status, body) or an exception."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def post_json(self, url, body, *, timeout):
        self.calls.append((url, body))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def make_client(responses, *, clock=None, cache=None, retries=2,
                skew=60, success_code=100000):
    config = ApiGatewayAuthConfig(
        base_url="https://gw.example.test/devgapi",
        max_auth_retries=retries, expiry_skew_seconds=skew,
        success_code=success_code)
    creds = ApiGatewayCredentials(user_id="15759HK",
                                  password=SECRET_PASSWORD)
    transport = FakeTransport(responses)
    sleeps = []
    client = ApiGatewayAuthClient(config, creds, transport=transport,
                                  cache=cache or TokenCache(),
                                  clock=clock or Clock(),
                                  sleep=sleeps.append)
    client._test_sleeps = sleeps
    return client, transport


# --- configuration ----------------------------------------------------------------

class TestConfiguration:
    def _set_valid(self, monkeypatch):
        monkeypatch.setenv("API_GATEWAY_BASE_URL",
                           "https://websrv.imaginex.hk:8443/devgapi/")
        monkeypatch.setenv("API_GATEWAY_USER_ID", "user1")
        monkeypatch.setenv("API_GATEWAY_PASSWORD", "pass1")

    def test_valid_config_loads_with_normalization(self, monkeypatch):
        self._set_valid(monkeypatch)
        monkeypatch.setenv("API_GATEWAY_LOGIN_PATH", "auth/login/")
        config = ga.load_auth_config()
        assert config.base_url == "https://websrv.imaginex.hk:8443/devgapi"
        assert config.login_path == "/auth/login"
        assert config.login_url == ("https://websrv.imaginex.hk:8443"
                                    "/devgapi/auth/login")
        assert config.client_id == "Web-Label"          # confirmed default
        assert config.success_code == 100000
        assert config.locale == "en-US"

    def test_missing_base_url_is_configuration_error(self):
        with pytest.raises(AuthError) as err:
            ga.load_auth_config()
        assert err.value.code == ga.AUTH_CONFIGURATION_ERROR

    def test_invalid_numeric_values_reported(self, monkeypatch):
        self._set_valid(monkeypatch)
        monkeypatch.setenv("API_GATEWAY_SUCCESS_CODE", "not-a-number")
        monkeypatch.setenv("API_GATEWAY_TIMEOUT_SECONDS", "0")
        state = ga.readiness()
        assert state["status"] == "configuration_error"
        joined = " ".join(state["problems"])
        assert "API_GATEWAY_SUCCESS_CODE" in joined
        assert "API_GATEWAY_TIMEOUT_SECONDS" in joined

    def test_missing_credentials_produce_clear_error(self, monkeypatch):
        monkeypatch.setenv("API_GATEWAY_BASE_URL", "https://gw.test")
        with pytest.raises(AuthError) as err:
            ga.load_credentials()
        assert err.value.code == ga.AUTH_CONFIGURATION_ERROR
        assert "API_GATEWAY_USER_ID" in str(err.value)

    def test_blank_credentials_rejected(self, monkeypatch):
        self._set_valid(monkeypatch)
        monkeypatch.setenv("API_GATEWAY_PASSWORD", "   ")
        with pytest.raises(AuthError):
            ga.load_credentials()

    def test_readiness_states(self, monkeypatch):
        assert ga.readiness()["status"] == "not_configured"
        self._set_valid(monkeypatch)
        assert ga.readiness() == {"status": "configured", "problems": []}
        monkeypatch.setenv("API_GATEWAY_BASE_URL", "not-a-url")
        assert ga.readiness()["status"] == "configuration_error"

    def test_readiness_never_exposes_values(self, monkeypatch):
        self._set_valid(monkeypatch)
        monkeypatch.setenv("API_GATEWAY_PASSWORD", "super-secret-pw")
        monkeypatch.setenv("API_GATEWAY_SUCCESS_CODE", "bad")
        blob = json.dumps(ga.readiness())
        assert "super-secret-pw" not in blob
        assert "user1" not in blob

    def test_credentials_repr_suppressed(self):
        creds = ApiGatewayCredentials(user_id="u", password="p-secret")
        assert "p-secret" not in repr(creds)
        assert "p-secret" not in str(creds)


# --- login ------------------------------------------------------------------------

class TestLogin:
    def test_successful_login_contract_and_caching(self):
        client, transport = make_client([(200, ok_envelope())])
        tokens = client.login()
        url, body = transport.calls[0]
        assert url == "https://gw.example.test/devgapi/auth/login"
        # exact confirmed request body
        assert body == {"client": "Web-Label", "userId": "15759HK",
                        "password": SECRET_PASSWORD, "locale": "en-US"}
        assert tokens.access_token == ACCESS_1
        assert tokens.refresh_token == REFRESH_1
        assert tokens.expires_at == pytest.approx(1_000_000.0 + 86400)
        assert client.cache.get() is tokens

    def test_http_200_with_failure_code_fails(self):
        # spec: HTTP 200 alone is NOT success; code 100001 = bad credentials
        client, _ = make_client([(200, {
            "status": "failed", "code": 100001,
            "reason": "Failed to validate user.",
            "note": "Submitted credentials are not correct.", "data": None})])
        with pytest.raises(AuthError) as err:
            client.login()
        assert err.value.code == ga.AUTH_LOGIN_FAILED
        assert err.value.gateway_code == 100001
        assert err.value.retryable is False
        assert client.cache.get() is None

    def test_http_401_login_fails_without_retry(self):
        client, transport = make_client([(401, {"code": 100001})])
        with pytest.raises(AuthError) as err:
            client.login()
        assert err.value.code == ga.AUTH_LOGIN_FAILED
        assert len(transport.calls) == 1              # 4xx never retried

    def test_non_2xx_is_gateway_rejected(self):
        client, _ = make_client([(403, {"code": 100002})])
        with pytest.raises(AuthError) as err:
            client.login()
        assert err.value.code == ga.AUTH_GATEWAY_REJECTED
        assert err.value.http_status == 403

    def test_malformed_json_is_response_invalid(self):
        client, _ = make_client([(200, None)])
        with pytest.raises(AuthError) as err:
            client.login()
        assert err.value.code == ga.AUTH_RESPONSE_INVALID

    def test_missing_or_empty_token_fields_fail(self):
        for data in ({}, {"accessToken": ""}, {"accessToken": "   "}):
            env = ok_envelope()
            env["data"] = data
            client, _ = make_client([(200, env)])
            with pytest.raises(AuthError) as err:
                client.login()
            assert err.value.code == ga.AUTH_RESPONSE_INVALID

    def test_expire_in_as_numeric_string_parsed(self):
        env = ok_envelope()
        env["data"]["expire_in"] = "3600"
        client, _ = make_client([(200, env)])
        tokens = client.login()
        assert tokens.expires_at == pytest.approx(1_000_000.0 + 3600)

    def test_no_expiry_metadata_gives_open_ended_token(self):
        env = ok_envelope()
        del env["data"]["expire_in"]
        client, _ = make_client([(200, env)])
        assert client.login().expires_at is None

    def test_transient_5xx_retried_then_succeeds(self):
        client, transport = make_client([(503, None), (200, ok_envelope())])
        tokens = client.login()
        assert tokens.access_token == ACCESS_1
        assert len(transport.calls) == 2
        assert client._test_sleeps                     # backoff injected

    def test_retry_exhaustion_is_typed(self):
        client, transport = make_client(
            [TransportTimeout("t"), (500, None), TransportFailure("f")],
            retries=2)
        with pytest.raises(AuthError) as err:
            client.login()
        assert err.value.code == ga.AUTH_RETRY_EXHAUSTED
        assert len(transport.calls) == 3               # 1 + 2 retries, no more

    def test_password_never_in_errors_or_logs(self, caplog):
        with caplog.at_level(logging.DEBUG,
                             logger="invoice_extractor.transfer.gateway_auth"):
            client, _ = make_client([(200, {"status": "failed",
                                            "code": 100001, "data": None})])
            with pytest.raises(AuthError) as err:
                client.login()
        blob = str(err.value) + " " + " ".join(r.getMessage()
                                               for r in caplog.records)
        assert SECRET_PASSWORD not in blob
        assert ACCESS_1 not in blob


# --- refresh ----------------------------------------------------------------------

def logged_in_client(extra_responses, **kwargs):
    client, transport = make_client([(200, ok_envelope())] + extra_responses,
                                    **kwargs)
    client.login()
    return client, transport


class TestRefresh:
    def test_successful_refresh_contract(self):
        client, transport = logged_in_client(
            [(200, ok_envelope(access=ACCESS_2, refresh=None))])
        tokens = client.refresh()
        url, body = transport.calls[1]
        assert url == "https://gw.example.test/devgapi/auth/refresh"
        assert body == {"rt": REFRESH_1}               # exact confirmed body
        assert tokens.access_token == ACCESS_2

    def test_rotated_refresh_token_replaces_old(self):
        client, _ = logged_in_client(
            [(200, ok_envelope(access=ACCESS_2, refresh=REFRESH_2))])
        assert client.refresh().refresh_token == REFRESH_2

    def test_omitted_replacement_keeps_old_refresh_token(self):
        # spec: NEVER overwrite the stored refresh token with empty/null
        for omitted in (None, ""):
            client, _ = logged_in_client(
                [(200, ok_envelope(access=ACCESS_2, refresh=omitted))])
            assert client.refresh().refresh_token == REFRESH_1

    def test_refresh_without_cached_token_is_token_missing(self):
        client, _ = make_client([])
        with pytest.raises(AuthError) as err:
            client.refresh()
        assert err.value.code == ga.AUTH_TOKEN_MISSING

    def test_rejected_refresh_is_typed(self):
        client, _ = logged_in_client([(401, {"code": 100001})])
        with pytest.raises(AuthError) as err:
            client.refresh()
        assert err.value.code == ga.AUTH_REFRESH_FAILED

    def test_malformed_refresh_response(self):
        client, _ = logged_in_client([(200, None)])
        with pytest.raises(AuthError) as err:
            client.refresh()
        assert err.value.code == ga.AUTH_RESPONSE_INVALID

    def test_refresh_updates_expiry(self):
        clock = Clock()
        client, _ = logged_in_client(
            [(200, ok_envelope(access=ACCESS_2, expire_in=7200))],
            clock=clock)
        clock.now += 1000
        tokens = client.refresh()
        assert tokens.expires_at == pytest.approx(clock.now + 7200)


# --- token lifecycle --------------------------------------------------------------

class TestLifecycle:
    def test_valid_cached_token_reused_without_requests(self):
        client, transport = logged_in_client([])
        assert client.ensure_access_token() == ACCESS_1
        assert client.ensure_access_token() == ACCESS_1
        assert len(transport.calls) == 1               # only the login

    def test_expired_token_triggers_refresh(self):
        clock = Clock()
        client, transport = logged_in_client(
            [(200, ok_envelope(access=ACCESS_2))], clock=clock)
        clock.now += 86400 + 1                          # past expiry
        assert not client.has_valid_access_token()
        assert client.ensure_access_token() == ACCESS_2
        assert transport.calls[1][0].endswith("/auth/refresh")

    def test_skew_invalidates_before_actual_expiry(self):
        clock = Clock()
        client, _ = logged_in_client(
            [(200, ok_envelope(access=ACCESS_2))], clock=clock, skew=120)
        clock.now += 86400 - 100                        # inside skew window
        assert not client.has_valid_access_token()

    def test_no_expiry_token_valid_until_invalidated(self):
        env = ok_envelope()
        del env["data"]["expire_in"]
        client, transport = make_client([(200, env),
                                         (200, ok_envelope(access=ACCESS_2))])
        client.login()
        assert client.has_valid_access_token()
        client.invalidate_access_token()
        assert not client.has_valid_access_token()
        assert client.ensure_access_token() == ACCESS_2  # via refresh
        assert len(transport.calls) == 2

    def test_refresh_rejection_causes_one_fresh_login(self):
        clock = Clock()
        client, transport = logged_in_client(
            [(401, {"code": 100001}),                  # refresh rejected
             (200, ok_envelope(access=ACCESS_2))],     # exactly one login
            clock=clock)
        clock.now += 86400 + 1
        assert client.ensure_access_token() == ACCESS_2
        urls = [u for u, _ in transport.calls]
        assert urls[1].endswith("/auth/refresh")
        assert urls[2].endswith("/auth/login")
        assert len(urls) == 3                          # no loops

    def test_refresh_transport_retry_then_success(self):
        clock = Clock()
        client, transport = logged_in_client(
            [TransportFailure("net"), (200, ok_envelope(access=ACCESS_2))],
            clock=clock)
        clock.now += 86400 + 1
        assert client.ensure_access_token() == ACCESS_2
        assert len(transport.calls) == 3               # login + 2 refresh tries

    def test_final_failure_typed_no_infinite_retry(self):
        clock = Clock()
        client, transport = logged_in_client(
            [(401, {"code": 100001}),                  # refresh rejected
             (401, {"code": 100001})],                 # fallback login fails
            clock=clock)
        clock.now += 86400 + 1
        with pytest.raises(AuthError) as err:
            client.ensure_access_token()
        assert err.value.code == ga.AUTH_LOGIN_FAILED
        assert len(transport.calls) == 3               # login+refresh+login

    def test_clear_tokens(self):
        client, _ = logged_in_client([])
        client.clear_tokens()
        assert client.cache.get() is None
        assert not client.has_valid_access_token()

    def test_handle_unauthorized_invalidates_and_reacquires(self):
        client, transport = logged_in_client(
            [(200, ok_envelope(access=ACCESS_2))])
        new_token = client.handle_unauthorized()
        assert new_token == ACCESS_2
        assert transport.calls[1][0].endswith("/auth/refresh")

    def test_authorization_header_shape(self):
        client, _ = logged_in_client([])
        assert client.get_authorization_header() == {
            "Authorization": f"Bearer {ACCESS_1}"}


# --- concurrency ------------------------------------------------------------------

class TestConcurrency:
    def test_simultaneous_ensure_causes_single_login(self):
        started = threading.Barrier(4)

        class SlowTransport(FakeTransport):
            def post_json(self, url, body, *, timeout):
                result = super().post_json(url, body, timeout=timeout)
                return result

        config = ApiGatewayAuthConfig(base_url="https://gw.test")
        creds = ApiGatewayCredentials(user_id="u", password="p")
        transport = SlowTransport([(200, ok_envelope())])
        client = ApiGatewayAuthClient(config, creds, transport=transport,
                                      cache=TokenCache(), clock=Clock(),
                                      sleep=lambda s: None)
        results = []

        def worker():
            started.wait()
            results.append(client.ensure_access_token())

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert results == [ACCESS_1] * 4
        assert len(transport.calls) == 1               # one login total


# --- redaction --------------------------------------------------------------------

class TestRedaction:
    def test_nested_payload_case_insensitive(self):
        payload = {
            "User": "u1",
            "Password": "p1",
            "data": {"accessToken": "tok-a", "refresh_token": "tok-r",
                     "expire_in": 86400,
                     "nested": [{"AUTHORIZATION": "Bearer xyz",
                                 "Client-Secret": "shh-cs-9"}]},
            "rt": "tok-r2",
            "carton": "001",                            # NOT sensitive
        }
        cleaned = redact(payload)
        blob = json.dumps(cleaned)
        for secret in ("p1", "tok-a", "tok-r", "Bearer xyz", "shh-cs-9",
                       "tok-r2"):
            assert secret not in blob
        assert cleaned["User"] == "u1"
        assert cleaned["carton"] == "001"               # untouched
        assert cleaned["data"]["expire_in"] == 86400

    def test_token_set_repr_suppressed(self):
        tokens = ga.ApiGatewayTokenSet(access_token="tok-a",
                                       refresh_token="tok-r",
                                       obtained_at=1.0)
        assert "tok-a" not in repr(tokens)
        assert "tok-r" not in str(tokens)

    def test_auth_error_carries_only_safe_metadata(self):
        err = AuthError(ga.AUTH_LOGIN_FAILED, "rejected", operation="login",
                        http_status=401, gateway_code=100001)
        blob = str(err)
        assert "[AUTH_LOGIN_FAILED]" in blob
        assert "http=401" in blob and "gateway_code=100001" in blob

    def test_logs_never_contain_tokens_across_full_lifecycle(self, caplog):
        with caplog.at_level(logging.DEBUG,
                             logger="invoice_extractor.transfer.gateway_auth"):
            clock = Clock()
            client, _ = logged_in_client(
                [(401, {"code": 100001}),
                 (200, ok_envelope(access=ACCESS_2, refresh=REFRESH_2))],
                clock=clock)
            clock.now += 86400 + 1
            client.ensure_access_token()
        blob = " ".join(r.getMessage() for r in caplog.records)
        for secret in (SECRET_PASSWORD, ACCESS_1, ACCESS_2, REFRESH_1,
                       REFRESH_2, "Bearer"):
            assert secret not in blob


# --- integration boundaries -------------------------------------------------------

class TestBoundaries:
    SRC = (ROOT / "apps" / "web" / "transfer" / "gateway_auth.py").read_text(
        encoding="utf-8")

    def test_product_endpoint_only_in_sanctioned_module(self):
        # Build 4 shipped no product code; Build 5 added it in exactly one
        # module (product_lookup.py). The auth layer itself stays free of
        # product endpoints.
        transfer_dir = ROOT / "apps" / "web" / "transfer"
        for path in transfer_dir.glob("*.py"):
            if path.name == "product_lookup.py":
                continue
            src = path.read_text(encoding="utf-8")
            assert "pluLabel" not in src, path.name
            assert "corpTool" not in src, path.name

    def test_no_token_persistence_anywhere(self):
        # the auth module never writes files and never touches Streamlit
        for forbidden in ("open(", "write_text", "session_state",
                          "import streamlit", "json.dump("):
            assert forbidden not in self.SRC, forbidden

    def test_tokens_never_written_to_job_or_review_artifacts(self, tmp_path,
                                                            monkeypatch):
        monkeypatch.setenv("WEB_JOBS_DIR", str(tmp_path / "jobs"))
        monkeypatch.setenv("TRANSFER_JOBS_DIR", str(tmp_path / "tj"))
        from tests.test_transfer_review import make_job
        from apps.web.transfer import review as rv
        client, _ = logged_in_client([])
        job_id = make_job(tmp_path)
        rv.save_review(job_id, rv.get_or_create_review(job_id))
        blob = ""
        for path in (tmp_path / "tj").rglob("*.json"):
            blob += path.read_text(encoding="utf-8")
        assert ACCESS_1 not in blob and REFRESH_1 not in blob
        assert "access_token" not in blob.lower()

    def test_invoice_workflow_untouched(self):
        for name in ("job_manager.py", "worker.py", "app.py"):
            src = (ROOT / "apps" / "web" / name).read_text(encoding="utf-8")
            assert "gateway_auth" not in src, name


# --- UI wiring (static) -----------------------------------------------------------

class TestUiWiring:
    RPAGE = (ROOT / "apps" / "web" / "transfer" / "review_page.py").read_text(
        encoding="utf-8")

    def test_readiness_placeholder_present_and_safe(self):
        assert "Product API authentication" in self.RPAGE
        assert "Build 5" in self.RPAGE
        assert "readiness" in self.RPAGE
        # only the config-check function is used - no client, no network,
        # no live login trigger on render
        for forbidden in ("build_client", "ensure_access_token", "login(",
                          "get_authorization_header"):
            assert forbidden not in self.RPAGE, forbidden

    def test_no_product_lookup_button(self):
        assert "Product lookup" in self.RPAGE       # placeholder text only
        assert "st.button(\"Product" not in self.RPAGE

    def test_render_does_not_construct_auth_client(self):
        assert "ApiGatewayAuthClient" not in self.RPAGE
