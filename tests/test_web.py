import contextlib
import io
import json
import os
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / 'claudeswap.py'


class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.g = runpy.run_path(str(SCRIPT))['main'].__globals__
        self.g['STATE_ROOT'] = str(self.root)
        self.autostart = []
        self.spawned = []
        self.killed = []
        self.alive = set()
        self.g['autostart_set'] = lambda on: self.autostart.append(on)
        self.g['web_pid_is_ours'] = lambda pid: pid in self.alive
        self.g['web_kill'] = lambda pid: self.killed.append(pid) or self.alive.discard(pid)

        def spawn():
            self.spawned.append(dict(self.state().get('env') or {}))
            pid = 1000 + len(self.spawned)
            self.alive.add(pid)
            st = self.state()
            st.update(pid=pid, version=self.g['__version__'])
            self.g['save_web_state'](st)
            return True
        self.g['web_spawn'] = spawn

    def state(self):
        p = self.root / 'web.json'
        return json.loads(p.read_text()) if p.exists() else {}

    def web(self, *args, env=None):
        out = io.StringIO()
        clean = {k: v for k, v in os.environ.items() if not k.startswith('CLSW_WEB_')}
        clean.update(env or {})
        with contextlib.redirect_stdout(out), mock.patch.dict(os.environ, clean, clear=True):
            self.g['cmd_web'](list(args))
        return out.getvalue()

    def test_default_is_off(self):
        self.assertIn('kapalı', self.web())
        self.g['web_ensure']()
        self.assertEqual(self.spawned, [])

    def test_on_persists_and_starts(self):
        out = self.web('on')
        self.assertIn('http://127.0.0.1:8790', out)
        self.assertTrue(self.state()['on'])
        self.assertEqual(self.autostart, [True])
        self.assertEqual(len(self.spawned), 1)
        self.web('on')                                   # zaten çalışıyor: ikinci kopya açılmaz
        self.assertEqual(len(self.spawned), 1)

    def test_off_stops_and_removes_autostart(self):
        self.web('on')
        pid = self.state()['pid']
        self.web('off')
        st = self.state()
        self.assertFalse(st['on'])
        self.assertNotIn('pid', st)
        self.assertEqual(self.killed, [pid])
        self.assertEqual(self.autostart, [True, False])
        self.g['web_ensure']()                           # kapalıyken hiçbir komut yeniden açmaz
        self.assertEqual(len(self.spawned), 1)

    def test_ensure_restarts_after_reboot(self):
        self.web('on')
        self.alive.clear()                               # makine yeniden açıldı, süreç yok
        self.g['web_ensure']()
        self.assertEqual(len(self.spawned), 2)

    def test_ensure_replaces_older_version(self):
        self.web('on')
        st = self.state()
        st['version'] = '0.9.0'
        self.g['save_web_state'](st)
        self.g['web_ensure']()
        self.assertEqual(len(self.spawned), 2)
        self.assertEqual(len(self.killed), 1)

    def test_on_saves_env_and_plain_on_resets_to_local(self):
        out = self.web('on', env={'CLSW_WEB_HOST': '0.0.0.0', 'CLSW_WEB_PORT': '9000'})
        self.assertIn('UYARI', out)
        self.assertEqual(self.spawned[-1], {'CLSW_WEB_HOST': '0.0.0.0', 'CLSW_WEB_PORT': '9000'})
        self.web('on')
        self.assertEqual(self.spawned[-1], {})
        self.assertEqual(len(self.killed), 1)

    def test_auto_run_does_nothing_when_off(self):
        with mock.patch.dict('sys.modules', {'cost_web': None}):
            self.g['cmd_web_run'](auto=True)             # cost_web yüklenmeye çalışılsa ImportError olurdu
        self.web('on')
        with mock.patch.dict('sys.modules', {'cost_web': None}):
            self.g['cmd_web_run'](auto=True)             # zaten çalışıyor: ikinci sunucu açılmaz

    def test_bad_subcommand(self):
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(io.StringIO()):
            self.web('nope')
        self.assertEqual(cm.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
