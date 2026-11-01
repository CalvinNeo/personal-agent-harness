import argparse
import datetime
import hashlib
import json
import os
import pathlib
import shutil
import stat
import subprocess


BASE = pathlib.Path('/root/agent_bot_workspace')
STAMP = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d')
BACKUP = BASE / 'data/backups' / f'workspace-migration-{STAMP}'
JOURNAL = BACKUP / 'migration.json'
SERVICES = (
    'astrbot.service',
    'astrbot-codex-runner.service',
    'xhs-mcp.service',
    'codex-api-proxy.service',
)
MOUNTS = (
    ('/opt/astrbot', 'utils/astrbot-runtime', False),
    ('/opt/astrbot-codex-runner', 'utils/codex-runner', False),
    ('/opt/xiaohongshu-mcp', 'utils/xiaohongshu-mcp', False),
    ('/var/lib/astrbot', 'data/astrbot', False),
    ('/var/lib/codexrunner', 'data/codexrunner', False),
    ('/var/lib/codexproxy', 'data/codexproxy', False),
    ('/var/lib/xhs', 'data/xhs', False),
    ('/etc/astrbot', 'data/config/astrbot', False),
    ('/etc/xhs', 'data/config/xhs', False),
    ('/opt/agent-bot-plugins', 'utils/plugins', True),
)
DEPENDENCIES = {
    'astrbot.service': ('/opt/astrbot', '/opt/agent-bot-plugins', '/var/lib/astrbot', '/etc/astrbot', '/etc/xhs'),
    'astrbot-codex-runner.service': ('/opt/astrbot', '/opt/astrbot-codex-runner', '/var/lib/codexrunner'),
    'codex-api-proxy.service': ('/opt/astrbot-codex-runner', '/var/lib/codexproxy'),
    'xhs-mcp.service': ('/opt/xiaohongshu-mcp', '/var/lib/xhs', '/etc/xhs'),
    'astrbot-firewall.service': ('/etc/astrbot',),
}


def configure(base):
    global BASE, BACKUP, JOURNAL
    if not base.is_absolute() or any(character.isspace() for character in str(base)):
        raise ValueError('Workspace path must be absolute without whitespace')
    BASE = base
    BACKUP = BASE / 'data/backups' / f'workspace-migration-{STAMP}'
    JOURNAL = BACKUP / 'migration.json'


