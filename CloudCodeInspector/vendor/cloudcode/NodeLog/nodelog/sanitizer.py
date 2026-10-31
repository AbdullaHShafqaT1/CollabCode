import re
from typing import Any, Dict, List, Union

DEFAULT_MASK = "********"

# Regex patterns for sensitive information
BEARER_PATTERN = re.compile(r'(?i)\b(Bearer\s+)([A-Za-z0-9\-._~+/]+=*)')
KEY_VALUE_PATTERN = re.compile(
    r'(?i)\b(api[_-]?key|token|auth[_-]?token|secret|password|access[_-]?key|client[_-]?secret|private[_-]?key)(\s*[:=]\s*)([^\s,;\'"}\]]+)'
)
URI_CREDENTIAL_PATTERN = re.compile(r'([a-zA-Z0-9+.-]+://[^:\s/@]+):([^@\s/]+)@')
OPENAI_KEY_PATTERN = re.compile(r'\bsk-[a-zA-Z0-9_\-]{16,}\b')
GITHUB_TOKEN_PATTERN = re.compile(r'\b(ghp|gho|ghu|ghs|ghr)_[a-zA-Z0-9]{20,}\b')
AWS_KEY_PATTERN = re.compile(r'\b(AKIA|ABIA|ACCA|ASIA)[0-9A-Z]{16}\b')

SENSITIVE_KEY_NAMES = {
    "password", "passwd", "secret", "token", "api_key", "apikey", "api-key",
    "access_token", "auth_token", "authorization", "private_key", "client_secret"
}

def sanitize_text(text: str, mask: str = DEFAULT_MASK) -> str:
    """Detects and masks sensitive patterns in a text string."""
    if not isinstance(text, str) or not text:
        return text

    # 1. Bearer tokens (e.g., "Bearer ********")
    text = BEARER_PATTERN.sub(rf'\g<1>{mask}', text)

    # 2. Key-value credentials (e.g., "api_key=********")
    text = KEY_VALUE_PATTERN.sub(rf'\g<1>\g<2>{mask}', text)

    # 3. Connection URIs (e.g., "postgres://admin:********@localhost:5432/db")
    text = URI_CREDENTIAL_PATTERN.sub(rf'\g<1>:{mask}@', text)

    # 4. Standalone token patterns (sk-..., ghp_..., AWS keys)
    text = OPENAI_KEY_PATTERN.sub(mask, text)
    text = GITHUB_TOKEN_PATTERN.sub(mask, text)
    text = AWS_KEY_PATTERN.sub(mask, text)

    return text

def sanitize_data(data: Any, mask: str = DEFAULT_MASK) -> Any:
    """Recursively sanitizes sensitive information in dictionaries, lists, and strings."""
    if isinstance(data, str):
        return sanitize_text(data, mask=mask)
    elif isinstance(data, dict):
        sanitized: Dict[str, Any] = {}
        for k, v in data.items():
            if str(k).lower() in SENSITIVE_KEY_NAMES and isinstance(v, (str, int, float)):
                sanitized[k] = mask
            else:
                sanitized[k] = sanitize_data(v, mask=mask)
        return sanitized
    elif isinstance(data, list):
        return [sanitize_data(item, mask=mask) for item in data]
    elif isinstance(data, tuple):
        return tuple(sanitize_data(item, mask=mask) for item in data)
    return data
