import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import uuid


BASE = pathlib.Path('/var/lib/codexrunner/workspaces')
CODE = pathlib.Path('/opt/astrbot-codex-runner')
sys.path.insert(0, str(CODE))
specification = importlib.util.spec_from_file_location('migration_runner_probe', CODE / 'codex-runner.py')
runner = importlib.util.module_from_spec(specification)
sys.modules[specification.name] = runner
specification.loader.exec_module(runner)
name = uuid.uuid4().hex
workspace = BASE / name
workspace.mkdir(mode=0o700)
(workspace / '.tmp').mkdir(mode=0o700)
try:
    arguments = runner.command(workspace, 'read-only')
    endpoint = arguments.index('/usr/local/bin/codex')
    wrapper = arguments[:endpoint]
    test = (
        'test -r /opt/astrbot-codex-runner/codex-runner.py && '
        'test ! -r /var/lib/astrbot/data/cmd_config.json && '
        'test ! -r /var/lib/codexproxy/upstream-token && '
        'test ! -r /etc/xhs/api-token && '
        'test ! -r /root/agent_bot_workspace/data/astrbot/data/cmd_config.json && '
        'test ! -e /run/astrbot-codex/bridge.sock && '
        'test ! -e /var/lib/codexrunner/jobs && '
        'test -r /var/lib/codexrunner/codex/config.toml && '
        'printf probe > .tmp/probe.txt && '
        '! touch outside-workspace.txt && '
        'echo "PASS sandbox: assigned tmp writable, host state and credentials isolated"'
    )
    result = subprocess.run(wrapper + ['/bin/sh', '-c', test], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    print(json.dumps({'returncode': result.returncode, 'stdout': result.stdout.strip(), 'stderr': result.stderr[-1200:]}, ensure_ascii=False))
    if result.returncode:
        raise SystemExit(result.returncode)
finally:
    shutil.rmtree(workspace)
