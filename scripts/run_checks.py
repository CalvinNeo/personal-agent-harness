#!/usr/bin/env python3
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SUITES = [
    ("plugins/long_term_memory", "test_*.py"),
    ("plugins/reminder", "test_*.py"),
    ("plugins/image_context", "test_*.py"),
    ("plugins/intent_router", "test_*.py"),
    ("plugins/codex_bridge", "test_*.py"),
    ("plugins/xhs_search", "test_*.py"),
    ("plugins/travel_assistant", "test_*.py"),
    ("services", "test_*.py"),
    ("tests", "test_portable_*.py"),
    ("tests", "test_qq_image_log_patch.py"),
    ("tests", "test_timeout_bridge.py"),
    ("tests", "test_public_image_download.py"),
]


def main():
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    for directory, pattern in SUITES:
        print("Running " + directory + "/" + pattern, flush=True)
        subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", directory, "-p", pattern, "-v"],
                       cwd=ROOT, env=environment, check=True)


if __name__ == "__main__":
    main()
