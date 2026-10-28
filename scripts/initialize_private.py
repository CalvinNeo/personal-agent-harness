#!/usr/bin/env python3
"""Create a new, disabled, deny-all AstrBot configuration. Never overwrite one."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import secrets
from urllib.parse import urlsplit


def build_config(password: str, api_base: str, model: str, search: bool) -> dict:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600000).hex()
    return {
        "config_version": 5,
        "dashboard": {
            "enable": True, "username": "astrbot-admin",
            "password": hashlib.md5(password.encode()).hexdigest(),
            "pbkdf2_password": f"pbkdf2_sha256$600000${salt}${digest}",
            "password_storage_upgraded": True, "password_change_required": True,
            "jwt_secret": secrets.token_hex(48), "host": "127.0.0.1", "port": 6185,
            "trust_proxy_headers": False, "disable_access_log": True,
            "auth_rate_limit": {"enable": True, "average_interval": 1.0, "max_burst": 3},
        },
        "platform": [{"id": "private_qq_bot", "type": "qq_official", "enable": False,
                      "appid": "", "secret": "", "enable_group_c2c": True,
                      "enable_guild_direct_message": False, "use_markdown": False}],
        "platform_settings": {
            "unique_session": False, "enable_id_white_list": True,
            "id_whitelist": ["__deny_all_until_configured__"], "id_whitelist_log": True,
            "wl_ignore_admin_on_group": False, "wl_ignore_admin_on_friend": False,
            "no_permission_reply": False, "ignore_bot_self_message": True,
            "ignore_at_all": True, "empty_mention_waiting_need_reply": False,
            "rate_limit": {"time": 60, "count": 10, "strategy": "discard"},
        },
        "provider_sources": [{"id": "custom_model", "provider": "openai",
                              "type": "openai_chat_completion", "provider_type": "chat_completion",
                              "enable": False, "key": ["$MODEL_API_KEY"], "api_base": api_base,
                              "timeout": 120, "proxy": "", "custom_headers": {}}],
        "provider": [{"id": "custom_chat", "provider_source_id": "custom_model",
                      "model": model, "enable": False, "modalities": ["text", "image", "tool_use"]}],
        "provider_settings": {
            "enable": False, "computer_use_runtime": "none", "computer_use_require_admin": True,
            "web_search": search, "web_search_link": search, "websearch_provider": "anysearch",
            "proactive_capability": {"add_cron_tools": False},
        },
        "admins_id": [], "disable_builtin_commands": True, "disable_metrics": True,
        "log_level": "INFO", "http_proxy": "",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=Path("/var/lib/astrbot"))
    parser.add_argument("--etc-dir", type=Path, default=Path("/etc/astrbot"))
    parser.add_argument("--service-user", default="astrbot")
    parser.add_argument("--api-base", default="https://api.example.com/v1")
    parser.add_argument("--model", default="REPLACE_WITH_MODEL_NAME")
    parser.add_argument("--enable-search", action="store_true")
    args = parser.parse_args()
    url = urlsplit(args.api_base)
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
        parser.error("--api-base must be HTTPS and contain no credentials/query/fragment")
    os.umask(0o077)
    config = args.state_dir / "data/cmd_config.json"
    password_file = args.etc_dir / "admin-initial-password"
    if config.exists() or password_file.exists():
        raise SystemExit("Existing configuration or initial password found; refusing to replace it")
    account = pwd.getpwnam(args.service_user)
    password = "Aa9" + secrets.token_urlsafe(30)
    config.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    args.etc_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    with password_file.open("x", encoding="utf-8") as handle:
        handle.write(password + "\n")
    with config.open("x", encoding="utf-8") as handle:
        json.dump(build_config(password, args.api_base, args.model, args.enable_search), handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    for path in (args.state_dir, config.parent, config):
        os.chown(path, account.pw_uid, account.pw_gid)
    config.chmod(0o600)
    password_file.chmod(0o600)
    print("Created disabled, deny-all config and root-only initial password; credentials not printed")


if __name__ == "__main__":
    main()
