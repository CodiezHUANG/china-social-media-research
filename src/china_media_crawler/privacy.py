"""Environment isolation and pseudonymization."""
from __future__ import annotations

import hashlib
import os

from china_media_crawler.models import Step

SAFE_INHERITED_ENVIRONMENT = {
    "APPDATA",
    "COMSPEC",
    "HOME",
    "HOMEDRIVE",
    "HOMEPATH",
    "LANG",
    "LC_ALL",
    "LOCALAPPDATA",
    "PATH",
    "PATHEXT",
    "PLAYWRIGHT_BROWSERS_PATH",
    "PROGRAMDATA",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_FILE",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "TZ",
    "USERPROFILE",
    "WINDIR",
    "XDG_CACHE_HOME",
}

def child_environment(step: Step) -> dict[str, str]:
    inherited_names = SAFE_INHERITED_ENVIRONMENT | set(step.pass_environment)
    environment = {
        name: value
        for name in inherited_names
        if (value := os.getenv(name)) is not None
    }
    environment.update(step.env)
    return environment

def pseudonym(author: str, salt: str) -> str | None:
    if not author or not salt:
        return None
    digest = hashlib.sha256(f"{salt}:{author}".encode("utf-8")).hexdigest()
    return f"u_{digest[:16]}"

