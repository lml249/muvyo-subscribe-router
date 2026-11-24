from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List, Sequence

SENSITIVE_QUERY_RE = re.compile(
    r"(?i)([?&](?:token|api[_-]?key|apikey|password|passwd|secret|cookie|"
    r"authorization|access[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"pick[_-]?code|pickcode|pc|receive[_-]?code|share[_-]?code|pwd|sign)=)[^&#\s\"'<>]+"
)
AUTH_HEADER_RE = re.compile(
    r"(?im)^(\s*(?:authorization|cookie|x-api-key|x-emby-token|x-mediabrowser-token)\s*[:=]\s*)[^\r\n]+"
)
TOKEN_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(token|api[_-]?key|apikey|mv[_-]?api[_-]?key|password|passwd|secret|"
    r"authorization|cookie|access[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"pick[_-]?code|pickcode|receive[_-]?code|share[_-]?code)\s*[:=]\s*[^,\s\"'<>]+"
)
QUOTED_SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"""(?i)((["'])(?:token|api[_-]?key|apikey|mv[_-]?api[_-]?key|password|passwd|secret|"""
    r"""authorization|cookie|access[_-]?key|access[_-]?token|refresh[_-]?token|"""
    r"""pick[_-]?code|pickcode|pc|receive[_-]?code|share[_-]?code|pwd)\2\s*:\s*)(["'])(?:\\.|(?!\3).)*\3"""
)
REDIRECT_URL_RE = re.compile(r"(?i)https?://[^\s\"'<>]*/redirect\?[^\s\"'<>]+")

SENSITIVE_FIELD_NAMES = {
    "token",
    "apikey",
    "mvapikey",
    "password",
    "passwd",
    "pwd",
    "secret",
    "authorization",
    "cookie",
    "accesskey",
    "accesstoken",
    "refreshtoken",
    "pickcode",
    "pc",
    "receivecode",
    "sharecode",
    "xapikey",
    "xembytoken",
    "xmediabrowsertoken",
    "probetarget",
    "redirecturl",
    "directurl",
    "playurl",
    "streamurl",
    "shareurl",
    "sharelink",
}


def short_hash(value: str) -> str:
    raw = str(value or "")
    if not raw:
        return ""
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:8]


def is_sensitive_field(key: object) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(key or "").casefold())
    return normalized in SENSITIVE_FIELD_NAMES


def redact_sensitive_text(
    value: object,
    max_length: int = 1000,
    extra_secrets: Sequence[str] | None = None,
) -> str:
    text = str(value or "")
    if extra_secrets:
        for secret in extra_secrets:
            s = str(secret or "")
            if len(s) >= 3 and s in text:
                text = text.replace(s, "[REDACTED]")
    redacted = REDIRECT_URL_RE.sub("<redacted-redirect-url>", text)
    redacted = SENSITIVE_QUERY_RE.sub(r"\1[REDACTED]", redacted)
    redacted = AUTH_HEADER_RE.sub(r"\1[REDACTED]", redacted)
    redacted = TOKEN_ASSIGNMENT_RE.sub(lambda m: f"{m.group(1)}=[REDACTED]", redacted)
    redacted = QUOTED_SENSITIVE_ASSIGNMENT_RE.sub(
        lambda m: f"{m.group(1)}{m.group(3)}[REDACTED]{m.group(3)}",
        redacted,
    )
    if max_length > 0 and len(redacted) > max_length:
        return redacted[:max_length] + "...[TRUNCATED]"
    return redacted


def redact_sensitive_data(value: Any, extra_secrets: Sequence[str] | None = None) -> Any:
    if isinstance(value, dict):
        result: Dict[Any, Any] = {}
        for k, v in value.items():
            if is_sensitive_field(k):
                result[k] = "[REDACTED]"
            else:
                result[k] = redact_sensitive_data(v, extra_secrets=extra_secrets)
        return result
    if isinstance(value, list):
        return [redact_sensitive_data(item, extra_secrets=extra_secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive_data(item, extra_secrets=extra_secrets) for item in value)
    if isinstance(value, str):
        return redact_sensitive_text(value, extra_secrets=extra_secrets)
    return value


def redact_exception(
    exc: BaseException,
    max_length: int = 400,
    extra_secrets: Sequence[str] | None = None,
) -> str:
    return redact_sensitive_text(
        f"{exc.__class__.__name__}: {exc}",
        max_length=max_length,
        extra_secrets=extra_secrets,
    )