def run(*arguments, check=True):
    result = subprocess.run(arguments, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if check and result.returncode:
        raise RuntimeError(f'{arguments[0]} failed ({result.returncode}): {result.stdout[-3000:]}')
    return result


def record(data):
    temporary = JOURNAL.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    temporary.chmod(0o600)
    os.replace(temporary, JOURNAL)


def file_digest(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def manifest():
    result = {}
    roots = [pathlib.Path(original) for original, _, _ in MOUNTS[:-1]]
    roots.append(pathlib.Path('/usr/local/libexec/xhs-mcp-start'))
    for root in roots:
        candidates = root.rglob('*') if root.is_dir() else [root]
        for path in candidates:
            if '__pycache__' in path.parts or path.suffix == '.pyc':
                continue
            metadata = path.stat()
            if not stat.S_ISREG(metadata.st_mode):
                continue
            result[str(path)] = {
                'sha256': file_digest(path), 'size': metadata.st_size,
                'mode': stat.S_IMODE(metadata.st_mode),
                'uid': metadata.st_uid, 'gid': metadata.st_gid,
            }
    return result


def compare_manifest(expected):
    problems = []
    for name, metadata in expected.items():
        path = pathlib.Path(name)
        try:
            current = path.stat()
            actual = {
                'sha256': file_digest(path), 'size': current.st_size,
                'mode': stat.S_IMODE(current.st_mode),
                'uid': current.st_uid, 'gid': current.st_gid,
            }
            if actual != metadata:
                problems.append(name)
        except OSError:
            problems.append(name)
    if problems:
        raise RuntimeError(f'Manifest mismatch: {problems[:15]}')


def preflight():
    if os.geteuid() != 0 or pathlib.Path('/root').stat().st_mode & 0o077:
        raise RuntimeError('Root access and private /root permissions are required')
    if BASE.exists() and (BASE.is_symlink() or BASE.stat().st_mode & 0o077):
        raise RuntimeError('Existing workspace root must be a private real directory')
    if JOURNAL.exists():
        raise RuntimeError('An existing migration journal must be reviewed first')
    for original, relative, readonly in MOUNTS:
        source = pathlib.Path(original)
        destination = BASE / relative
        if original == '/opt/agent-bot-plugins':
            if source.exists():
                raise RuntimeError(f'Unexpected existing mount alias: {source}')
        elif not source.is_dir() or source.is_symlink() or run('mountpoint', '-q', str(source), check=False).returncode == 0:
            raise RuntimeError(f'Unexpected source layout: {source}')
        if destination.exists():
            raise RuntimeError(f'Destination already exists: {destination}')
    for service in DEPENDENCIES:
        override = pathlib.Path('/etc/systemd/system') / f'{service}.d/workspace.conf'
        if override.exists():
            raise RuntimeError(f'Existing drop-in: {override}')
    if '# BEGIN agent-bot-workspace' in pathlib.Path('/etc/fstab').read_text():
        raise RuntimeError('Existing workspace mount configuration')
    for path in pathlib.Path('/var/lib/codexrunner/jobs').glob('*.json'):
        job = json.loads(path.read_text())
        if job.get('state') in ('queued', 'running', 'accepted'):
            raise RuntimeError(f'Active Codex job: {path.name}')
    print(json.dumps({
        'mounts': [{'source': str(BASE / relative), 'runtime_alias': original, 'readonly': readonly} for original, relative, readonly in MOUNTS],
        'stop_services': SERVICES,
        'root_mode_unchanged': '0700',
    }, ensure_ascii=False, indent=2), flush=True)


def split_plugins(data):
    plugin_root = BASE / 'data/astrbot/data/plugins'
    code_root = BASE / 'utils/plugins'
    code_root.mkdir(mode=0o755)
    code_root.chmod(0o755)
    for plugin in sorted(plugin_root.iterdir()):
        if not plugin.is_dir() or plugin.is_symlink():
            raise RuntimeError(f'Unexpected plugin entry: {plugin}')
        destination = code_root / plugin.name
        destination.mkdir(mode=0o755)
        destination.chmod(0o755)
        for path in sorted(plugin.iterdir()):
            if not path.is_file() or path.is_symlink():
                continue
            if path.suffix not in ('.py', '.yaml', '.yml', '.md') and path.name != '_conf_schema.json':
                continue
            target = destination / path.name
            os.rename(path, target)
            path.symlink_to(f'/opt/agent-bot-plugins/{plugin.name}/{path.name}')
            data['plugin_files'].append([str(path), str(target)])
            record(data)
    print(f'Separated {len(data["plugin_files"])} plugin source files from state', flush=True)


def migration():
    preflight()
    BACKUP.mkdir(mode=0o700, parents=True)
    data = {
        'phase': 'prepared', 'started_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'initially_active': [service for service in SERVICES if run('systemctl', 'is-active', '--quiet', service, check=False).returncode == 0],
        'moved': [], 'mounted': [], 'plugin_files': [], 'overrides': [], 'wrapper_moved': False,
    }
    shutil.copy2('/etc/fstab', BACKUP / 'fstab.before')
    record(data)
    try:
        for service in SERVICES:
            run('systemctl', 'stop', service)
        data['phase'] = 'stopped'
        data['stopped_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        record(data)
        for service in SERVICES:
            if run('systemctl', 'is-active', '--quiet', service, check=False).returncode == 0:
                raise RuntimeError(f'Service did not stop: {service}')
        expected = manifest()
        (BACKUP / 'live-files.before.json').write_text(json.dumps(expected, indent=2))
        (BACKUP / 'live-files.before.json').chmod(0o600)
        print(f'Services stopped; fingerprinted {len(expected)} live files', flush=True)
        for original, relative, readonly in MOUNTS[:-1]:
            source = pathlib.Path(original)
            destination = BASE / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.rename(source, destination)
            source.mkdir(mode=0o755)
            data['moved'].append([original, str(destination)])
            record(data)
        split_plugins(data)
        wrapper = pathlib.Path('/usr/local/libexec/xhs-mcp-start')
        wrapper_destination = BASE / 'utils/xiaohongshu-mcp/xhs-mcp-start'
        os.rename(wrapper, wrapper_destination)
        wrapper.symlink_to('/opt/xiaohongshu-mcp/xhs-mcp-start')
        data['wrapper_moved'] = True
        record(data)
        lines = ['\n# BEGIN agent-bot-workspace']
        for original, relative, readonly in MOUNTS:
            pathlib.Path(original).mkdir(mode=0o755, exist_ok=True)
            options = 'bind,ro' if readonly else 'bind'
            lines.append(f'{BASE / relative} {original} none {options} 0 0')
        lines.append('# END agent-bot-workspace\n')
        with pathlib.Path('/etc/fstab').open('a') as output:
            output.write('\n'.join(lines))
        for service, dependencies in DEPENDENCIES.items():
            override = pathlib.Path('/etc/systemd/system') / f'{service}.d/workspace.conf'
            override.parent.mkdir(mode=0o755, exist_ok=True)
            override.write_text('[Unit]\nRequiresMountsFor=' + ' '.join(dependencies) + '\n')
            override.chmod(0o644)
            data['overrides'].append(str(override))
            record(data)
        run('systemctl', 'daemon-reload')
        for original, relative, readonly in MOUNTS:
            unit = run('systemd-escape', '--path', '--suffix=mount', original).stdout.strip()
            run('systemctl', 'start', unit)
            data['mounted'].append(original)
            record(data)
        compare_manifest(expected)
        data['phase'] = 'migrated-and-verified'
        data['verified_files'] = len(expected)
        record(data)
        print(f'All {len(expected)} original file contents, owners and modes match', flush=True)
        restart(data)
    except BaseException:
        if data.get('phase') not in ('migrated-and-verified', 'services-restarted'):
            rollback(data)
        raise


def restart(data, phase='services-restarted'):
    for service in reversed(SERVICES):
        if service in data['initially_active']:
            run('systemctl', 'start', service)
    data['phase'] = phase
    data['restarted_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    record(data)
    print('All previously active services restarted', flush=True)


def rollback(data):
    print('Rolling back migration', flush=True)
    for service in SERVICES:
        run('systemctl', 'stop', service, check=False)
    for original, _, _ in reversed(MOUNTS):
        if run('mountpoint', '-q', original, check=False).returncode == 0:
            run('umount', original)
    for source, destination in reversed(data['plugin_files']):
        path = pathlib.Path(source)
        if path.is_symlink():
            path.unlink()
        os.rename(destination, source)
    wrapper = pathlib.Path('/usr/local/libexec/xhs-mcp-start')
    if data['wrapper_moved']:
        wrapper.unlink()
        os.rename(BASE / 'utils/xiaohongshu-mcp/xhs-mcp-start', wrapper)
    for original, destination in reversed(data['moved']):
        pathlib.Path(original).rmdir()
        os.rename(destination, original)
    alias = pathlib.Path('/opt/agent-bot-plugins')
    if alias.exists():
        alias.rmdir()
    for override in data['overrides']:
        pathlib.Path(override).unlink(missing_ok=True)
    shutil.copy2(BACKUP / 'fstab.before', '/etc/fstab')
    run('systemctl', 'daemon-reload')
    data['phase'] = 'rolled-back'
    record(data)
    restart(data, phase='rolled-back')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('plan', 'migrate', 'rollback'))
    parser.add_argument('--base', type=pathlib.Path, default=BASE)
    arguments = parser.parse_args()
    configure(arguments.base)
    os.umask(0o077)
    if arguments.action == 'plan':
        preflight()
    elif arguments.action == 'migrate':
        migration()
    elif arguments.action == 'rollback':
        rollback(json.loads(JOURNAL.read_text()))


if __name__ == '__main__':
    main()
