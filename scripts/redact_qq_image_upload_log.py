#!/usr/bin/env python3
"""Remove QQ image payloads from one known AstrBot upload-failure warning.

Run after the repository's normal AstrBot customizations. This independent,
idempotent patch changes only the warning text. It recognizes its own applied
state and refuses unknown versions of the target method without printing the
source or a payload. --check never changes the target file.
"""
from __future__ import annotations

import argparse
import ast
from pathlib import Path


TARGET = Path("astrbot/core/platform/sources/qqofficial/qqofficial_message_event.py")
OLD = 'logger.warning(f"上传图片API返回None，共尝试5次后放弃: {payload}")'
NEW = 'logger.warning("[QQOfficial] Image upload failed after retries; request payload omitted.")'


class PatchError(ValueError):
    """A source compatibility failure; messages contain no inspected content."""


def patched_source(source: str) -> tuple[str, bool]:
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        raise PatchError("Refusing invalid target Python source") from error
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef)
               and node.name == "QQOfficialMessageEvent"]
    if len(classes) != 1:
        raise PatchError("Refusing unknown QQ event class")
    methods = [node for node in classes[0].body if isinstance(node, ast.AsyncFunctionDef)
               and node.name == "upload_group_and_c2c_image"]
    if len(methods) != 1:
        raise PatchError("Refusing unknown QQ image upload method")
    method_source = ast.get_source_segment(source, methods[0]) or ""
    if source.count(OLD) == 0 and source.count(NEW) == 1 and method_source.count(NEW) == 1:
        return source, False
    if source.count(OLD) != 1 or source.count(NEW) != 0 or method_source.count(OLD) != 1:
        raise PatchError("Refusing unknown or mixed QQ image warning state")
    result = source.replace(OLD, NEW, 1)
    # Never install malformed source even if the exact marker was present.
    ast.parse(result)
    return result, True


def apply_patch(site_packages: Path, *, check: bool = False) -> str:
    path = site_packages / TARGET
    if path.is_symlink() or not path.is_file():
        raise PatchError("Refusing missing, non-file or symlink QQ source")
    try:
        original = path.read_bytes()
        replacement, changed = patched_source(original.decode("utf-8"))
    except (OSError, UnicodeError) as error:
        raise PatchError("Unable to read QQ source") from error
    if not changed:
        return "already-patched"
    if check:
        return "applicable"
    if path.is_symlink() or path.read_bytes() != original:
        raise PatchError("QQ source changed during verification; no patch applied")
    # Updating the existing file preserves its owner and mode. The complete
    # replacement has already passed Python parsing and scoped-state checks.
    try:
        path.write_bytes(replacement.encode("utf-8"))
    except OSError as error:
        raise PatchError("Unable to write QQ source") from error
    if path.read_bytes() != replacement.encode("utf-8"):
        raise PatchError("QQ source post-patch verification failed")
    return "patched"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site-packages", required=True, type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        state = apply_patch(args.site_packages, check=args.check)
    except PatchError as error:
        parser.exit(1, str(error) + "\n")
    print({
        "already-patched": "QQ image upload warning is already redacted",
        "applicable": "QQ image upload warning patch is applicable; no changes made",
        "patched": "QQ image upload warning redacted and verified",
    }[state])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
