"""Reusable API Gateway authentication client (Build 4).

Implements the contract confirmed from the ImagineX API Gateway
Specification (v0.851-CorpTools) and the working label-print integration:

  POST {base}/auth/login    {"client", "userId", "password", "locale"}
  POST {base}/auth/refresh  {"rt": "<refresh token>"}

Both return the envelope {status, code, reason, note, data}; success is
code == 100000 REGARDLESS of HTTP status, and data carries
{accessToken, expire_in (seconds), refreshToken}. A refresh response may
rotate the refresh token; per the spec, a stored refresh token is NEVER
overwritten by an empty/null replacement.

Security invariants (Build 4):

  * tokens live only in a process-local, thread-safe in-memory cache -
    never on disk, never in job/review artifacts, never in Streamlit
    session state, never in the browser;
  * credentials come from backend environment variables only;
  * every error message and log line passes through redaction - passwords
    and token values can never appear, even partially;
  * no product endpoint is called anywhere in this module - product
    lookup arrives in Build 5.
"""

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

logger = logging.getLogger("invoice_extractor.transfer.gateway_auth")

# --- defaults (contract-confirmed) ------------------------------------------------

DEFAULT_LOGIN_PATH = "/auth/login"
DEFAULT_REFRESH_PATH = "/auth/refresh"
DEFAULT_CLIENT_ID = "Web-Label"
DEFAULT_SUCCESS_CODE = 100000
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_MAX_AUTH_RETRIES = 2
DEFAULT_EXPIRY_SKEW_SECONDS = 60
DEFAULT_LOCALE = "en-US"          # spec: en-US | zh-CHS | zh-CHT

# --- typed error codes ------------------------------------------------------------

AUTH_CONFIGURATION_ERROR = "AUTH_CONFIGURATION_ERROR"
AUTH_TRANSPORT_ERROR = "AUTH_TRANSPORT_ERROR"
AUTH_TIMEOUT = "AUTH_TIMEOUT"
AUTH_RESPONSE_INVALID = "AUTH_RESPONSE_INVALID"
AUTH_GATEWAY_REJECTED = "AUTH_GATEWAY_REJECTED"
AUTH_LOGIN_FAILED = "AUTH_LOGIN_FAILED"
AUTH_REFRESH_FAILED = "AUTH_REFRESH_FAILED"
AUTH_TOKEN_MISSING = "AUTH_TOKEN_MISSING"
AUTH_TOKEN_EXPIRED = "AUTH_TOKEN_EXPIRED"
AUTH_ACCESS_DENIED = "AUTH_ACCESS_DENIED"
AUTH_RETRY_EXHAUSTED = "AUTH_RETRY_EXHAUSTED"

AUTH_ERROR_CODES = (
    AUTH_CONFIGURATION_ERROR, AUTH_TRANSPORT_ERROR, AUTH_TIMEOUT,
    AUTH_RESPONSE_INVALID, AUTH_GATEWAY_REJECTED, AUTH_LOGIN_FAILED,
    AUTH_REFRESH_FAILED, AUTH_TOKEN_MISSING, AUTH_TOKEN_EXPIRED,
    AUTH_ACCESS_DENIED, AUTH_RETRY_EXHAUSTED,
)

# --- redaction --------------------------------------------------------------------

SENSITIVE_KEY_PARTS = ("password", "access_token", "accesstoken",
                       "refresh_token", "refreshtoken", "authorization",
                       "token", "secret")
SENSITIVE_KEY_EXACT = ("rt",)           # the refresh request body key
REDACTED = "***redacted***"


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower().replace("-", "_")
    return (lowered in SENSITIVE_KEY_EXACT
            or any(part in lowered for part in SENSITIVE_KEY_PARTS))


