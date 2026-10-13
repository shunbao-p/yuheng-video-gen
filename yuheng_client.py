#!/usr/bin/env python3
"""Video-only Muse client. Stable submission state; no SSH or automatic generation retry."""
import argparse
from contextlib import contextmanager
import hashlib
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

class UploadOutcomeUnknown(RuntimeError):
    """An upload may already have taken effect; never repeat that POST blindly."""

class SlowRouteError(OSError):
    """The active download route has stalled or remained too slow."""

class Client:
    def __init__(self, endpoint, key_file, api_prefix='/muse/v1', media_timeout_seconds=930,
                 fallback_endpoint=None, route_state_file=None, state_endpoint_aliases=()):
        url = urllib.parse.urlsplit(endpoint)
        if url.username or url.password or url.query or url.fragment or not url.hostname:
            raise ValueError('Invalid endpoint')
        if url.scheme != 'https' and not (url.scheme == 'http' and url.hostname in ('127.0.0.1', '::1', 'localhost')):
            raise ValueError('HTTPS required except loopback')
        key_path = Path(key_file)
        if key_path.stat().st_mode & 0o077:
            raise ValueError('Key file must be private (0600)')
        self.key = key_path.read_text().strip()
        if not self.key or '\n' in self.key:
            raise ValueError('Invalid key file')
        if api_prefix not in ('/muse/v1', '/video/v1'):
            raise ValueError('Invalid API prefix')
        self.api_prefix = api_prefix
        if not isinstance(media_timeout_seconds, int) or not 930 <= media_timeout_seconds <= 1800:
            raise ValueError('Media socket timeout must be 930–1800 seconds')
        self.media_timeout_seconds = media_timeout_seconds
        self.endpoint = endpoint.rstrip('/')
        self.fallback_endpoint = None
        if fallback_endpoint:
            fallback = urllib.parse.urlsplit(fallback_endpoint)
            if (fallback.username or fallback.password or fallback.query or fallback.fragment
                    or not fallback.hostname or fallback.path not in ('', '/')
                    or (fallback.scheme != 'https' and not (fallback.scheme == 'http'
                        and fallback.hostname in ('127.0.0.1', '::1', 'localhost')))):
                raise ValueError('Invalid fallback endpoint; HTTPS required except loopback')
            self.fallback_endpoint = fallback_endpoint.rstrip('/')
            if self.fallback_endpoint == self.endpoint:
                raise ValueError('Fallback must differ from primary endpoint')
        if not isinstance(state_endpoint_aliases, (list, tuple)) or not all(isinstance(value, str) for value in state_endpoint_aliases):
            raise ValueError('Invalid state endpoint aliases')
        self.state_endpoint_aliases = tuple(state_endpoint_aliases)
        self.active_endpoint = self.endpoint
        self.route_state_file = Path(route_state_file) if route_state_file else None
        self.fallback_until = 0
        self.next_probe = 0
        self.probe_timeout_seconds = 5
        self.slow_window_seconds = 15
        self.minimum_download_bytes_per_second = 100 * 1024
        if self.fallback_endpoint and self.route_state_file and self.route_state_file.exists():
            if self.route_state_file.stat().st_mode & 0o077:
                raise ValueError('Route state must be private (0600)')
            try:
                saved = json.loads(self.route_state_file.read_text())
                if saved.get('primary') == self.endpoint and saved.get('fallback') == self.fallback_endpoint:
                    until = float(saved.get('until', 0))
                    if time.time() < until <= time.time() + 300:
                        self.fallback_until = until
                        self.active_endpoint = self.fallback_endpoint
            except (ValueError, TypeError, AttributeError):
                pass  # Route hints are optional; task identity never depends on them.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def use_fallback(self):
        if not self.fallback_endpoint or self.active_endpoint != self.endpoint:
            return False
        self.active_endpoint = self.fallback_endpoint
        self.fallback_until = time.time() + 300
        if self.route_state_file:
            atomic_json(self.route_state_file, {'primary': self.endpoint,
                        'fallback': self.fallback_endpoint, 'until': self.fallback_until})
        return True

    def select_route(self):
        if not self.fallback_endpoint:
            return
        if self.active_endpoint == self.fallback_endpoint and time.time() < self.fallback_until:
            return
        if self.active_endpoint == self.endpoint and time.monotonic() < self.next_probe:
            return
        # This authenticated, read-only lookup never creates a job or consumes units.
        # Two bounded probes distinguish sustained trouble from a single slow sample.
        for _ in range(2):
            start = time.monotonic()
            request = urllib.request.Request(self.endpoint + self.api_prefix + '/groups/installation-check')
            request.add_header('Authorization', 'Bearer ' + self.key)
            try:
                with self.opener.open(request, timeout=self.probe_timeout_seconds):
                    healthy = time.monotonic() - start < self.probe_timeout_seconds
            except urllib.error.HTTPError as error:
                healthy = error.code not in (502, 504) and time.monotonic() - start < self.probe_timeout_seconds
                error.close()
            except (urllib.error.URLError, TimeoutError, OSError):
                healthy = False
            if healthy:
                self.active_endpoint = self.endpoint
                self.fallback_until = 0
                self.next_probe = time.monotonic() + 60
                if self.route_state_file:
                    atomic_json(self.route_state_file, {'primary': self.endpoint,
                                'fallback': self.fallback_endpoint, 'until': 0})
                return
        self.active_endpoint = self.endpoint
        self.use_fallback()

    def request(self, method, path, data=None):
        if path.startswith('/muse/v1/'):
            path = self.api_prefix + path[len('/muse/v1'):]
        upload = method == 'POST' and path == self.api_prefix + '/references'
        media = upload or (method == 'GET' and path.endswith('/content'))
        self.select_route()
        replay_safe = method == 'GET'
        if method == 'POST' and path == self.api_prefix + '/groups':
            try:
                replay_safe = bool(json.loads(data).get('submission_id'))
            except (ValueError, TypeError, AttributeError):
                pass
        delay = 1
        for attempt in range(6):
            req = urllib.request.Request(self.active_endpoint + path, data=data, method=method)
            req.add_header('Authorization', 'Bearer ' + self.key)
            req.add_header('Content-Type', 'application/json' if path == self.api_prefix + '/groups' else 'application/octet-stream')
            try:
                # This bounds socket operations, not total download duration.
                # The server independently enforces media waiting and lease.
                return self.opener.open(req, timeout=self.media_timeout_seconds if media else (15 if self.fallback_endpoint else 600))
            except urllib.error.HTTPError as err:
                retry_after = err.headers.get('Retry-After') if err.headers else None
                with err:
                    body = err.read(65536)
                code = str(err.code)
                try:
                    code = json.loads(body).get('error', {}).get('code', code)
                except (ValueError, AttributeError):
                    pass
                pre_media_rejection = (err.code == 503 and code == 'SERVICE_BUSY') or (err.code == 429 and code == 'RATE_LIMIT')
                if err.code in (502, 504) and self.use_fallback():
                    if upload:
                        raise UploadOutcomeUnknown('Upload outcome is unknown; preserve the state and do not repeat this POST.') from None
                    if replay_safe:
                        continue
                retryable = pre_media_rejection if upload else err.code in (429, 503)
                if retryable and attempt < 5:
                    wait = delay
                    if retry_after:
                        try:
                            wait = min(30, max(1, int(retry_after)))
                        except ValueError:
                            wait = delay
                    time.sleep(wait + (attempt % 3) * 0.1)
                    delay = min(20, delay * 2)
                    continue
                if upload and err.code >= 500 and not pre_media_rejection:
                    raise UploadOutcomeUnknown('Upload outcome is unknown; keep the state file and ask an administrator to check the original upload. Do not repeat this POST.') from None
                raise RuntimeError('Video request rejected: ' + code) from None
            except (urllib.error.URLError, TimeoutError, OSError):
                switched = self.use_fallback()
                if upload:
                    raise UploadOutcomeUnknown('Upload outcome is unknown; keep the state file and ask an administrator to check the original upload. Do not repeat this POST.') from None
                if switched and replay_safe:
                    continue
                raise RuntimeError('Connection interrupted; retry with the same state file. Do not create a new submission.') from None
        raise RuntimeError('Connection interrupted; retry with the same state file. Do not create a new submission.')

    def json(self, method, path, data=None):
        for attempt in range(2):
            try:
                with self.request(method, path, data) as resp:
                    return json.load(resp)
            except (OSError, ValueError, http.client.IncompleteRead) as error:
                upload = method == 'POST' and path in ('/muse/v1/references', '/video/v1/references')
                switched = self.use_fallback()
                if upload:
                    raise UploadOutcomeUnknown('Upload acknowledgement is unknown; preserve the state and do not repeat this POST.') from None
                safe = method == 'GET'
                if method == 'POST' and path in ('/muse/v1/groups', '/video/v1/groups'):
                    try:
                        safe = bool(json.loads(data).get('submission_id'))
                    except (ValueError, TypeError, AttributeError):
                        pass
                if attempt == 0 and switched and safe:
                    continue
                raise RuntimeError('Response interrupted; recover with the same state file.') from error

