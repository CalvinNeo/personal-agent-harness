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
    "personal home directory": re.compile(r"/(?:Users|home)/[^/\s\"'<>]+(?:/|(?=[\s\"'<>]|$))"),
    "Windows personal home directory": re.compile(r"\b[A-Za-z]:[\\/]+(?:Users|Documents and Settings)[\\/]+[^\\/\s\"'<>]+", re.I),
    "JWT": re.compile(r"\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\b"),
    "QQ application identifier": re.compile(r'''["']?appid["']?\s*[:=]\s*["']?\d{6,}''', re.I),
    "literal credential": re.compile(r'''(?:api[_-]?key|appsecret|jwt_secret|password|secret)\s*["']?\s*[:=]\s*["'][A-Za-z0-9/+_.=-]{16,}["']''', re.I),
}
BLOCKED_NAMES = {
    "auth.json", "credentials.json", "cmd_config.json", "reminders.json", "admin-initial-password",
    "cookie.json", "cookies.json", "session.json", "sessions.json", "login-state.json", "login_state.json",
    "storage-state.json", "storage_state.json", "watches.json", "watch.json", "jobs.json", "job.json",
    "codex-jobs.json", "codex_jobs.json", "routes.json", "request-context.json", "request_context.json",
}
BLOCKED_DIRS = {
    "data", "state", "runtime", "workspaces", "backups", ".codex", ".astrbot", "__pycache__",
    "cookies", "sessions", "browser-profile", "browser_profile", "browser-profiles", "browser_profiles",
    "request-context", "request_context", "request-contexts", "request_contexts", ".playwright",
}
SAFE_NETWORKS = [ipaddress.ip_network(v) for v in ("127.0.0.0/8", "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")]
SAFE_V6_NETWORKS = [ipaddress.ip_network(v) for v in ("::/128", "::1/128", "2001:db8::/32")]
# This documented portable alias contains no deployment-specific account name.
PORTABLE_ROOT = re.compile(re.escape("/" + "root/agent_bot_workspace") + r"(?=/|[\s\"'<>`]|$)")
ROOT_HOME = re.compile(r"/" + r"root/[^/\s\"'<>`]+")
IDENTITY_VALUE = re.compile(
    r'''\b(?:sender(?:_id)?|group(?:_id|_openid)?|user(?:_id|_openid)?|member_openid|open_?id|qq(?:_id)?|app_?id)\b["']?\s*[:=]\s*["']?(\d{5,20})(?![\w.])''', re.I)
IDENTITY_LIST = re.compile(
    r'''\b(?:allowed|login|write|admin)_(?:senders|umos|users|groups)\b["']?\s*[:=]\s*\[([^\]]*)\]''', re.I)
NUMERIC_ID = re.compile(r'''(?<![\w.])\d{5,20}(?![\w.])''')
NUMERIC_UMO = re.compile(r":(?:Group|Friend|Private|Direct)Message:\d{5,20}(?![\w.])", re.I)
IPV6_LITERAL = re.compile(r"(?<![\w:])(?:[0-9a-f]{0,4}:){2,}[0-9a-f:.]*(?:%[\w.-]+)?", re.I)
NETWORK_CONTEXT = re.compile(r'''\b(?:host|hostname|endpoint|proxy|server|ip(?:v6)?|address|addr)\b["']?\s*[:=]|https?://\[''', re.I)


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(ROOT), *args], stderr=subprocess.DEVNULL)


def private_terms(path: Path) -> list[str]:
    """Load a denylist without requiring names to have four characters."""
    path = path.resolve()
    root = ROOT.resolve()
    if path == root or root in path.parents:
        raise ValueError("private terms file must be outside the repository")
    values = json.loads(path.read_text())
    if not isinstance(values, list) or any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError("private terms must be a JSON list of nonempty strings")
    return values


def safe_label(label: str, terms: list[str]) -> str:
    # Private filenames must not leak through otherwise redacted diagnostics.
    for term in sorted(terms, key=len, reverse=True):
        label = label.replace(term, "[private]")
    return label.replace("\n", "\\n").replace("\r", "\\r")


