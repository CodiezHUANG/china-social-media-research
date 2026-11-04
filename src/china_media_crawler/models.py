"""Plans and canonical records for collectors."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from china_media_crawler import __version__

_SENSITIVE_QUERY_KEYS = {
    "access_token", "auth", "authorization", "cookie", "session",
    "sessionid", "token", "web_session", "xsec_token",
}

@dataclass
class Step:
    label: str
    command: list[str]
    cwd: str
    env: dict[str, str] = field(default_factory=dict)
    pass_environment: list[str] = field(default_factory=list)
    stdout_file: str | None = None


@dataclass
class Plan:
    created_at: str
    platform: str
    surface: str
    output: str
    limits: dict[str, int]
    required_environment: list[str]
    steps: list[Step]
    timeout_seconds: int = 1800
    live_network_action: bool = True
    backend: str = "unspecified"
    schema_version: str = "0.2"
    tool_version: str = __version__
    authentication: dict[str, Any] = field(default_factory=dict)
    purpose: str | None = None
    notes: list[str] = field(default_factory=list)

@dataclass
class Record:
    platform: str
    surface: str
    object_id: str
    content: str
    published_at: str | None
    captured_at: str
    author_pseudonym: str | None = None
    engagement: dict[str, Any] = field(default_factory=dict)
    platform_data: dict[str, Any] = field(default_factory=dict)
    schema_version: str = "0.2"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def redact_argument(value: str) -> str:
    if "://" not in value:
        return value
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    if not parsed.query:
        return value
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    if not any(key.lower() in _SENSITIVE_QUERY_KEYS for key, _ in pairs):
        return value
    redacted = [
        (key, "REDACTED" if key.lower() in _SENSITIVE_QUERY_KEYS else item)
        for key, item in pairs
    ]
    return urlunsplit(parsed._replace(query=urlencode(redacted)))


def plan_dict(plan: Plan) -> dict[str, Any]:
    data = asdict(plan)
    for step in data.get("steps", []):
        step["command"] = [redact_argument(item) for item in step.get("command", [])]
    return data
