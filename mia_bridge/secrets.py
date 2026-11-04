"""Read secrets from the macOS Keychain.

All secrets live under one generic-password service, one account per secret:

    security add-generic-password -s ai.atriel.mia-bridge -a anthropic-api-key -w 'sk-ant-...'
    security add-generic-password -s ai.atriel.mia-bridge -a dashboard-password -w '...'
    security add-generic-password -s ai.atriel.mia-bridge -a graph-cert-key-b64 \
        -w "$(base64 -i mia-bridge-key.pem | tr -d '\\n')"

The PEM is stored base64-encoded because `security -w` prints multi-line
values as hex. For local development, MIA_BRIDGE_<ACCOUNT> environment
variables (dashes become underscores, upper-case) take precedence.
"""
from __future__ import annotations

import base64
import os
import subprocess

SERVICE = "ai.atriel.mia-bridge"


def get(account: str) -> str:
    env = "MIA_BRIDGE_" + account.upper().replace("-", "_")
    if os.environ.get(env):
        return os.environ[env]
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", SERVICE, "-a", account, "-w"],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Keychain item {SERVICE}/{account} not found") from e
    return out.stdout.strip()


def anthropic_api_key() -> str:
    return get("anthropic-api-key")


def dashboard_password() -> str:
    return get("dashboard-password")


def graph_cert_private_key() -> str:
    return base64.b64decode(get("graph-cert-key-b64")).decode()