def inspect_content(label: str, data: bytes, terms: list[str]) -> list[str]:
    label = safe_label(label, terms)
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
        if ROOT_HOME.search(PORTABLE_ROOT.sub("/portable-root", line)):
            issues.append(f"{label}:{line_no}: root personal home directory")
        if (IDENTITY_VALUE.search(line) or NUMERIC_UMO.search(line)
                or any(NUMERIC_ID.search(match[1]) for match in IDENTITY_LIST.finditer(line))):
            issues.append(f"{label}:{line_no}: numeric account identifier")
        for match in re.finditer(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])", line):
            candidate = match[0]
            # A browser version in a user-agent is not a network endpoint.
            if re.search(r"(?:Chrome|Chromium|Firefox|Edg|Safari|Version)/$", line[:match.start()]):
                continue
            try:
                value = ipaddress.ip_address(candidate)
            except ValueError:
                continue
            if str(value) != "0.0.0.0" and not any(value in net for net in SAFE_NETWORKS):
                issues.append(f"{label}:{line_no}: non-example IPv4 address")
        for candidate in IPV6_LITERAL.findall(line):
            try:
                value = ipaddress.ip_address(candidate)
            except ValueError:
                continue
            if value.version != 6:
                continue
            mapped = value.ipv4_mapped
            if mapped is not None and (str(mapped) == "0.0.0.0" or any(mapped in net for net in SAFE_NETWORKS)):
                continue
            if not any(value in net for net in SAFE_V6_NETWORKS):
                # Special-use literals are common SSRF rejection examples. An
                # actual endpoint/configuration binding still requires review.
                embedded = value.sixtofour or (value.teredo[1] if value.teredo else None)
                if (not value.is_global or value.is_site_local or embedded is not None and not embedded.is_global) and not NETWORK_CONTEXT.search(line):
                    continue
                issues.append(f"{label}:{line_no}: non-example IPv6 address")
        for match in re.finditer(r"[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", line):
            # URI userinfo is not an email mailbox. Query-string email values
            # remain covered because /, ? and # end the authority prefix.
            if re.search(r'''[A-Za-z][A-Za-z0-9+.-]*://[^/\s"'<>?#@]*$''', line[:match.start()]):
                continue
            email = match[0]
            domain = email.rsplit("@", 1)[1].lower()
            if domain not in {"example.com", "example.org", "users.noreply.github.com"}:
                issues.append(f"{label}:{line_no}: non-example email address")
    return issues


def inspect_path(label: str, name: str, terms: list[str] = ()) -> list[str]:
    path = Path(name)
    label = safe_label(label, terms)
    issues = []
    if any(term in name for term in terms):
        issues.append(f"{label}: private denylist path match")
    # A template suffix cannot turn a runtime directory into public source.
    blocked_directory = bool({part.lower() for part in path.parts[:-1]} & BLOCKED_DIRS)
    basename = path.name.lower()
    blocked_file = not basename.endswith(".example") and (
        basename in BLOCKED_NAMES or basename.endswith((".env", ".pem", ".key", ".p12", ".pfx", ".log", ".har", ".db", ".sqlite", ".sqlite3", ".sqlite3-wal", ".sqlite3-shm", ".jsonl", ".pyc"))
        or basename.startswith(".env") or basename.endswith("_config.json")
        or re.fullmatch(r"(?:.*[-_.])?(?:cookie[s]?|session[s]?|login[-_]state|storage[-_]state|watch(?:es)?|jobs?|routes?|request[-_]context)\.json", basename))
    if blocked_directory or blocked_file:
        issues.append(f"{label}: runtime/credential path is not source")
    return issues


