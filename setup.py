#!/usr/bin/env python3
"""Install the standalone Skill and a private credential; verify with GET only.

SPDX-License-Identifier: AGPL-3.0-only
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid

FILES = ('SKILL.md', 'yuheng_client.py', 'setup.py', 'README.md', 'LICENSE', 'NOTICE.txt')
OVERRIDES = ('YUHENG_ENDPOINT', 'MUSE_ENDPOINT', 'YUHENG_API_KEY_FILE', 'MUSE_API_KEY_FILE',
             'YUHENG_FALLBACK_ENDPOINT', 'YUHENG_MEDIA_TIMEOUT_SECONDS', 'MUSE_MEDIA_TIMEOUT_SECONDS')

class InstallError(Exception):
    pass

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def safe_path(path, *, allow_leaf_link=False):
    """Refuse existing links, special files and shared credential inodes."""
    if not path.is_absolute() or '..' in path.parts:
        raise InstallError('Installation paths must be absolute without .. components')
    for part in (list(reversed(path.parents)) + [path]):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if allow_leaf_link and part == path and stat.S_ISLNK(info.st_mode):
            continue
        if stat.S_ISLNK(info.st_mode):
            raise InstallError('Refusing a symlink in an installation path')
        if not stat.S_ISREG(info.st_mode) and not stat.S_ISDIR(info.st_mode):
            raise InstallError('Refusing a special file in an installation path')
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise InstallError('Refusing a hardlinked installation file')

def private_directory(path):
    safe_path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)

def private_write(path, contents):
    safe_path(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(str(path), flags, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(contents)
        stream.flush()
        os.fsync(stream.fileno())

def endpoint_value(value):
    url = urllib.parse.urlsplit(value)
    if (url.username or url.password or url.query or url.fragment or not url.hostname
            or url.path not in ('', '/') or (url.scheme != 'https' and not
               (url.scheme == 'http' and url.hostname in ('localhost', '127.0.0.1', '::1')))):
        raise InstallError('Endpoints require HTTPS, or loopback HTTP, without credentials or paths')
    return value.rstrip('/')

def verify_connection(endpoint, fallback, prefix, key):
    """The reserved unknown group is reachable only after bearer authentication."""
    opener = urllib.request.build_opener(NoRedirect)
    for target in (endpoint, fallback):
        if not target:
            continue
        request = urllib.request.Request(target + prefix + '/groups/installation-check', method='GET',
                                         headers={'Authorization': 'Bearer ' + key})
        try:
            with opener.open(request, timeout=5) as response:
                status, body = response.status, response.read(65537)
        except urllib.error.HTTPError as error:
            status, body = error.code, error.read(65537)
            error.close()
        except (urllib.error.URLError, TimeoutError, OSError):
            if target == endpoint and fallback:
                continue
            raise InstallError('Read-only connection verification failed: endpoint unreachable') from None
        if status in (502, 504) and target == endpoint and fallback:
            continue
        if status == 401:
            raise InstallError('Read-only connection verification failed: key rejected (401)')
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeError):
            payload = None
        if (status == 404 and isinstance(payload, dict) and isinstance(payload.get('error'), dict)
                and payload['error'].get('code') == 'NOT_FOUND'):
            return
        raise InstallError('Read-only connection verification failed: unexpected platform response')
    raise InstallError('Read-only connection verification failed: no reachable endpoint')

def install(args, key):
    endpoint = endpoint_value(args.endpoint)
    fallback = endpoint_value(args.fallback_endpoint) if args.fallback_endpoint else None
    if fallback == endpoint:
        raise InstallError('Fallback must differ from the primary endpoint')
    conflicts = [name for name in OVERRIDES if os.environ.get(name)]
    if conflicts:
        raise InstallError('Conflicting environment overrides; clear these for this process and later Skill use: '
                           + ', '.join(conflicts))
    source = Path(__file__).resolve().parent
    payloads = {}
    for name in FILES:
        path = source / name
        safe_path(path)
        payloads[name] = path.read_bytes()
    home = Path.home()
    skill = Path(os.path.abspath(os.path.expanduser(args.skill_dir))) if args.skill_dir else home / '.codex/skills/yuheng-video-gen'
    config_dir = home / '.config/yuheng-star'
    config_path = config_dir / 'client.json'
    legacy_path = home / '.config/muse-fleet/client.json'
    protected = (home, config_dir, legacy_path.parent, source)
    if any(skill == path or skill in path.parents for path in protected) or config_dir in skill.parents or legacy_path.parent in skill.parents:
        raise InstallError('Skill directory must not overlap home, private config, or installer source')
    safe_path(skill, allow_leaf_link=True)
    safe_path(config_path)
    safe_path(legacy_path)
    old_path = config_path if config_path.exists() else legacy_path
    old = {}
    changed_route = False
    if old_path.exists():
        if old_path.stat().st_mode & 0o077:
            raise InstallError('Existing client config must be private (0600)')
        try:
            old = json.loads(old_path.read_text())
        except (ValueError, UnicodeError):
            raise InstallError('Existing client config is invalid; preserve and repair it before installing') from None
        if not isinstance(old, dict):
            raise InstallError('Existing client config must be an object')
        previous_key = old.get('api_key_file')
        if not isinstance(previous_key, str):
            raise InstallError('Existing configuration lacks a credential path')
        previous_path = Path(previous_key)
        safe_path(previous_path)
        if previous_path.stat().st_mode & 0o077:
            raise InstallError('Existing key file must be private (0600)')
        changed_identity = previous_path.read_text().strip() != key
        changed_route = (old.get('endpoint') != endpoint or old.get('fallback_endpoint') != fallback
                         or old.get('api_prefix', '/muse/v1') != args.api_prefix)
        if (changed_identity or changed_route) and not args.replace:
            raise InstallError('Existing identity or route differs; use --replace for the explicitly selected key/platform')
    if skill.is_symlink() and not args.replace:
        raise InstallError('Existing Skill is a link; use --replace to back up the link itself')
    if skill.exists() and not skill.is_dir():
        raise InstallError('Skill destination is not a directory')
    # Preflight preserved state before any mutation. Managed old files may be links:
    # they are never followed, and the original directory/link is backed up whole.
    preserved = []
    if skill.exists() and not skill.is_symlink():
        for path in skill.rglob('*'):
            if path.relative_to(skill).parts[0] in FILES:
                continue
            safe_path(path)
            preserved.append(path)
    aliases = old.get('state_endpoint_aliases', [])
    if not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases):
        raise InstallError('Existing state endpoint aliases are invalid')
    # A selected new route is not proof that historic aliases belong to it.
    # Keep them in the config backup; reactivate only explicitly confirmed ones.
    aliases = list(dict.fromkeys(([] if changed_route else aliases) + args.state_endpoint_alias))
    for alias in aliases:
        endpoint_value(alias)
    timeout = old.get('media_timeout_seconds', 930)
    if type(timeout) is not int or not 930 <= timeout <= 1800:
        raise InstallError('Existing media socket timeout is invalid')
    verify_connection(endpoint, fallback, args.api_prefix, key)
    private_directory(config_dir)
    private_directory(config_dir / 'keys')
    safe_path(skill.parent)
    skill.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = config_dir / '.setup.lock'
    try:
        private_write(lock, b'Private installation in progress\n')
    except FileExistsError:
        raise InstallError('Another installation is active; do not remove its lock') from None
    token = uuid.uuid4().hex
    key_path = config_dir / 'keys' / (token + '.key')
    temp_config = config_dir / ('client.json.new-' + token)
    backup_config = config_dir / ('client.json.backup-' + token)
    backup_skill = skill.with_name(skill.name + '.backup-' + token)
    staging = None
    skill_moved = False
    skill_installed = False
    config_installed = False
    try:
        staging = Path(tempfile.mkdtemp(prefix='.yuheng-install-', dir=str(skill.parent)))
        for name, contents in payloads.items():
            private_write(staging / name, contents)
        for path in preserved:
            dest = staging / path.relative_to(skill)
            if path.is_dir():
                dest.mkdir(parents=True, exist_ok=True, mode=0o700)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                private_write(dest, path.read_bytes())
        private_write(key_path, key.encode() + b'\n')
        config = dict(old)
        config.update(endpoint=endpoint, api_prefix=args.api_prefix, api_key_file=str(key_path),
                      media_timeout_seconds=timeout, state_endpoint_aliases=aliases)
        config.pop('fallback_endpoint', None)
        if fallback:
            config['fallback_endpoint'] = fallback
        private_write(temp_config, (json.dumps(config, indent=2) + '\n').encode())
        if old_path.exists():
            private_write(backup_config, old_path.read_bytes())
        safe_path(config_path)
        safe_path(skill, allow_leaf_link=True)
        if skill.exists() or skill.is_symlink():
            skill.rename(backup_skill)
            skill_moved = True
        staging.rename(skill)
        skill_installed = True
        os.replace(temp_config, config_path)
        config_installed = True
    finally:
        # Failure leaves the previous installation/config active. The unique key
        # is removed unless its config became active; old keys are never deleted.
        if not config_installed:
            if skill_installed:
                shutil.rmtree(skill)
            if skill_moved:
                backup_skill.rename(skill)
            if key_path.exists():
                key_path.unlink()
        if staging is not None and staging.exists():
            shutil.rmtree(staging)
        if temp_config.exists():
            temp_config.unlink()
        lock.unlink()
    print('Installed and authenticated by read-only GET. No video was generated.')
    print('Skill: ' + str(skill))
    print('Private config: ' + str(config_path))

def main():
    parser = argparse.ArgumentParser(description='Install with a key on stdin; never create a video during setup.')
    parser.add_argument('--endpoint', required=True)
    parser.add_argument('--fallback-endpoint')
    parser.add_argument('--api-prefix', choices=('/video/v1', '/muse/v1'), default='/video/v1')
    parser.add_argument('--skill-dir', help='Absolute personal Skill directory; default ~/.codex/skills/yuheng-video-gen')
    parser.add_argument('--replace', action='store_true', help='Explicitly select this key/platform; back up previous installation/config')
    parser.add_argument('--state-endpoint-alias', action='append', default=[], help='Confirmed historical endpoint of the same platform only')
    args = parser.parse_args()
    if sys.stdin.isatty():
        raise InstallError('Provide the key on stdin; do not use a command argument')
    key = sys.stdin.read(514).strip()
    if not key or len(key) > 512 or not all(33 <= ord(c) <= 126 for c in key):
        raise InstallError('Invalid key input; expected one nonempty credential on stdin')
    install(args, key)

if __name__ == '__main__':
    try:
        main()
    except (InstallError, OSError, ValueError):
        # Exception detail from OS/HTTP may contain credentials supplied by a
        # remote error. Emit only controlled InstallError text, never a traceback.
        error = sys.exc_info()[1]
        print(str(error) if isinstance(error, InstallError) else 'Installation failed: filesystem or configuration error', file=sys.stderr)
        sys.exit(1)
