#!/usr/bin/env python3
"""Offline checks against AstrBot's real whitelist implementation.

No application startup, network traffic, model requests, or configuration writes.
Run locally with Python 3.10+ and the extracted source / pinned wheel alongside
this script. To verify an installed copy, pass --source-root pointing to the
directory containing its astrbot/ package. Optionally supply --config to verify
the live configuration's access-control flags and rejected synthetic events.

Only imports used for logging and event/context typing are stubbed. The Stage
base, MessageType enum, and WhitelistCheckStage are loaded from upstream source.
The session-isolation method is extracted verbatim with Python's AST, so the
unique_session checks also exercise the upstream implementation.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import hashlib
import json
import logging
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from zipfile import ZipFile


SCRIPT_DIR = Path(__file__).resolve().parent


class Sources:
    def __init__(self, source_root: Path, wheel: Path | None):
        self.root = source_root
        self.wheel = ZipFile(wheel) if wheel is not None else None

    def read(self, name: str) -> str:
        path = self.root / name
        disk = path.read_bytes() if path.is_file() else None
        packaged = self.wheel.read(name) if self.wheel is not None else None
        if disk is not None and packaged is not None and disk != packaged:
            raise AssertionError(f"Extracted source differs from pinned wheel: {name}")
        data = disk if disk is not None else packaged
        if data is None:
            raise FileNotFoundError(path)
        return data.decode("utf-8")


def module(name: str, **attributes) -> ModuleType:
    result = ModuleType(name)
    result.__path__ = []
    result.__dict__.update(attributes)
    sys.modules[name] = result
    return result


def load_implementation(sources: Sources):
    logger = logging.getLogger("whitelist-verification")
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    for package in (
        "astrbot",
        "astrbot.core",
        "astrbot.core.platform",
        "astrbot.core.pipeline",
        "astrbot.core.pipeline.whitelist_check",
    ):
        module(package)
    sys.modules["astrbot.core"].logger = logger
    module("astrbot.core.platform.astr_message_event", AstrMessageEvent=object)
    module("astrbot.core.pipeline.context", PipelineContext=object)

    loaded = {}
    for name in (
        "astrbot.core.platform.message_type",
        "astrbot.core.pipeline.stage",
        "astrbot.core.pipeline.whitelist_check.stage",
    ):
        filename = name.replace(".", "/") + ".py"
        text = sources.read(filename)
        result = module(name)
        result.__package__ = name.rpartition(".")[0]
        exec(compile(text, filename, "exec"), result.__dict__)
        loaded[name] = result

    message_type = loaded["astrbot.core.platform.message_type"].MessageType
    stage_type = loaded[
        "astrbot.core.pipeline.whitelist_check.stage"
    ].WhitelistCheckStage
    stage_source = sources.read("astrbot/core/pipeline/whitelist_check/stage.py")
    print("Whitelist source SHA256:", hashlib.sha256(stage_source.encode()).hexdigest())

    # Keep the exact upstream mapping and method, without executing EventBus
    # startup or importing the rest of AstrBot and its external dependencies.
    tree = ast.parse(sources.read("astrbot/core/event_bus.py"))
    assignment = next(
        node for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "UNIQUE_SESSION_ID_BUILDERS"
    )
    event_bus = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "EventBus"
    )
    isolate = next(
        node for node in event_bus.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_apply_session_isolation"
    )
    subset = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__",
                names=[ast.alias(name="annotations")], level=0,
            ),
            assignment,
            isolate,
        ],
        type_ignores=[],
    )
    environment = {"MessageType": message_type}
    exec(compile(ast.fix_missing_locations(subset), "event_bus_subset", "exec"), environment)
    return stage_type, message_type, environment["_apply_session_isolation"]


class Event:
    def __init__(
        self, message_type, *, group: str = "group_allowed", sender: str = "user_a",
        platform_id: str = "private_qq_bot", role: str = "member", private=False,
    ):
        self.kind = message_type.FRIEND_MESSAGE if private else message_type.GROUP_MESSAGE
        self.message_obj = SimpleNamespace(type=self.kind)
        self.group = "" if private else group
        self.sender = sender
        self.platform_id = platform_id
        self.session_id = sender if private else group
        self.role = role
        self.stopped = False
        self.extras = {}

    @property
    def unified_msg_origin(self):
        return f"{self.platform_id}:{self.kind.value}:{self.session_id}"

    def get_platform_name(self):
        return "qq_official"

    def get_message_type(self):
        return self.kind

    def get_group_id(self):
        return self.group

    def get_sender_id(self):
        return self.sender

    def set_extra(self, key, value):
        self.extras[key] = value

    def stop_event(self):
        self.stopped = True


def config(whitelist, *, unique_session=False):
    return {"platform_settings": {
        "unique_session": unique_session,
        "enable_id_white_list": True,
        "id_whitelist": whitelist,
        "id_whitelist_log": True,
        "wl_ignore_admin_on_group": False,
        "wl_ignore_admin_on_friend": False,
    }}


async def verify(args):
    # A custom installed source tree is authoritative. Only the local default
    # source tree falls back to the pinned wheel for files not extracted yet.
    fallback = args.wheel
    if fallback is None and args.source_root is None:
        fallback = SCRIPT_DIR / "packages/astrbot-4.29.3-py3-none-any.whl"
    sources = Sources(args.source_root or SCRIPT_DIR / "wheel-source", fallback)
    stage_type, message_type, isolate = load_implementation(sources)
    checks = 0

    async def check(label, policy, event, *, allowed, apply_isolation=False):
        nonlocal checks
        if apply_isolation:
            isolate(None, event, policy)
        stage = stage_type()
        await stage.initialize(SimpleNamespace(astrbot_config=policy))
        await stage.process(event)
        actual = not event.stopped
        if actual != allowed:
            raise AssertionError(
                f"{label}: expected {'ALLOW' if allowed else 'DENY'}, "
                f"got {'ALLOW' if actual else 'DENY'}"
            )
        checks += 1
        print(f"PASS {label}: {'ALLOW' if actual else 'DENY'}")

    def event(**kwargs):
        return Event(message_type, **kwargs)

    deny_policy = config([args.placeholder])
    for role in ("member", "admin"):
        await check(f"placeholder/group/{role}", deny_policy, event(role=role), allowed=False)
        await check(
            f"placeholder/private/{role}", deny_policy,
            event(role=role, private=True), allowed=False,
        )

    group_umo = event().unified_msg_origin
    group_policy = config([group_umo])
    for role in ("member", "admin"):
        for sender in ("user_a", "user_b"):
            await check(
                f"configured-group/{role}/{sender}", group_policy,
                event(role=role, sender=sender), allowed=True,
            )
        await check(
            f"other-group/{role}", group_policy,
            event(role=role, group="group_other"), allowed=False,
        )
        await check(
            f"private/{role}", group_policy,
            event(role=role, private=True), allowed=False,
        )
        await check(
            f"other-bot-same-group/{role}", group_policy,
            event(role=role, platform_id="other_bot"), allowed=False,
        )

    await check(
        "unique_session=false/full-group-UMO", group_policy,
        event(sender="user_b"), allowed=True, apply_isolation=True,
    )
    await check(
        "unique_session=true/full-group-UMO-is-insufficient",
        config([group_umo], unique_session=True), event(sender="user_b"),
        allowed=False, apply_isolation=True,
    )
    await check(
        "unique_session=true/raw-group-ID", config(["group_allowed"], unique_session=True),
        event(sender="user_b"), allowed=True, apply_isolation=True,
    )
    await check(
        "unique_session=true/raw-group-ID-denies-other-group",
        config(["group_allowed"], unique_session=True), event(group="group_other"),
        allowed=False, apply_isolation=True,
    )

    if args.config is not None:
        live = json.loads(args.config.read_text(encoding="utf-8-sig"))
        settings = live["platform_settings"]
        if settings.get("enable_id_white_list") is not True:
            raise AssertionError("Configured whitelist is not enabled")
        allowlist = [str(x).strip() for x in settings.get("id_whitelist", []) if str(x).strip()]
        if not allowlist:
            raise AssertionError("An empty allowlist allows every session in AstrBot")
        for key in ("wl_ignore_admin_on_group", "wl_ignore_admin_on_friend"):
            if settings.get(key) is not False:
                raise AssertionError(f"Admin bypass must be disabled: {key}")
        if args.require_placeholder and allowlist != [args.placeholder]:
            raise AssertionError("Current whitelist is not exactly the expected deny placeholder")
        if args.require_single_group:
            if len(allowlist) != 1:
                raise AssertionError("Expected exactly one full group UMO in the allowlist")
            parts = allowlist[0].split(":", 2)
            if len(parts) != 3 or parts[1] != message_type.GROUP_MESSAGE.value or not all(parts):
                raise AssertionError("The sole allowlist entry must be a full group UMO")
            platform_id, _, target_group = parts
            platforms = [p for p in live.get("platform", []) if p.get("id") == platform_id]
            if len(platforms) != 1 or platforms[0].get("type") != "qq_official":
                raise AssertionError("The group UMO must identify the configured official QQ adapter")
            if settings.get("unique_session", False):
                raise AssertionError("Full group UMO policy requires unique_session=false")
            for role in ("member", "admin"):
                for index, sender in enumerate(("__verification_member_a__", "__verification_member_b__"), 1):
                    await check(
                        f"supplied-config/target-group/{role}/{index}", live,
                        event(role=role, group=target_group, sender=sender, platform_id=platform_id),
                        allowed=True, apply_isolation=True,
                    )
                await check(
                    f"supplied-config/other-bot-target-group/{role}", live,
                    event(role=role, group=target_group, platform_id=platform_id + "__other__"),
                    allowed=False, apply_isolation=True,
                )
                await check(
                    f"supplied-config/target-id-as-private/{role}", live,
                    event(role=role, private=True, sender=target_group, platform_id=platform_id),
                    allowed=False, apply_isolation=True,
                )
                await check(
                    f"supplied-config/same-bot-other-group/{role}", live,
                    event(role=role, group=target_group + "__other__", platform_id=platform_id),
                    allowed=False, apply_isolation=True,
                )
        for role in ("member", "admin"):
            await check(
                f"supplied-config/unlisted-group/{role}", live,
                event(role=role, group="__verification_unlisted_group__"), allowed=False,
                apply_isolation=True,
            )
            await check(
                f"supplied-config/unlisted-private/{role}", live,
                event(role=role, private=True, sender="__verification_unlisted_user__"),
                allowed=False, apply_isolation=True,
            )
        print("PASS supplied-config: nonempty allowlist enabled; administrator bypasses disabled")

    print(f"SUCCESS: {checks} behavioral checks passed; no network or configuration changes.")
    print("Scope: QQ whitelist stage and session isolation only; WebChat is exempt upstream.")
    print("WebUI/HTTP access must therefore remain separately protected by loopback binding and SSH.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, help="Directory containing installed astrbot/ package")
    parser.add_argument("--wheel", type=Path, help="Pinned wheel to fill missing files and verify source equality")
    parser.add_argument("--config", type=Path, help="Optional live cmd_config.json (read only; values are not printed)")
    parser.add_argument("--placeholder", default="__deny_all_until_configured__")
    parser.add_argument("--require-placeholder", action="store_true", help="Require supplied config's allowlist to contain only --placeholder")
    parser.add_argument("--require-single-group", action="store_true", help="Require exactly one official QQ group UMO and verify its actual group is allowed")
    args = parser.parse_args()
    if (args.require_placeholder or args.require_single_group) and args.config is None:
        parser.error("--require-placeholder and --require-single-group require --config")
    if args.require_placeholder and args.require_single_group:
        parser.error("--require-placeholder and --require-single-group are mutually exclusive")
    try:
        asyncio.run(verify(args))
    except Exception as error:
        print(f"FAILED: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
