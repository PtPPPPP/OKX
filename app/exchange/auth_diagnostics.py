from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum


class AuthenticationCategory(StrEnum):
    MISSING_CREDENTIALS = "missing_credentials"
    EMPTY_CREDENTIALS = "empty_credentials"
    INVALID_API_KEY = "invalid_api_key"
    INVALID_SECRET = "invalid_secret"
    INVALID_PASSPHRASE = "invalid_passphrase"
    INVALID_SIGNATURE = "invalid_signature"
    EXPIRED_TIMESTAMP = "expired_timestamp"
    CLOCK_SKEW = "clock_skew"
    WRONG_ENVIRONMENT = "wrong_environment"
    MISSING_SIMULATED_HEADER = "missing_simulated_header"
    WRONG_REQUEST_PATH = "wrong_request_path"
    WRONG_HTTP_METHOD = "wrong_http_method"
    IP_WHITELIST_REJECTED = "ip_whitelist_rejected"
    PERMISSION_DENIED = "permission_denied"
    API_KEY_EXPIRED = "api_key_expired"
    NETWORK_OR_PROXY_INTERFERENCE = "network_or_proxy_interference"
    RESPONSE_NOT_AUTH_RELATED = "response_not_auth_related"
    UNKNOWN_AUTHENTICATION_ERROR = "unknown_authentication_error"


@dataclass(frozen=True, slots=True)
class CredentialFieldStatus:
    configured: bool
    source: str
    has_leading_or_trailing_whitespace: bool
    contains_linebreak: bool


@dataclass(frozen=True, slots=True)
class AuthenticationDiagnostic:
    category: AuthenticationCategory
    okx_code: str | None
    http_status: int | None
    endpoint: str | None
    simulated_trading: bool
    credential_presence: dict[str, CredentialFieldStatus]
    local_time: datetime
    server_time: datetime | None
    clock_skew_ms: int | None
    request_path: str | None
    likely_causes: tuple[str, ...]
    safe_recommendations: tuple[str, ...]

    def safe_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["category"] = self.category.value
        result["local_time"] = self.local_time.astimezone(UTC).isoformat()
        result["server_time"] = (
            self.server_time.astimezone(UTC).isoformat() if self.server_time else None
        )
        return result


def classify_authentication_failure(
    *,
    okx_code: str | None,
    http_status: int | None,
    message: str,
) -> AuthenticationCategory:
    text = message.lower()
    if okx_code == "50102" or "timestamp" in text:
        return AuthenticationCategory.EXPIRED_TIMESTAMP
    if "signature" in text or okx_code in {"50113", "50114"}:
        return AuthenticationCategory.INVALID_SIGNATURE
    if "passphrase" in text:
        return AuthenticationCategory.INVALID_PASSPHRASE
    if "ip" in text and ("white" in text or "bind" in text):
        return AuthenticationCategory.IP_WHITELIST_REJECTED
    if "permission" in text or "authorization" in text:
        return AuthenticationCategory.PERMISSION_DENIED
    if "expired" in text:
        return AuthenticationCategory.API_KEY_EXPIRED
    if "api key" in text or http_status in {401, 403}:
        return AuthenticationCategory.INVALID_API_KEY
    return AuthenticationCategory.UNKNOWN_AUTHENTICATION_ERROR