def redact(value: Any) -> Any:
    """Deep-copy redaction: any mapping value under a sensitive key (case
    insensitive; includes camelCase forms and the refresh body key 'rt')
    is replaced entirely. Lists/tuples are walked; scalars pass through."""
    if isinstance(value, dict):
        return {k: (REDACTED if _is_sensitive_key(str(k))
                    else redact(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


class AuthError(Exception):
    """Typed, redaction-safe authentication error. Never carries
    passwords, token values, Authorization headers, or request bodies."""

    def __init__(self, code: str, message: str, *, operation: str | None = None,
                 http_status: int | None = None,
                 gateway_code: int | None = None,
                 retryable: bool = False):
        self.code = code
        self.operation = operation
        self.http_status = http_status
        self.gateway_code = gateway_code
        self.retryable = retryable
        parts = [f"[{code}]", message]
        if operation:
            parts.append(f"(operation={operation}")
            tail = []
            if http_status is not None:
                tail.append(f"http={http_status}")
            if gateway_code is not None:
                tail.append(f"gateway_code={gateway_code}")
            parts[-1] += (", " + ", ".join(tail) if tail else "") + ")"
        super().__init__(" ".join(parts))


# --- configuration ----------------------------------------------------------------

@dataclass(frozen=True)
class ApiGatewayAuthConfig:
    base_url: str
    login_path: str = DEFAULT_LOGIN_PATH
    refresh_path: str = DEFAULT_REFRESH_PATH
    client_id: str = DEFAULT_CLIENT_ID
    success_code: int = DEFAULT_SUCCESS_CODE
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_auth_retries: int = DEFAULT_MAX_AUTH_RETRIES
    expiry_skew_seconds: int = DEFAULT_EXPIRY_SKEW_SECONDS
    locale: str = DEFAULT_LOCALE

    @property
    def login_url(self) -> str:
        return self.base_url + self.login_path

    @property
    def refresh_url(self) -> str:
        return self.base_url + self.refresh_path


@dataclass(frozen=True, repr=False)
class ApiGatewayCredentials:
    """Backend-only credentials. repr/str never include values."""
    user_id: str
    password: str

    def __repr__(self) -> str:          # defense in depth
        return "ApiGatewayCredentials(user_id=***, password=***)"


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _normalize_base_url(raw: str) -> str:
    return raw.rstrip("/")


def _normalize_path(raw: str, default: str) -> str:
    value = raw.strip() or default
    if not value.startswith("/"):
        value = "/" + value
    return value.rstrip("/") or default


def config_problems() -> list[str]:
    """Safe, human-readable configuration problems (empty = configured).
    Never includes any configured VALUE for credential variables."""
    problems = []
    base = _env("API_GATEWAY_BASE_URL")
    if not base:
        problems.append("API_GATEWAY_BASE_URL is not set.")
    elif not (base.startswith("http://") or base.startswith("https://")):
        problems.append("API_GATEWAY_BASE_URL must start with http(s)://.")
    if not (_env("API_GATEWAY_CLIENT_ID") or DEFAULT_CLIENT_ID):
        problems.append("API_GATEWAY_CLIENT_ID is blank.")
    for name, default in (("API_GATEWAY_SUCCESS_CODE", DEFAULT_SUCCESS_CODE),
                          ("API_GATEWAY_MAX_AUTH_RETRIES",
                           DEFAULT_MAX_AUTH_RETRIES),
                          ("API_GATEWAY_EXPIRY_SKEW_SECONDS",
                           DEFAULT_EXPIRY_SKEW_SECONDS)):
        raw = _env(name)
        if raw:
            try:
                int(raw)
            except ValueError:
                problems.append(f"{name} must be an integer.")
    raw = _env("API_GATEWAY_TIMEOUT_SECONDS")
    if raw:
        try:
            if float(raw) <= 0:
                problems.append("API_GATEWAY_TIMEOUT_SECONDS must be > 0.")
        except ValueError:
            problems.append("API_GATEWAY_TIMEOUT_SECONDS must be a number.")
    if not _env("API_GATEWAY_USER_ID"):
        problems.append("API_GATEWAY_USER_ID is not set.")
    if not _env("API_GATEWAY_PASSWORD"):
        problems.append("API_GATEWAY_PASSWORD is not set.")
    return problems


def load_auth_config() -> ApiGatewayAuthConfig:
    problems = [p for p in config_problems()
                if not p.startswith(("API_GATEWAY_USER_ID",
                                     "API_GATEWAY_PASSWORD"))]
    if problems:
        raise AuthError(AUTH_CONFIGURATION_ERROR, " ".join(problems))
    return ApiGatewayAuthConfig(
        base_url=_normalize_base_url(_env("API_GATEWAY_BASE_URL")),
        login_path=_normalize_path(_env("API_GATEWAY_LOGIN_PATH"),
                                   DEFAULT_LOGIN_PATH),
        refresh_path=_normalize_path(_env("API_GATEWAY_REFRESH_PATH"),
                                     DEFAULT_REFRESH_PATH),
        client_id=_env("API_GATEWAY_CLIENT_ID") or DEFAULT_CLIENT_ID,
        success_code=int(_env("API_GATEWAY_SUCCESS_CODE")
                         or DEFAULT_SUCCESS_CODE),
        timeout_seconds=float(_env("API_GATEWAY_TIMEOUT_SECONDS")
                              or DEFAULT_TIMEOUT_SECONDS),
        max_auth_retries=int(_env("API_GATEWAY_MAX_AUTH_RETRIES")
                             or DEFAULT_MAX_AUTH_RETRIES),
        expiry_skew_seconds=int(_env("API_GATEWAY_EXPIRY_SKEW_SECONDS")
                                or DEFAULT_EXPIRY_SKEW_SECONDS),
        locale=_env("API_GATEWAY_LOCALE") or DEFAULT_LOCALE,
    )


def load_credentials() -> ApiGatewayCredentials:
    user_id = _env("API_GATEWAY_USER_ID")
    password = _env("API_GATEWAY_PASSWORD")
    missing = [name for name, value in
               (("API_GATEWAY_USER_ID", user_id),
                ("API_GATEWAY_PASSWORD", password)) if not value]
    if missing:
        raise AuthError(AUTH_CONFIGURATION_ERROR,
                        "Missing credentials: " + ", ".join(missing)
                        + ". Set them in the backend environment only.")
    return ApiGatewayCredentials(user_id=user_id, password=password)


def readiness() -> dict:
    """Config-only readiness summary for the UI. NEVER performs a network
    request and never includes configured values."""
    problems = config_problems()
    if not problems:
        return {"status": "configured", "problems": []}
    only_unset = all("is not set" in p for p in problems)
    return {"status": "not_configured" if only_unset else "configuration_error",
            "problems": problems}


# --- token set + cache ------------------------------------------------------------

@dataclass(repr=False)
class ApiGatewayTokenSet:
    access_token: str
    refresh_token: str | None
    obtained_at: float
    expires_at: float | None = None     # epoch seconds; None = no expiry info
    token_type: str = "Bearer"

    def __repr__(self) -> str:
        return (f"ApiGatewayTokenSet(access_token=***, refresh_token=***, "
                f"obtained_at={self.obtained_at}, expires_at={self.expires_at})")

    def is_valid(self, *, now: float, skew_seconds: int) -> bool:
        if not self.access_token:
            return False
        if self.expires_at is None:
            return True                  # valid until invalidated
        return now < self.expires_at - skew_seconds


class TokenCache:
    """Process-local, thread-safe. Deliberately NOT persisted: no disk, no
    Streamlit session state, no job/review artifacts. Each app process or
    container maintains (and re-acquires) its own tokens."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self._tokens: ApiGatewayTokenSet | None = None

    def get(self) -> ApiGatewayTokenSet | None:
        with self.lock:
            return self._tokens

    def set(self, tokens: ApiGatewayTokenSet | None) -> None:
        with self.lock:
            self._tokens = tokens

    def invalidate_access_token(self) -> None:
        with self.lock:
            if self._tokens is not None:
                self._tokens.expires_at = self._tokens.obtained_at  # force invalid

    def clear(self) -> None:
        with self.lock:
            self._tokens = None


_default_cache = TokenCache()


def reset_default_cache() -> None:      # test hook
    _default_cache.clear()


# --- transport --------------------------------------------------------------------

class TransportTimeout(Exception):
    pass


class TransportFailure(Exception):
    pass


class AuthTransport(Protocol):
    def post_json(self, url: str, body: dict, *,
                  timeout: float) -> tuple[int, Any]:
        """(http_status, parsed_json_or_None). Raise TransportTimeout /
        TransportFailure for network-level problems. Implementations must
        never log the request body."""
        ...


class HttpxAuthTransport:
    """Production transport on httpx (the repository's established HTTP
    library). No retries here - the client owns the retry policy."""

    def post_json(self, url: str, body: dict, *,
                  timeout: float) -> tuple[int, Any]:
        import httpx
        try:
            response = httpx.post(url, json=body, timeout=timeout,
                                  headers={"Content-Type": "application/json"})
        except httpx.TimeoutException as exc:
            raise TransportTimeout(str(type(exc).__name__)) from exc
        except httpx.HTTPError as exc:
            raise TransportFailure(str(type(exc).__name__)) from exc
        try:
            parsed = response.json()
        except (json.JSONDecodeError, ValueError):
            parsed = None
        return response.status_code, parsed


# --- envelope parsing (contract-confirmed) ----------------------------------------

def _parse_envelope(body: Any) -> dict:
    """{status, code, reason, note, data} with reason falling back to
    message/msg, mirroring the verified label-print parser."""
    if not isinstance(body, dict):
        return {"status": None, "code": None, "reason": None, "note": None,
                "data": None}

    def _str(v):
        return v.strip() if isinstance(v, str) and v.strip() else None

    def _num(v):
        if isinstance(v, (int, float)):
            return int(v)
        if isinstance(v, str) and v.strip():
            try:
                return int(float(v))
            except ValueError:
                return None
        return None

    return {
        "status": _str(body.get("status")),
        "code": _num(body.get("code")),
        "reason": (_str(body.get("reason")) or _str(body.get("message"))
                   or _str(body.get("msg"))),
        "note": _str(body.get("note")),
        "data": body.get("data"),
    }


def _token_fields(data: Any) -> tuple[str | None, str | None, float | None]:
    """(access_token, refresh_token, expire_in_seconds) from envelope data."""
    if not isinstance(data, dict):
        return None, None, None
    access = data.get("accessToken")
    access = access.strip() if isinstance(access, str) else None
    refresh = data.get("refreshToken")
    refresh = refresh.strip() if isinstance(refresh, str) else None
    expire = data.get("expire_in")
    if isinstance(expire, str) and expire.strip():
        try:
            expire = float(expire)
        except ValueError:
            expire = None
    if not isinstance(expire, (int, float)) or expire <= 0:
        expire = None
    return (access or None), (refresh or None), expire


# --- auth client ------------------------------------------------------------------

class ApiGatewayAuthClient:
    """login / refresh / ensure_access_token with narrow retries and a
    single controlled re-login fallback. Thread-safe via the cache lock;
    concurrent ensure_access_token() callers perform ONE login."""

    def __init__(self, config: ApiGatewayAuthConfig,
                 credentials: ApiGatewayCredentials,
                 transport: AuthTransport | None = None,
                 cache: TokenCache | None = None,
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep):
        self.config = config
        self.credentials = credentials
        self.transport = transport or HttpxAuthTransport()
        self.cache = cache if cache is not None else _default_cache
        self.clock = clock
        self.sleep = sleep

    # --- request plumbing --------------------------------------------------------

    def _post_with_retries(self, operation: str, url: str,
                           body: dict) -> tuple[int, Any]:
        """Retry ONLY transport timeouts/failures and 5xx responses, up to
        max_auth_retries extra attempts. 4xx and gateway-level rejections
        are never retried here."""
        attempts = max(0, self.config.max_auth_retries) + 1
        last_error: AuthError | None = None
        for attempt in range(1, attempts + 1):
            try:
                status, parsed = self.transport.post_json(
                    url, body, timeout=self.config.timeout_seconds)
            except TransportTimeout:
                last_error = AuthError(
                    AUTH_TIMEOUT, "The API Gateway did not respond within "
                    f"{self.config.timeout_seconds:.0f}s.",
                    operation=operation, retryable=True)
            except TransportFailure:
                last_error = AuthError(
                    AUTH_TRANSPORT_ERROR,
                    "The API Gateway could not be reached.",
                    operation=operation, retryable=True)
            else:
                if status >= 500:
                    last_error = AuthError(
                        AUTH_TRANSPORT_ERROR,
                        "The API Gateway returned a server error.",
                        operation=operation, http_status=status,
                        retryable=True)
                else:
                    if attempt > 1:
                        logger.info("gateway auth %s succeeded on retry %d",
                                    operation, attempt - 1)
                    return status, parsed
            logger.warning("gateway auth %s attempt %d/%d failed: %s",
                           operation, attempt, attempts, last_error.code)
            if attempt < attempts:
                self.sleep(min(2.0 ** (attempt - 1), 5.0))
        raise AuthError(AUTH_RETRY_EXHAUSTED,
                        f"Authentication {operation} failed after "
                        f"{attempts} attempt(s): {last_error.code}.",
                        operation=operation,
                        http_status=last_error.http_status,
                        retryable=False)

    def _store_tokens(self, operation: str, envelope: dict,
                      *, keep_refresh: str | None = None) -> ApiGatewayTokenSet:
        access, refresh, expire_in = _token_fields(envelope["data"])
        if not access:
            raise AuthError(AUTH_RESPONSE_INVALID,
                            "The gateway response did not include a usable "
                            "access token.", operation=operation,
                            gateway_code=envelope["code"])
        now = self.clock()
        # spec rule: never overwrite a stored refresh token with empty/null
        effective_refresh = refresh or keep_refresh
        tokens = ApiGatewayTokenSet(
            access_token=access,
            refresh_token=effective_refresh,
            obtained_at=now,
            expires_at=(now + expire_in) if expire_in else None,
        )
        self.cache.set(tokens)
        logger.info("gateway auth %s ok (expires_at=%s, refresh_rotated=%s)",
                    operation,
                    tokens.expires_at and round(tokens.expires_at),
                    bool(refresh and refresh != keep_refresh))
        return tokens

    # --- operations --------------------------------------------------------------

    def login(self) -> ApiGatewayTokenSet:
        body = {
            "client": self.config.client_id,
            "userId": self.credentials.user_id,
            "password": self.credentials.password,
            "locale": self.config.locale,
        }
        status, parsed = self._post_with_retries("login",
                                                 self.config.login_url, body)
        envelope = _parse_envelope(parsed)
        if status == 401 or (200 <= status < 300
                             and envelope["code"] is not None
                             and envelope["code"] != self.config.success_code):
            raise AuthError(AUTH_LOGIN_FAILED,
                            "The API Gateway rejected the login credentials.",
                            operation="login", http_status=status,
                            gateway_code=envelope["code"])
        if not (200 <= status < 300):
            raise AuthError(AUTH_GATEWAY_REJECTED,
                            "The API Gateway rejected the login request.",
                            operation="login", http_status=status,
                            gateway_code=envelope["code"])
        if parsed is None or envelope["code"] is None:
            raise AuthError(AUTH_RESPONSE_INVALID,
                            "The gateway login response was not valid JSON "
                            "in the documented envelope shape.",
                            operation="login", http_status=status)
        return self._store_tokens("login", envelope)

    def refresh(self) -> ApiGatewayTokenSet:
        current = self.cache.get()
        if current is None or not current.refresh_token:
            raise AuthError(AUTH_TOKEN_MISSING,
                            "No refresh token is cached; login is required.",
                            operation="refresh")
        body = {"rt": current.refresh_token}
        status, parsed = self._post_with_retries("refresh",
                                                 self.config.refresh_url, body)
        envelope = _parse_envelope(parsed)
        if status == 401 or (200 <= status < 300
                             and envelope["code"] is not None
                             and envelope["code"] != self.config.success_code):
            raise AuthError(AUTH_REFRESH_FAILED,
                            "The refresh token was rejected (expired or "
                            "invalid).", operation="refresh",
                            http_status=status,
                            gateway_code=envelope["code"])
        if not (200 <= status < 300):
            raise AuthError(AUTH_GATEWAY_REJECTED,
                            "The API Gateway rejected the refresh request.",
                            operation="refresh", http_status=status,
                            gateway_code=envelope["code"])
        if parsed is None or envelope["code"] is None:
            raise AuthError(AUTH_RESPONSE_INVALID,
                            "The gateway refresh response was not valid "
                            "JSON in the documented envelope shape.",
                            operation="refresh", http_status=status)
        return self._store_tokens("refresh", envelope,
                                  keep_refresh=current.refresh_token)

    # --- validity + lifecycle ----------------------------------------------------

    def has_valid_access_token(self) -> bool:
        tokens = self.cache.get()
        return bool(tokens and tokens.is_valid(
            now=self.clock(), skew_seconds=self.config.expiry_skew_seconds))

    def ensure_access_token(self) -> str:
        """Cached valid token -> refresh when possible -> login. A rejected
        refresh clears the stale refresh state and falls back to exactly
        ONE fresh login. Concurrent callers are serialized by the cache
        lock and the second caller reuses the first result."""
        with self.cache.lock:
            tokens = self.cache.get()
            if tokens and tokens.is_valid(
                    now=self.clock(),
                    skew_seconds=self.config.expiry_skew_seconds):
                return tokens.access_token
            if tokens and tokens.refresh_token:
                try:
                    return self.refresh().access_token
                except AuthError as exc:
                    if exc.code in (AUTH_REFRESH_FAILED, AUTH_TOKEN_MISSING,
                                    AUTH_RETRY_EXHAUSTED):
                        logger.info("gateway refresh unavailable (%s); "
                                    "attempting one fresh login", exc.code)
                        self.cache.clear()      # stale refresh state
                    else:
                        raise
            return self.login().access_token

    def invalidate_access_token(self) -> None:
        """Mark the access token invalid (keeps the refresh token so the
        next ensure_access_token() can refresh)."""
        self.cache.invalidate_access_token()

    def clear_tokens(self) -> None:
        self.cache.clear()

    # --- Build 5 hooks ------------------------------------------------------------

    def get_authorization_header(self) -> dict[str, str]:
        """Server-side only. Never expose the returned header (or the
        token inside it) to UI code, logs, or artifacts."""
        return {"Authorization": f"Bearer {self.ensure_access_token()}"}

    def handle_unauthorized(self) -> str:
        """For the future product caller: after an HTTP 401 from an
        authorized endpoint, invalidate the access token and obtain a
        fresh one (refresh, then one re-login fallback). Build 5 retries
        the failed product batch ONCE with the returned token."""
        self.invalidate_access_token()
        return self.ensure_access_token()


def build_client(transport: AuthTransport | None = None,
                 cache: TokenCache | None = None) -> ApiGatewayAuthClient:
    """Construct a client from backend environment configuration."""
    return ApiGatewayAuthClient(load_auth_config(), load_credentials(),
                                transport=transport, cache=cache)
