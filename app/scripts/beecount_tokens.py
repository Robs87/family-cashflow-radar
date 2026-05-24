"""BeeCount token storage helpers.

Tokens are private financial credentials. Keep them out of repo files and use
macOS Keychain for persistence when the local Web app is running on macOS.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


KEYCHAIN_SERVICE = "family-cashflow-radar.beecount"
DEFAULT_READ_TOKEN_ENV = "BEECOUNT_READ_API_TOKEN"
DEFAULT_ACCESS_TOKEN_ENV = "BEECOUNT_ACCESS_TOKEN"
DEFAULT_REFRESH_TOKEN_ENV = "BEECOUNT_REFRESH_TOKEN"

TokenSource = Literal["env", "keychain", ""]


@dataclass(frozen=True)
class StoredToken:
    value: str
    source: TokenSource


def _security_binary() -> str:
    binary = shutil.which("security")
    if not binary:
        raise RuntimeError("当前系统找不到 macOS security 命令，无法写入 Keychain")
    return binary


def read_keychain_token(account: str) -> str:
    try:
        result = subprocess.run(
            [
                _security_binary(),
                "find-generic-password",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                account,
                "-w",
            ],
            check=False,
            text=True,
            capture_output=True,
        )
    except RuntimeError:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.rstrip("\n")


def write_keychain_token(account: str, token: str) -> None:
    token = token.strip()
    if not token:
        return
    result = subprocess.run(
        [
            _security_binary(),
            "add-generic-password",
            "-U",
            "-s",
            KEYCHAIN_SERVICE,
            "-a",
            account,
            "-w",
            token,
        ],
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "Keychain 写入失败"
        raise RuntimeError(message)


def get_token(env_name: str) -> StoredToken:
    value = os.environ.get(env_name, "").strip()
    if value:
        return StoredToken(value=value, source="env")
    value = read_keychain_token(env_name).strip()
    if value:
        return StoredToken(value=value, source="keychain")
    return StoredToken(value="", source="")


def token_is_configured(env_name: str) -> bool:
    return bool(get_token(env_name).value)


def write_beecount_config(
    config_path: Path,
    *,
    base_url: str,
    ledger_id: str,
    limit: int,
    read_token: str = "",
    access_token: str = "",
    refresh_token: str = "",
    read_token_env: str = DEFAULT_READ_TOKEN_ENV,
    access_token_env: str = DEFAULT_ACCESS_TOKEN_ENV,
    refresh_token_env: str = DEFAULT_REFRESH_TOKEN_ENV,
) -> None:
    if read_token.strip():
        write_keychain_token(read_token_env, read_token)
    if access_token.strip():
        write_keychain_token(access_token_env, access_token)
    if refresh_token.strip():
        write_keychain_token(refresh_token_env, refresh_token)

    config_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "base_url": base_url.strip(),
        "ledger_id": ledger_id.strip(),
        "read_token_env": read_token_env.strip() or DEFAULT_READ_TOKEN_ENV,
        "access_token_env": access_token_env.strip() or DEFAULT_ACCESS_TOKEN_ENV,
        "refresh_token_env": refresh_token_env.strip() or DEFAULT_REFRESH_TOKEN_ENV,
        "limit": int(limit),
    }
    config_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