def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.muse-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)

def prepare(plan_path):
    plan = json.loads(Path(plan_path).read_text())
    clips = plan.get('clips')
    if not isinstance(clips, list) or not 1 <= len(clips) <= 10:
        raise ValueError('A group needs 1–10 clips')
    canonical = []
    for clip in clips:
        if clip.get('duration', 10) != 10 or not clip.get('prompt'):
            raise ValueError('Each clip must have a prompt and duration 10')
        files = []
        for name in clip.get('references', []):
            path = Path(name)
            if not path.is_absolute():
                path = Path(plan_path).resolve().parent / path
            data = path.read_bytes()
            if not 0 < len(data) <= 20 * 1024 * 1024:
                raise ValueError('Reference exceeds 20MiB')
            files.append({'path': str(path.resolve()), 'hash': hashlib.sha256(data).hexdigest()})
        canonical.append({'prompt': clip['prompt'], 'style': clip.get('style', ''), 'duration': 10, 'references': files})
    fingerprint = []
    for clip in canonical:
        fingerprint.append({**clip, 'references': [f['hash'] for f in clip['references']]})
    digest = hashlib.sha256(json.dumps(fingerprint, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return canonical, digest

@contextmanager
def submission_lock(state_path):
    state_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(state_path) + '.lock', os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, 'r+b') as lock:
        if os.name == 'nt':
            import msvcrt
            if not os.fstat(lock.fileno()).st_size:
                lock.write(b'\0')
                lock.flush()
            lock.seek(0)
            acquire = lambda: msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            release = lambda: msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            acquire = lambda: fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            release = lambda: fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        try:
            acquire()
        except OSError:
            raise RuntimeError('Submission already running with this state file; retry after that process exits.') from None
        try:
            yield
        finally:
            release()
    # Keep the lock inode: unlinking it would let another caller bypass a held lock.

def submit(client, plan_path, state_path):
    state_path = Path(state_path).resolve()
    with submission_lock(state_path):
        clips, digest = prepare(plan_path)
        if state_path.exists():
            state = json.loads(state_path.read_text())
            if state['input_hash'] != digest or (state['endpoint'] != client.endpoint and state['endpoint'] not in getattr(client, 'state_endpoint_aliases', ())):
                raise ValueError('State belongs to a different request. Intentional redo costs new units and needs a new state file.')
        else:
            state = {'submission_id': uuid.uuid4().hex, 'input_hash': digest, 'endpoint': client.endpoint, 'uploads': {}}
            atomic_json(state_path, state)  # Persist before the first network request.
        if state.get('group_id'):
            return client.json('GET', '/muse/v1/groups/' + state['group_id'])
        if state.get('upload_pending'):
            raise UploadOutcomeUnknown('An earlier upload outcome is unknown; keep this state file and check the original upload before resuming. Do not create a new state to repeat it.')
        payload = {'submission_id': state['submission_id'], 'clips': []}
        for clip in clips:
            refs = []
            for f in clip['references']:
                if f['hash'] not in state['uploads']:
                    data = Path(f['path']).read_bytes()
                    if hashlib.sha256(data).hexdigest() != f['hash']:
                        raise ValueError('Reference changed during upload')
                    state['upload_pending'] = f['hash']
                    atomic_json(state_path, state)
                    try:
                        asset = client.json('POST', '/muse/v1/references', data)
                    except UploadOutcomeUnknown:
                        raise
                    except RuntimeError:
                        # A known rejection may be corrected and resumed. An
                        # unknown transport/5xx result deliberately retains it.
                        state.pop('upload_pending', None)
                        atomic_json(state_path, state)
                        raise
                    if asset['sha256'] != f['hash']:
                        raise ValueError('Reference hash mismatch')
                    state['uploads'][f['hash']] = asset['id']
                    state.pop('upload_pending', None)
                    atomic_json(state_path, state)
                refs.append(state['uploads'][f['hash']])
            payload['clips'].append({**{k: clip[k] for k in ('prompt', 'style', 'duration')}, 'references': refs})
        accepted = client.json('POST', '/muse/v1/groups', json.dumps(payload, ensure_ascii=False).encode())
        state['group_id'] = accepted['group_id']
        atomic_json(state_path, state)
        return accepted

def download(client, asset, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    name = asset['id'] + '.mp4'
    if not name.replace('.mp4', '').isalnum():
        raise ValueError('Invalid asset ID')
    final = output / name
    if final.exists() and hashlib.sha256(final.read_bytes()).hexdigest() == asset['sha256']:
        return str(final.resolve())
    for attempt in range(2):
        fd, temp = tempfile.mkstemp(prefix='.muse-', suffix='.part', dir=output)
        try:
            h = hashlib.sha256()
            size = 0
            with os.fdopen(fd, 'wb') as out, client.request('GET', '/muse/v1/assets/' + asset['id'] + '/content') as response:
                window_start = None
                window_size = 0
                slow_windows = 0
                watch_speed = (getattr(client, 'fallback_endpoint', None)
                               and client.active_endpoint == client.endpoint)
                while True:
                    # read1 returns available bytes rather than waiting to fill 64 KiB.
                    # Before the first byte, preserve the server's media-preparation wait.
                    chunk = getattr(response, 'read1', response.read)(65536)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > asset['size']:
                        raise ValueError('Unexpected download size')
                    h.update(chunk)
                    out.write(chunk)
                    if watch_speed:
                        now = time.monotonic()
                        if window_start is None:
                            window_start = now
                            # After delivery begins, a stalled socket is network trouble.
                            if getattr(response, 'fp', None):
                                response.fp.raw._sock.settimeout(client.slow_window_seconds)
                        else:
                            window_size += len(chunk)
                            elapsed = now - window_start
                            if elapsed >= client.slow_window_seconds:
                                slow_windows = slow_windows + 1 if window_size / elapsed < client.minimum_download_bytes_per_second else 0
                                if slow_windows >= 2:
                                    raise SlowRouteError('Primary download remained too slow')
                                window_start, window_size = now, 0
                out.flush()
                os.fsync(out.fileno())
            if size != asset['size'] or h.hexdigest() != asset['sha256']:
                raise ValueError('Incomplete download or hash mismatch; retry download, not generation')
            os.replace(temp, final)
            return str(final.resolve())
        except (OSError, ValueError, http.client.IncompleteRead):
            if attempt == 0 and hasattr(client, 'use_fallback') and client.use_fallback():
                continue  # Restart only the same asset; never submit another generation.
            raise
        finally:
            if os.path.exists(temp):
                os.unlink(temp)


def configured_client():
    endpoint = os.environ.get('YUHENG_ENDPOINT') or os.environ.get('MUSE_ENDPOINT')
    key_file = os.environ.get('YUHENG_API_KEY_FILE') or os.environ.get('MUSE_API_KEY_FILE')
    api_prefix = '/muse/v1'
    fallback_endpoint = os.environ.get('YUHENG_FALLBACK_ENDPOINT')
    route_state_file = None
    state_endpoint_aliases = ()
    media_timeout = os.environ.get('YUHENG_MEDIA_TIMEOUT_SECONDS') or os.environ.get('MUSE_MEDIA_TIMEOUT_SECONDS')
    if not endpoint or not key_file:
        config_path = Path.home() / '.config/yuheng-star/client.json'
        if not config_path.exists():
            config_path = Path.home() / '.config/muse-fleet/client.json'
        if not config_path.exists():
            raise ValueError('Video client is not configured; set YUHENG_ENDPOINT and YUHENG_API_KEY_FILE')
        if config_path.stat().st_mode & 0o077:
            raise ValueError('Client config must be private (0600)')
        config = json.loads(config_path.read_text())
        endpoint = endpoint or config.get('endpoint')
        key_file = key_file or config.get('api_key_file')
        api_prefix = config.get('api_prefix', '/muse/v1')
        media_timeout = media_timeout or config.get('media_timeout_seconds', 930)
        # An explicit endpoint override must not silently send its key to a saved fallback.
        if not (os.environ.get('YUHENG_ENDPOINT') or os.environ.get('MUSE_ENDPOINT')):
            fallback_endpoint = fallback_endpoint or config.get('fallback_endpoint')
            state_endpoint_aliases = config.get('state_endpoint_aliases', ())
        route_state_file = config_path.with_name('video-route.json')
    if not endpoint or not key_file:
        raise ValueError('Video client configuration is incomplete')
    try:
        media_timeout = int(media_timeout or 930)
    except (TypeError, ValueError):
        raise ValueError('Invalid media socket timeout') from None
    return Client(endpoint, key_file, api_prefix, media_timeout, fallback_endpoint, route_state_file, state_endpoint_aliases)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['submit', 'query', 'wait'])
    parser.add_argument('--state', required=True)
    parser.add_argument('--plan')
    parser.add_argument('--output', default='videos')
    parser.add_argument('--timeout', type=int, default=3600)
    args = parser.parse_args()
    client = configured_client()
    if args.action == 'submit':
        if not args.plan:
            parser.error('--plan is required')
        print(json.dumps(submit(client, args.plan, args.state), ensure_ascii=False))
        return
    state = json.loads(Path(args.state).read_text())
    if (state['endpoint'] != client.endpoint and state['endpoint'] not in getattr(client, 'state_endpoint_aliases', ())):
        raise ValueError('Endpoint does not match saved submission')
    if not state.get('group_id'):
        raise ValueError('Submission acknowledgement missing; run submit with the same plan/state to recover')
    deadline = time.monotonic() + args.timeout
    delay = 2
    while True:
        view = client.json('GET', '/muse/v1/groups/' + state['group_id'])
        if args.action == 'query':
            print(json.dumps(view, ensure_ascii=False))
            return
        for asset in view.get('assets', []):
            if asset['content_type'] == 'video/mp4' and asset['state'] == 'READY' and asset['expires_at'] > time.time():
                print(download(client, asset, args.output), flush=True)
        if view['group']['state'] in ('COMPLETED', 'FAILED', 'PARTIAL_SUCCESS', 'RECONCILING'):
            print(json.dumps({'group': view['group'], 'clips': view['clips']}, ensure_ascii=False))
            return
        if time.monotonic() >= deadline:
            raise RuntimeError('Wait timed out. Query the saved group later; no regeneration was requested.')
        time.sleep(delay)
        delay = min(20, delay * 2)

if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, KeyError, OSError) as err:
        print(str(err), file=sys.stderr)
        sys.exit(1)
