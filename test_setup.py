"""Private installer regressions. All credentials and paths are isolated fixtures."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SOURCE = Path(__file__).resolve().parent
KEY = 'fixture-only-key-never-real'

class Handler(BaseHTTPRequestHandler):
    status = 404
    payload = {'error': {'code': 'NOT_FOUND'}}
    requests = []
    def log_message(self, *args):
        pass
    def do_GET(self):
        type(self).requests.append((self.command, self.path, self.headers.get('Authorization')))
        self.send_response(type(self).status)
        self.end_headers()
        self.wfile.write(json.dumps(type(self).payload).encode())
    def do_POST(self):
        raise AssertionError('Installer must never POST')

class SetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name).resolve()
        Handler.status, Handler.payload, Handler.requests = 404, {'error': {'code': 'NOT_FOUND'}}, []
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.endpoint = f'http://127.0.0.1:{self.server.server_port}'
        self.config = self.home / '.config/yuheng-star/client.json'
        self.skill = self.home / '.codex/skills/yuheng-video-gen'
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(('YUHENG_', 'MUSE_')) and not k.lower().endswith('_proxy')}
        self.env['HOME'] = str(self.home)
        self.env['NO_PROXY'] = '*'
    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.tmp.cleanup()
    def run_setup(self, *args, key=KEY):
        r = subprocess.run([sys.executable, str(SOURCE / 'setup.py'), '--endpoint', self.endpoint,
                            '--api-prefix', '/video/v1', *args], input=key+'\n', text=True,
                           env=self.env, capture_output=True)
        self.assertNotIn(key, r.stdout+r.stderr)
        return r
    def test_installs_private_config_and_readonly_auth(self):
        r = self.run_setup(); self.assertEqual(r.returncode, 0, r.stderr)
        config = json.loads(self.config.read_text()); key = Path(config['api_key_file'])
        self.assertTrue(key.is_absolute()); self.assertEqual(key.read_text().strip(), KEY)
        for p in (key, self.config): self.assertEqual(p.stat().st_mode & 0o777, 0o600)
        for p in (self.config.parent, key.parent): self.assertEqual(p.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.skill / 'yuheng_client.py').read_bytes(), (SOURCE / 'yuheng_client.py').read_bytes())
        self.assertTrue(Handler.requests)
        self.assertTrue(all(method == 'GET' and path == '/video/v1/groups/installation-check' and auth == 'Bearer '+KEY
                            for method,path,auth in Handler.requests))
    def test_401_does_not_install(self):
        Handler.status, Handler.payload = 401, {'error': {'code':'UNAUTHORIZED'}}
        self.assertNotEqual(self.run_setup().returncode, 0); self.assertFalse(self.config.exists())
    def test_unknown_404_does_not_install(self):
        Handler.payload = {'html': 'not found'}
        self.assertNotEqual(self.run_setup().returncode, 0); self.assertFalse(self.config.exists())
    def test_identity_change_requires_replace_preserves_state(self):
        self.assertEqual(self.run_setup().returncode, 0)
        old = self.config.read_bytes(); old_key = Path(json.loads(old)['api_key_file'])
        (self.skill / 'saved-state.json').write_text('historical state')
        self.assertNotEqual(self.run_setup(key='different-fixture-key').returncode, 0)
        self.assertEqual(self.config.read_bytes(), old)
        r = self.run_setup('--replace', key='different-fixture-key'); self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(old_key.read_text().strip(), KEY)
        self.assertEqual((self.skill / 'saved-state.json').read_text(), 'historical state')
        self.assertTrue(list(self.config.parent.glob('client.json.backup-*')))
    def test_config_symlink_and_hardlink_rejected(self):
        self.config.parent.mkdir(parents=True); other=self.home/'other'; other.write_text('{}')
        self.config.symlink_to(other)
        self.assertNotEqual(self.run_setup('--replace').returncode, 0); self.assertEqual(other.read_text(), '{}')
        self.config.unlink(); os.link(other,self.config)
        self.assertNotEqual(self.run_setup('--replace').returncode, 0); self.assertEqual(other.read_text(), '{}')
    def test_parent_symlink_rejected(self):
        other=self.home/'outside'; other.mkdir(); (self.home/'.config').symlink_to(other)
        self.assertNotEqual(self.run_setup().returncode, 0); self.assertEqual(list(other.iterdir()), [])
    def test_old_skill_symlink_is_backed_up_not_followed(self):
        other=self.home/'outside'; other.mkdir(); (other/'keep').write_text('keep')
        self.skill.parent.mkdir(parents=True); self.skill.symlink_to(other)
        r=self.run_setup('--replace'); self.assertEqual(r.returncode,0,r.stderr)
        self.assertFalse(self.skill.is_symlink()); self.assertEqual((other/'keep').read_text(),'keep')
        self.assertTrue(any(p.is_symlink() for p in self.skill.parent.glob('yuheng-video-gen.backup-*')))
    def test_environment_override_rejected(self):
        for var in ('YUHENG_ENDPOINT','MUSE_ENDPOINT','YUHENG_API_KEY_FILE','MUSE_API_KEY_FILE','YUHENG_FALLBACK_ENDPOINT'):
            with self.subTest(var=var):
                self.env[var]='conflicting-override'; r=self.run_setup('--replace')
                self.assertNotEqual(r.returncode,0); self.assertIn(var,r.stderr); self.env.pop(var)
    def test_unreachable_primary_uses_explicit_fallback(self):
        self.endpoint='http://127.0.0.1:1'
        fallback=f'http://127.0.0.1:{self.server.server_port}'
        r=self.run_setup('--fallback-endpoint',fallback); self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(json.loads(self.config.read_text())['fallback_endpoint'],fallback)
    def test_unreachable_endpoint_does_not_install(self):
        self.endpoint='http://127.0.0.1:1'
        self.assertNotEqual(self.run_setup().returncode,0); self.assertFalse(self.config.exists())
    def test_no_redirect_following_or_unauthenticated_fallback(self):
        Handler.status,Handler.payload=302,{'error':{'code':'NOT_FOUND'}}
        r=self.run_setup(); self.assertNotEqual(r.returncode,0); self.assertEqual(len(Handler.requests),1)
    def test_old_aliases_timeout_and_legacy_config_preserved(self):
        self.assertEqual(self.run_setup('--state-endpoint-alias','http://127.0.0.1:28172').returncode,0)
        old=json.loads(self.config.read_text()); old['media_timeout_seconds']=1000
        self.config.write_text(json.dumps(old)); self.config.chmod(0o600)
        legacy=self.home/'.config/muse-fleet/client.json'; legacy.parent.mkdir()
        self.config.rename(legacy)
        r=self.run_setup(); self.assertEqual(r.returncode,0,r.stderr)
        conf=json.loads(self.config.read_text())
        self.assertEqual(conf['state_endpoint_aliases'],['http://127.0.0.1:28172'])
        self.assertEqual(conf['media_timeout_seconds'],1000); self.assertTrue(legacy.exists())
    def test_symlinked_or_hardlinked_state_fails_before_changes(self):
        self.assertEqual(self.run_setup().returncode,0)
        old=self.config.read_bytes(); other=self.home/'outside-state'; other.write_text('state')
        dest=self.skill/'old-state'; dest.symlink_to(other)
        self.assertNotEqual(self.run_setup('--replace').returncode,0); self.assertEqual(self.config.read_bytes(),old)
        dest.unlink(); os.link(other,dest)
        self.assertNotEqual(self.run_setup('--replace').returncode,0); self.assertEqual(other.read_text(),'state')
    def test_custom_agent_dir_and_preserved_nested_state(self):
        dest=self.home/'.agents/skills/yuheng-video-gen'
        self.assertEqual(self.run_setup('--skill-dir',str(dest)).returncode,0)
        (dest/'tasks').mkdir(); (dest/'tasks/state.json').write_text('saved')
        self.assertEqual(self.run_setup('--skill-dir',str(dest),'--replace').returncode,0)
        self.assertEqual((dest/'tasks/state.json').read_text(),'saved')
    def test_dangerous_overlapping_skill_directory_rejected(self):
        for dest in (self.home,self.home/'.config',self.config.parent,self.config.parent/'nested',SOURCE):
            with self.subTest(dest=dest):
                self.assertNotEqual(self.run_setup('--skill-dir',str(dest),'--replace').returncode,0)
        self.assertFalse(self.config.exists())

    def test_staging_failure_removes_lock_and_preserves_retry(self):
        self.assertEqual(self.run_setup().returncode,0)
        old_config=self.config.read_bytes()
        (self.skill/'state.json').write_text('original state')
        before={str(p.relative_to(self.skill)):p.read_bytes() for p in self.skill.rglob('*') if p.is_file()}
        code = "import runpy,sys; from unittest import mock; sys.argv=sys.argv[1:]; "
        code += "patch=mock.patch('tempfile.mkdtemp',side_effect=OSError('fixture disk full')); patch.start(); runpy.run_path(sys.argv[0],run_name='__main__')"
        r=subprocess.run([sys.executable,'-c',code,str(SOURCE/'setup.py'),'--endpoint',self.endpoint,
                          '--api-prefix','/video/v1','--replace'],input=KEY+'\n',env=self.env,capture_output=True,text=True)
        self.assertNotEqual(r.returncode,0); self.assertNotIn(KEY,r.stdout+r.stderr)
        self.assertFalse((self.config.parent/'.setup.lock').exists())
        self.assertEqual(self.config.read_bytes(),old_config)
        self.assertEqual({str(p.relative_to(self.skill)):p.read_bytes() for p in self.skill.rglob('*') if p.is_file()},before)
        self.assertEqual(self.run_setup('--replace').returncode,0)
    def test_route_change_does_not_silently_inherit_aliases(self):
        alias='http://127.0.0.1:28172'
        self.assertEqual(self.run_setup('--state-endpoint-alias',alias).returncode,0)
        old_config=self.config.read_bytes()
        self.endpoint=f'http://localhost:{self.server.server_port}'
        r=self.run_setup('--replace'); self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(json.loads(self.config.read_text())['state_endpoint_aliases'],[])
        self.assertTrue(any(p.read_bytes()==old_config for p in self.config.parent.glob('client.json.backup-*')))
        r=self.run_setup('--replace','--state-endpoint-alias',alias); self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(json.loads(self.config.read_text())['state_endpoint_aliases'],[alias])

    def test_help_needs_no_key_or_network(self):
        r=subprocess.run([sys.executable,str(SOURCE/'setup.py'),'--help'],env=self.env,capture_output=True,text=True)
        self.assertEqual(r.returncode,0); self.assertEqual(Handler.requests,[])

if __name__ == '__main__': unittest.main()
