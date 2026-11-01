#!/usr/bin/env python3
"""Apply verified AstrBot 4.29.3 changes; refuse unknown or partially changed files."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess


def source_state(site_packages, entries):
    states = []
    for entry in entries:
        relative = Path(entry["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Unsafe manifest source path")
        target = site_packages / relative
        if target.is_symlink() or any(parent.is_symlink() for parent in target.parents if parent != site_packages and site_packages in parent.parents):
            raise ValueError("Refusing symlink source: " + str(relative))
        value = hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else None
        if value == entry["original_sha256"]:
            states.append("original")
        elif value == entry["patched_sha256"]:
            states.append("patched")
        else:
            raise ValueError("Refusing unknown source content: " + str(relative))
    if not states or len(set(states)) != 1:
        raise ValueError("Mixed patch state; restore the pinned baseline before retrying")
    return states[0]


def apply_patches(site_packages, *, check=False):
    if site_packages.is_symlink() or not site_packages.is_dir():
        raise ValueError("Refusing invalid site-packages directory")
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "patches/manifest.json").read_text())
    if source_state(site_packages, manifest["files"]) == "patched":
        return "All pinned framework customizations already installed"
    command = ["patch", "--batch", "--forward", "-p1", "-i", str(root / "patches/astrbot-4.29.3-customizations.patch")]
    subprocess.run(command + ["--dry-run"], cwd=site_packages, check=True, capture_output=True)
    if check:
        return "All pinned framework customizations are applicable; no changes made"
    subprocess.run(command, cwd=site_packages, check=True, capture_output=True)
    if source_state(site_packages, manifest["files"]) != "patched":
        raise ValueError("Post-patch verification failed")
    return "All pinned framework customizations applied and verified"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site-packages", required=True, type=Path)
    parser.add_argument("--check", action="store_true", help="Verify applicability without changing files")
    args = parser.parse_args()
    try:
        print(apply_patches(args.site_packages, check=args.check))
    except (ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()
