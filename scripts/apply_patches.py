#!/usr/bin/env python3
"""Apply verified AstrBot 4.29.3 changes; refuse unknown or partially changed files."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site-packages", required=True, type=Path)
    parser.add_argument("--check", action="store_true", help="Verify applicability without changing files")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "patches/manifest.json").read_text())
    states = []
    for entry in manifest["files"]:
        value = hashlib.sha256((args.site_packages / entry["path"]).read_bytes()).hexdigest()
        if value == entry["original_sha256"]:
            states.append("original")
        elif value == entry["patched_sha256"]:
            states.append("patched")
        else:
            raise SystemExit(f"Refusing unknown source content: {entry['path']}")
    if len(set(states)) != 1:
        raise SystemExit("Mixed patch state; restore both original files before retrying")
    if states[0] == "patched":
        print("Both pinned patches already installed")
        return
    command = ["patch", "--batch", "--forward", "-p1", "-i", str(root / "patches/astrbot-4.29.3-customizations.patch")]
    subprocess.run(command + ["--dry-run"], cwd=args.site_packages, check=True)
    if args.check:
        print("Both pinned patches are applicable; no changes made")
        return
    subprocess.run(command, cwd=args.site_packages, check=True)
    for entry in manifest["files"]:
        actual = hashlib.sha256((args.site_packages / entry["path"]).read_bytes()).hexdigest()
        if actual != entry["patched_sha256"]:
            raise SystemExit("Post-patch verification failed")
    print("Both patches applied and verified")


if __name__ == "__main__":
    main()
