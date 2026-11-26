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


class Tty(io.StringIO):
    def isatty(self):
        return True


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.g = runpy.run_path(str(SCRIPT))['main'].__globals__
        self.g['STATE_ROOT'] = str(self.root)
        self.g['UPDATE_STATE'] = str(self.root / 'update.json')
        self.g['FROZEN'] = True
        self.g['install_method'] = lambda: None          # testi çalıştıran python brew'dan olabilir
        self.g['__version__'] = '1.0.0'
        self.installed = []
        self.g['do_install'] = lambda rel: self.installed.append(rel['version']) or 'yeni'
        self.g['rerun'] = lambda target, argv: (_ for _ in ()).throw(SystemExit(0))
        self.rel = {'version': (1, 1, 0), 'notes': '- yeni özellik', 'asset': {'url': 'x'}, 'token': 't'}
        self.g['latest_release'] = lambda timeout=5: (self.rel, None)

    def offer(self, answer, tty=True):
        out = Tty() if tty else io.StringIO()
        stdin = Tty(answer + '\n') if tty else io.StringIO(answer + '\n')
        with mock.patch('sys.stdin', stdin), mock.patch('sys.stdout', out), mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('CLSW_NO_UPDATE', None)
            try:
                self.g['maybe_offer_update'](['clsw', 'list'])
            except SystemExit:
                pass
        return out.getvalue()

    def test_parse_version(self):
        pv = self.g['parse_version']
        self.assertEqual(pv('v1.2.3'), (1, 2, 3))
        self.assertEqual(pv('1.10.0'), (1, 10, 0))
        self.assertGreater(pv('v1.10.0'), pv('v1.9.9'))
        self.assertIsNone(pv('v1.2'))
        self.assertIsNone(pv('latest'))

    def test_asset_name_by_platform(self):
        name = self.g['asset_name']
        with mock.patch('sys.platform', 'win32'), mock.patch('platform.machine', return_value='AMD64'):
            self.assertEqual(name(), 'clsw-windows-x64.zip')
        with mock.patch('sys.platform', 'darwin'), mock.patch('platform.machine', return_value='arm64'):
            self.assertEqual(name(), 'clsw-macos-arm64.tar.gz')
        with mock.patch('sys.platform', 'linux'), mock.patch('platform.machine', return_value='x86_64'):
            self.assertEqual(name(), 'clsw-linux-x64.tar.gz')

    def test_yes_installs_and_reruns(self):
        out = self.offer('e')
        self.assertIn('v1.0.0  →  v1.1.0', out)
        self.assertIn('yeni özellik', out)
        self.assertEqual(self.installed, [(1, 1, 0)])

    def test_enter_means_yes(self):
        self.offer('')
        self.assertEqual(self.installed, [(1, 1, 0)])

    def test_no_snoozes_that_version(self):
        out = self.offer('h')
        self.assertIn('Atlandı', out)
        self.assertEqual(self.installed, [])
        st = json.loads((self.root / 'update.json').read_text(encoding='utf-8'))
        self.assertIn('v1.1.0', st['snoozed'])
        st['checked'] = 0                                   # aralık geçmiş olsa da aynı sürüm sorulmaz
        (self.root / 'update.json').write_text(json.dumps(st), encoding='utf-8')
        self.assertEqual(self.offer('e'), '')
        self.rel = dict(self.rel, version=(1, 2, 0))        # daha yeni sürüm gelince yine sorulur
        st['checked'] = 0
        (self.root / 'update.json').write_text(json.dumps(st), encoding='utf-8')
        self.assertIn('v1.2.0', self.offer('h'))

    def test_silent_when_not_interactive(self):
        self.assertEqual(self.offer('e', tty=False), '')
        self.assertEqual(self.installed, [])

    def test_silent_when_current_or_running_from_source(self):
        self.rel = dict(self.rel, version=(1, 0, 0))
        self.assertEqual(self.offer('e'), '')
        self.rel = dict(self.rel, version=(2, 0, 0))
        self.g['FROZEN'] = False
        (self.root / 'update.json').unlink(missing_ok=True)
        self.assertEqual(self.offer('e'), '')
        self.assertEqual(self.installed, [])

    def test_no_network_is_silent(self):
        self.g['latest_release'] = lambda timeout=5: (None, 'GitHub erişimi yok')
        self.assertEqual(self.offer('e'), '')

    def test_check_throttled(self):
        calls = []
        self.g['latest_release'] = lambda timeout=5: calls.append(1) or (None, 'yok')
        self.offer('e')
        self.offer('e')
        self.assertEqual(len(calls), 1)

    def test_install_method_detection(self):
        g = runpy.run_path(str(SCRIPT))['main'].__globals__
        g['FROZEN'] = True
        im = g['install_method']
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('CLSW_INSTALLER', None)
            for exe, want in (('/opt/homebrew/Cellar/clsw/1.1.0/bin/clsw', 'brew'),
                              ('/home/linuxbrew/.linuxbrew/Cellar/clsw/1.1.0/bin/clsw', 'brew'),
                              ('/usr/lib/node_modules/@orjinalp/clsw/vendor/clsw-linux-x64', 'npm'),
                              ('C:\\Users\\kullanici\\AppData\\Roaming\\npm\\node_modules\\@orjinalp\\clsw\\vendor\\clsw.exe', 'npm'),
                              ('/usr/local/bin/clsw', None)):
                with mock.patch('os.path.realpath', return_value=exe), mock.patch('sys.executable', exe):
                    self.assertEqual(im(), want, exe)
            os.environ['CLSW_INSTALLER'] = 'npm'
            with mock.patch('sys.executable', '/x/clsw'):
                self.assertEqual(im(), 'npm')

    def test_managed_install_asks_and_uses_package_manager(self):
        self.g['install_method'] = lambda: 'brew'
        calls = []
        self.g['managed_update'] = lambda via, argv: calls.append(via) or (_ for _ in ()).throw(SystemExit(0))
        out = self.offer('e')
        self.assertIn('Şimdi güncelleyeyim mi?', out)
        self.assertIn('brew upgrade clsw', out)
        self.assertEqual(calls, ['brew'])
        self.assertEqual(self.installed, [])                 # kendi dosyasını değiştirmez

    def test_npm_update_hands_over_to_launcher(self):
        g = runpy.run_path(str(SCRIPT))['main'].__globals__
        with mock.patch.dict(os.environ, {'CLSW_LAUNCHER_UPDATE': '1'}), self.assertRaises(SystemExit) as cm:
            g['managed_update']('npm', ['clsw', 'list'])
        self.assertEqual(cm.exception.code, g['LAUNCHER_UPDATE_EXIT'])

    def test_update_command_uses_package_manager(self):
        self.g['install_method'] = lambda: 'npm'
        calls = []
        self.g['managed_update'] = lambda via, argv: calls.append(via) or (_ for _ in ()).throw(SystemExit(0))
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            self.g['cmd_update']([])
        self.assertEqual(calls, ['npm'])
        self.assertEqual(self.installed, [])

    def test_latest_release_works_without_token(self):
        g = runpy.run_path(str(SCRIPT))['main'].__globals__
        body = json.dumps({'tag_name': 'v9.0.0', 'body': 'n', 'assets': [
            {'name': g['asset_name'](), 'browser_download_url': 'https://example.com/x', 'size': 1, 'digest': 'sha256:' + '0' * 64}]}).encode()
        seen = []

        def fake_urlopen(req, timeout=None):
            seen.append(req.get_header('Authorization'))
            return contextlib.closing(io.BytesIO(body))
        g['urllib'].request.urlopen, orig = fake_urlopen, g['urllib'].request.urlopen
        try:
            rel, err = g['latest_release']()
        finally:
            g['urllib'].request.urlopen = orig
        self.assertIsNone(err)
        self.assertEqual(rel['version'], (9, 0, 0))
        self.assertEqual(seen, [None])

    def layout_g(self):
        g = runpy.run_path(str(SCRIPT))['main'].__globals__
        g['HOME'] = str(self.root / 'home')
        g['FROZEN'] = True
        g['install_method'] = lambda: None
        return g

    def make_bundle(self, where, marker):
        exe = 'clsw.exe' if os.name == 'nt' else 'clsw'
        d = Path(where) / 'clsw'
        (d / '_internal').mkdir(parents=True)
        (d / exe).write_text(marker)
        (d / '_internal' / 'lib.txt').write_text(marker)
        return d

    def make_archive(self, marker):
        import shutil
        src = self.root / ('src-' + marker)
        self.make_bundle(src, marker)
        fmt = 'zip' if os.name == 'nt' else 'gztar'
        return shutil.make_archive(str(self.root / ('arc-' + marker)), fmt, str(src), 'clsw')

    def test_install_from_archive_switches_current(self):
        g = self.layout_g()
        exe = 'clsw.exe' if os.name == 'nt' else 'clsw'
        g['bundle_dir'] = lambda: str(self.make_bundle(self.root / 'indirilen', '1.0.0'))
        g['__version__'] = '1.0.0'
        with contextlib.redirect_stdout(io.StringIO()):
            g['cmd_install']()
        cur = Path(g['app_root']()) / 'current'
        self.assertEqual((cur / exe).read_text(), '1.0.0')
        if os.name != 'nt':
            self.assertEqual((Path(g['bin_dir']()) / 'clsw').read_text(), '1.0.0')
            self.assertEqual((Path(g['bin_dir']()) / 'claudeswap').read_text(), '1.0.0')
        for ver in ('1.1.0', '1.2.0', '1.3.0'):
            arc = self.make_archive(ver)
            g['download_asset'] = lambda rel, dest, arc=arc: __import__('shutil').copyfile(arc, dest)
            rel = {'version': g['parse_version'](ver), 'asset': {'name': os.path.basename(arc)}}
            target = g['install_release'](rel)
            self.assertEqual(Path(target).read_text(), ver)
            self.assertEqual((cur / '_internal' / 'lib.txt').read_text(), ver)
        g['bundle_dir'] = lambda: str(Path(g['app_root']()) / 'versions' / '1.3.0' / 'clsw')
        g['cleanup_versions']()
        left = sorted(os.listdir(Path(g['app_root']()) / 'versions'))
        self.assertEqual(left, ['1.2.0', '1.3.0'])                              # en yeni iki sürüm kalır

    def test_archive_with_escaping_path_is_rejected(self):
        import tarfile
        g = self.layout_g()
        bad = self.root / 'kotu.tar.gz'
        (self.root / 'x.txt').write_text('x')
        with tarfile.open(bad, 'w:gz') as t:
            t.add(self.root / 'x.txt', arcname='../disari.txt')
        with self.assertRaises(RuntimeError):
            g['extract_archive'](str(bad), str(self.root / 'acilan'))
        self.assertFalse((self.root / 'disari.txt').exists())

    def test_busy_check_never_signals_processes(self):
        # Windows'ta os.kill(pid, 0) süreci öldürür; canlılık kontrolü sinyal göndermemeli.
        import subprocess, sys
        g = runpy.run_path(str(SCRIPT))['main'].__globals__
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        self.addCleanup(child.kill)
        d = self.root / '.claude' / 'sessions'
        d.mkdir(parents=True)
        (d / f'{child.pid}.json').write_text(json.dumps({'status': 'busy', 'sessionId': 'S1'}), encoding='utf-8')
        (d / '999999.json').write_text(json.dumps({'status': 'busy', 'sessionId': 'S2'}), encoding='utf-8')
        g['SLOTS'] = [{'name': 'claude', 'dir': str(self.root / '.claude')}]
        if os.name == 'nt':
            with mock.patch('os.kill', side_effect=AssertionError('os.kill çağrıldı')):
                ids = g['busy_session_ids']()
        else:
            ids = g['busy_session_ids']()
        self.assertEqual(ids, {'S1'})
        self.assertIsNone(child.poll(), 'kontrol edilen süreç kapanmış')

    def test_prompt_comes_after_command_output(self):
        out = Tty()
        self.g['dispatch'] = lambda argv: print('TABLO') or []
        self.g['activate'] = lambda changed: None
        with mock.patch('sys.stdin', Tty('h\n')), mock.patch('sys.stdout', out):
            os.environ.pop('CLSW_NO_UPDATE', None)
            self.g['main'](['clsw', 'list'])
        text = out.getvalue()
        self.assertLess(text.index('TABLO'), text.index('v1.0.0  →  v1.1.0'))
        self.assertEqual(self.installed, [])

    def test_slow_network_skips_prompt(self):
        import threading
        gate = threading.Event()
        self.addCleanup(gate.set)
        self.g['latest_release'] = lambda timeout=5: (gate.wait(5), (self.rel, None))[1]
        out = Tty()
        with mock.patch('sys.stdin', Tty('e\n')), mock.patch('sys.stdout', out):
            os.environ.pop('CLSW_NO_UPDATE', None)
            self.g['finish_update_check'](self.g['start_update_check'](), wait=0.05)
        self.assertEqual(out.getvalue(), '')
        self.assertEqual(self.installed, [])

    def test_npm_update_only_code_for_new_launcher(self):
        g = runpy.run_path(str(SCRIPT))['main'].__globals__
        with mock.patch.dict(os.environ, {'CLSW_LAUNCHER_UPDATE': '1', 'CLSW_LAUNCHER': '2'}), \
                self.assertRaises(SystemExit) as cm:
            g['managed_update']('npm', None)
        self.assertEqual(cm.exception.code, g['LAUNCHER_UPDATE_ONLY_EXIT'])
        with mock.patch.dict(os.environ, {'CLSW_LAUNCHER_UPDATE': '1'}), self.assertRaises(SystemExit) as cm:
            os.environ.pop('CLSW_LAUNCHER', None)
            g['managed_update']('npm', None)                                  # eski başlatıcı: eski kod
        self.assertEqual(cm.exception.code, g['LAUNCHER_UPDATE_EXIT'])

    def test_version_and_update_commands_skip_account_lookup(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.g['main'](['clsw', 'version'])
        self.assertIn('clsw v1.0.0', buf.getvalue())
        self.assertNotIn('GitHub', buf.getvalue())

    def test_version_mentions_newer_release_seen_last_check(self):
        self.offer('h')
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.g['cmd_version']()
        self.assertIn("GitHub'da v1.1.0 var", buf.getvalue())


if __name__ == '__main__':
    unittest.main()
