"""写入前敏感信息脱敏（token / .env / 密钥等）。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Tuple

# 常见密钥/令牌形态
_PATTERNS: List[Tuple[str, re.Pattern[str]]] = [
    (
        "pem_private_key",
        re.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
            re.IGNORECASE,
        ),
    ),
    (
        "bearer_token",
        re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9\-._~+/]+=*)"),
    ),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    ),
    (
        "openai_sk",
        re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    ),
    (
        "github_pat",
        re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    ),
    (
        "github_fine_grained",
        re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    ),
    (
        "aws_access_key",
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    ),
    (
        "slack_token",
        re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    ),
    (
        "generic_api_key_assign",
        re.compile(
            r"(?i)\b(api[_-]?key|access[_-]?token|secret[_-]?key|client[_-]?secret|"
            r"private[_-]?key|auth[_-]?token|refresh[_-]?token|password|passwd|pwd)"
            r"(\s*[=:：]\s*)([^\s\"']{8,})"
        ),
    ),
    (
        "env_secret_line",
        re.compile(
            r"(?im)^([A-Z][A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|API_KEY|PRIVATE_KEY|ACCESS_KEY)[A-Z0-9_]*)"
            r"(\s*=\s*)([^\n\r]+)$"
        ),
    ),
    (
        "url_password",
        re.compile(r"(?i)(://[^:/\s]+:)([^@/\s]{3,})(@)"),
    ),
]

_REDACT = "[REDACTED]"


@dataclass
class ScrubResult:
    text: str
    redacted: bool
    kinds: List[str]

    def as_dict(self) -> dict:
        return {
            "text": self.text,
            "redacted": self.redacted,
            "kinds": list(self.kinds),
        }


def scrub_text(text: str) -> ScrubResult:
    """对文本做敏感信息替换；无命中则原样返回。"""
    if not text:
        return ScrubResult(text=text or "", redacted=False, kinds=[])

    out = text
    kinds: List[str] = []

    for name, pat in _PATTERNS:
        if name == "bearer_token":
            new_out, n = pat.subn(rf"\1{_REDACT}", out)
        elif name == "generic_api_key_assign":
            new_out, n = pat.subn(rf"\1\2{_REDACT}", out)
        elif name == "env_secret_line":
            new_out, n = pat.subn(rf"\1\2{_REDACT}", out)
        elif name == "url_password":
            new_out, n = pat.subn(rf"\1{_REDACT}\3", out)
        else:
            new_out, n = pat.subn(_REDACT, out)
        if n:
            out = new_out
            if name not in kinds:
                kinds.append(name)

    return ScrubResult(text=out, redacted=bool(kinds), kinds=kinds)


def scrub_pair(question: str, answer: str) -> Tuple[ScrubResult, ScrubResult]:
    return scrub_text(question), scrub_text(answer)
