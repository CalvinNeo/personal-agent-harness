#!/usr/bin/env python3
"""Check source/index/history without printing matched private values.

Optional private terms are a JSON list kept OUTSIDE this repository. Rules are
defense in depth, not a proof that arbitrary natural-language personal data is absent.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
RULES = {
    "API token": re.compile(r"\b(?:sk|ghp|gho|ghu|ghs|github_pat)-?[A-Za-z0-9_\-]{24,}\b"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "QQ/private 32-hex identifier": re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{32}(?![0-9a-fA-F])"),
    "personal home directory": re.compile(r"/(?:Users|home)/[A-Za-z0-9_.-]+/"),
    "JWT": re.compile(r"\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\b"),
    "QQ application identifier": re.compile(r'''["']?appid["']?\s*[:=]\s*["']?\d{6,}''', re.I),
    "literal credential": re.compile(r'''(?:api[_-]?key|appsecret|jwt_secret|password|secret)\s*["']?\s*[:=]\s*["'][A-Za-z0-9/+_.=-]{16,}["']''', re.I),
}
BLOCKED_NAMES = {"auth.json", "credentials.json", "cmd_config.json", "reminders.json", "admin-initial-password"}
BLOCKED_DIRS = {"data", "state", "runtime", "workspaces", "backups", ".codex", ".astrbot", "__pycache__"}
SAFE_NETWORKS = [ipaddress.ip_network(v) for v in ("127.0.0.0/8", "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")]


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(ROOT), *args], stderr=subprocess.DEVNULL)


def inspect_content(label: str, data: bytes, terms: list[str]) -> list[str]:
    issues = []
    if len(data) > 1024 * 1024:
        return [f"{label}: unexpected large file; review outside the source repository"]
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError:
        return [f"{label}: unexpected binary file"]
    for line_no, line in enumerate(content.splitlines(), 1):
        for category, pattern in RULES.items():
            if pattern.search(line):
                issues.append(f"{label}:{line_no}: {category}")
        if any(term in line for term in terms):
            issues.append(f"{label}:{line_no}: private denylist match")
        for candidate in re.findall(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])", line):
            try:
                value = ipaddress.ip_address(candidate)
            except ValueError:
                continue
            if str(value) != "0.0.0.0" and not any(value in net for net in SAFE_NETWORKS):
                issues.append(f"{label}:{line_no}: non-example IPv4 address")
        for email in re.findall(r"[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", line):
            domain = email.rsplit("@", 1)[1].lower()
            if domain not in {"example.com", "example.org", "users.noreply.github.com"}:
                issues.append(f"{label}:{line_no}: non-example email address")
    return issues


def inspect_path(label: str, name: str) -> list[str]:
    path = Path(name)
    if path.name.endswith(".example"):
        return []
    if (path.name in BLOCKED_NAMES or path.name.endswith((".env", ".pem", ".key", ".log", ".db", ".sqlite", ".sqlite3", ".sqlite3-wal", ".sqlite3-shm", ".jsonl", ".pyc"))
            or path.name.startswith(".env") or path.name.endswith("_config.json")
            or set(path.parts) & BLOCKED_DIRS):
        return [f"{label}: runtime/credential path is not source"]
    return []


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true", help="Inspect the complete Git index")
    parser.add_argument("--history", action="store_true", help="Also inspect all reachable Git blobs and commit metadata")
    parser.add_argument("--private-terms-file", type=Path, help="External JSON string list; never prints its values")
    args = parser.parse_args()
    terms = []
    if args.private_terms_file:
        path = args.private_terms_file.resolve()
        if path == ROOT or ROOT in path.parents:
            parser.error("private terms file must be outside the repository")
        terms = json.loads(path.read_text())
        if not isinstance(terms, list) or any(not isinstance(v, str) or len(v) < 4 for v in terms):
            parser.error("private terms must be a JSON list of strings, each at least four characters")
    issues = []
    count = 0
    if args.staged:
        files = git("ls-files", "-z").decode().split("\0")
    else:
        files = git("ls-files", "--cached", "--others", "--exclude-standard", "-z").decode().split("\0")
    for name in sorted(set(files) - {""}):
        label = ("index:" if args.staged else "worktree:") + name
        path = ROOT / name
        if path.is_symlink():
            issues.append(f"{label}: symlinks require manual review")
            continue
        try:
            data = git("show", ":" + name) if args.staged else path.read_bytes()
        except FileNotFoundError:
            issues.append(f"{label}: tracked file missing from worktree")
            continue
        issues.extend(inspect_path(label, name))
        issues.extend(inspect_content(label, data, terms))
        count += 1
    if args.history:
        objects = git("rev-list", "--objects", "--all").decode().splitlines()
        for row in objects:
            oid, _, name = row.partition(" ")
            kind = git("cat-file", "-t", oid).decode().strip()
            if kind == "blob":
                label = "history:" + name
                issues.extend(inspect_path(label, name))
                issues.extend(inspect_content(label, git("cat-file", "blob", oid), terms))
                count += 1
            elif kind == "commit":
                issues.extend(inspect_content("commit-metadata", git("cat-file", "commit", oid), terms))
    for issue in sorted(set(issues)):
        print(issue)
    if issues:
        print(f"FAIL: {len(set(issues))} findings; matched values were not printed")
        return 1
    print(f"PASS: {count} source/history objects checked; no configured privacy rule matched")
    return 0


if __name__ == "__main__":
    sys.exit(main())