def inspect_history(terms: list[str]) -> tuple[list[str], int]:
    """Inspect every historical path, while reading each blob only once."""
    issues, count = [], 0
    paths, blobs = set(), set()

    def inspect_tree(tree):
        nonlocal count
        # rev-list --objects chooses just one name per blob. Walk each tree so
        # a harmless blob reused as cookies.json or state/x.example is caught.
        for entry in git("ls-tree", "-r", "-z", tree).split(b"\0"):
            if not entry:
                continue
            metadata, raw_name = entry.split(b"\t", 1)
            mode, kind, oid = metadata.decode().split()
            name = raw_name.decode("utf-8", "surrogateescape")
            if (mode, name) not in paths:
                label = "history:" + name
                issues.extend(inspect_path(label, name, terms))
                if mode in {"120000", "160000"}:
                    issues.append(f"{safe_label(label, terms)}: symlink/submodule requires manual review")
                paths.add((mode, name))
            if kind == "blob" and oid not in blobs:
                issues.extend(inspect_content("history:" + name, git("cat-file", "blob", oid), terms))
                blobs.add(oid)
                count += 1

    for commit in git("rev-list", "--all").decode().splitlines():
        issues.extend(inspect_content("commit:" + commit[:12], git("cat-file", "commit", commit), terms))
        count += 1
        inspect_tree(commit)
    # Annotated tags have their own messages and tagger identity, including
    # nested tags; scan reachable tag objects rather than just branch commits.
    for oid in git("rev-list", "--objects", "--all", "--no-object-names").decode().splitlines():
        kind = git("cat-file", "-t", oid).strip()
        if kind == b"tag":
            data = git("cat-file", "tag", oid)
            issues.extend(inspect_content("tag:" + oid[:12], data, terms))
            if b"\ntype tree\n" in data:
                inspect_tree(data.splitlines()[0].split()[1].decode())
            count += 1
        elif kind == b"blob" and oid not in blobs:
            issues.extend(inspect_content("history-object:" + oid[:12], git("cat-file", "blob", oid), terms))
            blobs.add(oid)
            count += 1
    # A lightweight tag can also point directly at a standalone tree.
    for line in git("for-each-ref", "--format=%(objecttype) %(objectname)", "refs/tags").decode().splitlines():
        kind, oid = line.split()
        if kind == "tree":
            inspect_tree(oid)
    # Ref names can themselves carry private account or project names.
    issues.extend(inspect_content("git-refnames", git("for-each-ref", "--format=%(refname)"), terms))
    return issues, count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true", help="Inspect the complete Git index")
    parser.add_argument("--history", action="store_true", help="Also inspect all reachable Git blobs and commit metadata")
    parser.add_argument("--private-terms-file", type=Path, help="External JSON string list; never prints its values")
    args = parser.parse_args()
    terms = []
    if args.private_terms_file:
        try:
            terms = private_terms(args.private_terms_file)
        except (ValueError, OSError):
            parser.error("private terms file must be outside the repository and contain a JSON list of nonempty strings")
    issues = []
    count = 0
    modes = {}
    if args.staged:
        for entry in git("ls-files", "--stage", "-z").split(b"\0"):
            if not entry:
                continue
            metadata, raw_name = entry.split(b"\t", 1)
            mode, oid, stage = metadata.decode().split()
            name = raw_name.decode("utf-8", "surrogateescape")
            modes[name] = mode
            if stage != "0":
                issues.append(f"index:{safe_label(name, terms)}: unresolved index conflict")
        files = modes
    else:
        files = git("ls-files", "--cached", "--others", "--exclude-standard", "-z").decode().split("\0")
    for name in sorted(set(files) - {""}):
        label = ("index:" if args.staged else "worktree:") + name
        path = ROOT / name
        if modes.get(name) in {"120000", "160000"} or (not args.staged and path.is_symlink()):
            issues.append(f"{label}: symlinks require manual review")
            continue
        try:
            data = git("show", ":" + name) if args.staged else path.read_bytes()
        except (OSError, subprocess.CalledProcessError):
            issues.append(f"{label}: source file unavailable for inspection")
            continue
        issues.extend(inspect_path(label, name, terms))
        issues.extend(inspect_content(label, data, terms))
        count += 1
    if args.history:
        findings, checked = inspect_history(terms)
        issues.extend(findings)
        count += checked
    for issue in sorted({safe_label(issue, terms) for issue in issues}):
        print(issue)
    if issues:
        print(f"FAIL: {len(set(issues))} findings; matched values were not printed")
        return 1
    print(f"PASS: {count} source/history objects checked; no configured privacy rule matched")
    return 0


if __name__ == "__main__":
    sys.exit(main())
