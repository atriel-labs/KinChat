"""Read secrets from the macOS Keychain.

All secrets live under one generic-password service, one account per secret:

    security add-generic-password -s kinchat -a anthropic-api-key -w 'sk-ant-...'
    security add-generic-password -s kinchat -a dashboard-password -w '...'
    security add-generic-password -s kinchat -a graph-cert-key-b64 \
        -w "$(base64 -i kinchat-key.pem | tr -d '\\n')"

The PEM is stored base64-encoded because `security -w` prints multi-line
values as hex. For local development, KINCHAT_<ACCOUNT> environment
variables (dashes become underscores, upper-case) take precedence.

Installs made before the rename keep working: the legacy service name and
environment-variable prefix are tried after the current ones.
"""
from __future__ import annotations

import base64
import os
import subprocess

SERVICE = "kinchat"
ENV_PREFIX = "KINCHAT_"
LEGACY_SERVICES = ("ai.atriel.mia-bridge",)
LEGACY_ENV_PREFIXES = ("MIA_BRIDGE_",)


def _keychain(service: str, account: str) -> str | None:
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            check=True, capture_output=True, text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return out.stdout.strip()


def get(account: str) -> str:
    suffix = account.upper().replace("-", "_")
    for prefix in (ENV_PREFIX, *LEGACY_ENV_PREFIXES):
        if os.environ.get(prefix + suffix):
            return os.environ[prefix + suffix]
    for service in (SERVICE, *LEGACY_SERVICES):
        value = _keychain(service, account)
        if value is not None:
            return value
    raise RuntimeError(f"Keychain item {SERVICE}/{account} not found")


def anthropic_api_key() -> str:
    return get("anthropic-api-key")


def dashboard_password() -> str:
    return get("dashboard-password")


def graph_cert_private_key() -> str:
    return base64.b64decode(get("graph-cert-key-b64")).decode()
