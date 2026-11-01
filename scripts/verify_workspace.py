import argparse
import collections
import json
import os
import pathlib
import sqlite3
import subprocess
import urllib.error
import urllib.request


BASE = pathlib.Path('/root/agent_bot_workspace')
ALIASES = (
    ('/opt/astrbot', 'utils/astrbot-runtime'),
    ('/opt/astrbot-codex-runner', 'utils/codex-runner'),
    ('/opt/xiaohongshu-mcp', 'utils/xiaohongshu-mcp'),
    ('/opt/agent-bot-plugins', 'utils/plugins'),
    ('/var/lib/astrbot', 'data/astrbot'),
    ('/var/lib/codexrunner', 'data/codexrunner'),
    ('/var/lib/codexproxy', 'data/codexproxy'),
    ('/var/lib/xhs', 'data/xhs'),
    ('/etc/astrbot', 'data/config/astrbot'),
    ('/etc/xhs', 'data/config/xhs'),
)
SERVICES = ('astrbot.service', 'astrbot-codex-runner.service', 'codex-api-proxy.service', 'xhs-mcp.service')
results = {}


def command(*arguments, expected=0):
    result = subprocess.run(arguments, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if result.returncode != expected:
        raise AssertionError(f'{arguments[0]}: {result.returncode}: {result.stdout[-1500:]}')
    return result.stdout


def access(account, path, mode='readable', allowed=True):
    operation = '-r' if mode == 'readable' else '-w'
    result = subprocess.run(['runuser', '-u', account, '--', 'test', operation, path])
    assert (result.returncode == 0) == allowed, (account, mode, path)


def main():
    global BASE
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', type=pathlib.Path, default=BASE)
    arguments = parser.parse_args()
    BASE = arguments.base
    for alias, relative in ALIASES:
        command('mountpoint', '-q', alias)
        assert os.path.samefile(alias, BASE / relative), alias
        results[alias] = {'source': str(BASE / relative), 'same_inode': True}
    for service in SERVICES:
        command('systemctl', 'is-active', '--quiet', service)
        command('systemctl', 'is-enabled', '--quiet', service)
    results['services'] = {service: 'active and enabled' for service in SERVICES}
    command('findmnt', '--verify', '--tab-file', '/etc/fstab')
    for alias, relative in ALIASES:
        unit = command('systemd-escape', '--path', '--suffix=mount', alias).strip()
        command('systemctl', 'is-active', '--quiet', unit)
    results['persistent_mounts'] = len(ALIASES)
    access('astrbot', '/opt/agent-bot-plugins/codex_bridge/main.py')
    access('astrbot', '/var/lib/astrbot/data/cmd_config.json')
    access('astrbot', '/var/lib/astrbot/data/plugins/codex_bridge/jobs.json', 'writable')
    access('codexrunner', '/opt/astrbot-codex-runner/codex-runner.py')
    access('codexrunner', '/var/lib/codexrunner/jobs', 'writable')
    access('codexproxy', '/var/lib/codexproxy/upstream-token')
    access('xhs', '/etc/xhs/api-token')
    access('xhs', '/var/lib/xhs/data/cookies.json', 'writable')
    for account in ('astrbot', 'codexrunner', 'codexproxy', 'xhs'):
        access(account, '/root', allowed=False)
    access('codexrunner', '/var/lib/astrbot/data/cmd_config.json', allowed=False)
    access('codexrunner', '/var/lib/codexproxy/upstream-token', allowed=False)
    access('codexrunner', '/etc/xhs/api-token', allowed=False)
    results['account_isolation'] = 'passed'
    databases = ('/var/lib/astrbot/data/data_v4.db', '/var/lib/astrbot/data/plugins/long_term_memory/memory.sqlite3')
    for name in databases:
        with sqlite3.connect(f'file:{name}?mode=ro', uri=True) as database:
            assert database.execute('PRAGMA quick_check').fetchall() == [('ok',)], name
    results['sqlite_integrity'] = {name: 'ok' for name in databases}
    jobs = collections.Counter()
    for path in pathlib.Path('/var/lib/codexrunner/jobs').glob('*.json'):
        jobs[json.loads(path.read_text()).get('state')] += 1
    results['jobs'] = dict(jobs)
    for url in ('http://127.0.0.1:6185/', 'http://127.0.0.1:18060/health'):
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                assert response.status == 200
                response.read(512)
                results[url] = response.status
        except urllib.error.HTTPError as error:
            if url.endswith('/health') and error.code == 401:
                results[url] = 'reachable; authentication enforced'
            else:
                raise
    token = pathlib.Path('/etc/xhs/api-token').read_text().strip()
    request = urllib.request.Request('http://127.0.0.1:18060/health', headers={'Authorization': 'Bearer ' + token})
    with urllib.request.urlopen(request, timeout=10) as response:
        assert response.status == 200
        results['xhs_authenticated_health'] = 200
    results['proxy_token_permission'] = oct(pathlib.Path('/var/lib/codexproxy/upstream-token').stat().st_mode & 0o777)
    results['root_permission'] = oct(pathlib.Path('/root').stat().st_mode & 0o777)
    output = BASE / 'data/migration/verification.json'
    output.write_text(json.dumps(results, ensure_ascii=False, indent=2))
    output.chmod(0o600)
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
