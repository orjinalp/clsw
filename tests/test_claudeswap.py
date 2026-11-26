import contextlib
import io
import json
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'claudeswap.py'
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # cost_web


def load(root):
    m = runpy.run_path(str(SCRIPT))
    g = m['main'].__globals__
    g['HOME'] = str(root)
    g['STATE_ROOT'] = str(root / '.claudeswap')
    g['BACKUP_ROOT'] = str(root / '.claudeswap/backups')
    g['SLOTS'] = [m['_slot']('claude' if i == 1 else f'claude{i}',
        str(root / ('.claude' if i == 1 else f'.claude{i}')),
        str(root / '.claude.json' if i == 1 else root / f'.claude{i}/.claude.json'))
        for i in range(1, 11)]
    g['running_sessions'] = lambda: []
    g['activate'] = lambda slots: None  # gerçek API/claude çağrısı yapma
    return g


class SwapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.g = load(self.root)
        self.slots = self.g['SLOTS']
        self.seed(0, 'one@example.test')

    def seed(self, index, email):
        s = self.slots[index]
        Path(s['dir']).mkdir(exist_ok=True)
        Path(s['json']).write_text(json.dumps({'oauthAccount': {'emailAddress': email}, 'theme': str(index)}))
        Path(s['creds']).write_text(json.dumps({'claudeAiOauth': {'accessToken': email}}))

    def call(self, *args):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.g['main'](['claudeswap', *args])

    def snapshot(self):
        return {(s['name'], k): self.g['read_bytes'](s[k]) for s in self.slots for k in ('json', 'creds')}

    def test_all_slots_and_roundtrip(self):
        for slot in self.slots[1:]:
            token = Path(self.slots[0]['creds']).read_bytes()
            self.call(slot['name'])
            self.assertFalse(Path(self.slots[0]['creds']).exists())
            self.assertEqual(Path(slot['creds']).read_bytes(), token)
            self.assertEqual(json.loads(Path(self.slots[0]['json']).read_text()), {'theme': '0'})
            self.call('one')
            self.assertEqual(Path(self.slots[0]['creds']).read_bytes(), token)

    def test_empty_idempotent(self):
        self.call('empty')
        before = self.snapshot()
        self.call('empty')
        self.assertEqual(before, self.snapshot())
        self.assertTrue(Path(self.slots[1]['creds']).exists())

    def test_full_and_ambiguous(self):
        for i in range(1, 10): self.seed(i, 'one@other.test')
        before = self.snapshot()
        for args in [('empty',), ('one',), ('claude11',), ('claude2', 'invalid'), ('a','b','c')]:
            with self.assertRaises(SystemExit): self.call(*args)
            self.assertEqual(before, self.snapshot())

    def test_bad_empty_candidate(self):
        slot = self.slots[1]
        Path(slot['dir']).mkdir()
        for bad in [b'{broken', b'[]', b'null', b'\xff']:
            Path(slot['json']).write_bytes(bad)
            before = self.snapshot()
            with self.assertRaises(SystemExit): self.call('empty')
            self.assertEqual(before, self.snapshot())

    def test_invalid_credentials_rejected(self):
        for bad in [b'broken', b'[]', b'{"claudeAiOauth": "wrong"}']:
            Path(self.slots[0]['creds']).write_bytes(bad)
            before = self.snapshot()
            with self.assertRaises(SystemExit): self.call('claude3')
            self.assertEqual(before, self.snapshot())

    def test_each_write_failure_restores(self):
        original = self.g['atomic_write']
        for failure_at in range(1, 5):
            calls = [0]
            def fail(path, data, private=False):
                calls[0] += 1
                if calls[0] == failure_at: raise OSError('test')
                original(path, data)
            self.g['atomic_write'] = fail
            before = self.snapshot()
            with self.assertRaises(SystemExit): self.call('claude3')
            self.assertEqual(before, self.snapshot())
        self.g['atomic_write'] = original

    def test_rollback_failure_reports_backup_and_attempts_all_files(self):
        calls = []
        def fail(path, data, private=False):
            calls.append(path)
            raise OSError('test')
        self.g['atomic_write'] = fail
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            self.g['main'](['claudeswap', 'claude3'])
        self.assertEqual(len(calls), 5)
        self.assertIn('Kurtarma yedeği:', err.getvalue())
        self.assertTrue(list((self.root / '.claudeswap/backups').glob('*/claude.credentials.json')))

    def test_concurrent_opposite_swaps(self):
        before = Path(self.slots[0]['creds']).read_bytes()
        bootstrap = "import runpy,sys; m=runpy.run_path(sys.argv[1]); g=m['load'](m['Path'](sys.argv[2])); g['main'](['claudeswap',*sys.argv[3:]])"
        procs = [subprocess.Popen([sys.executable, '-c', bootstrap, __file__, str(self.root),
                  *(['claude','claude3'] if i % 2 else ['claude3','claude'])],
                  stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(12)]
        for p in procs:
            _, err = p.communicate(timeout=30)
            self.assertEqual(p.returncode, 0, err)
        self.assertEqual(Path(self.slots[0]['creds']).read_bytes(), before)
        self.assertFalse(Path(self.slots[2]['creds']).exists())


class ExpiryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.g = load(Path(self.tmp.name))
        self.g['USE_COLOR'] = False
        for name in ('bold', 'dim', 'red', 'green', 'yellow'):
            self.g[name] = lambda s: s

    def test_list_columns(self):
        self.g['fetch_live'] = lambda i: {**i, 'status': 'test'}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.g['cmd_list']()
        self.assertNotIn('TOKEN BİTİŞ', out.getvalue())   # kullanıcı isteğiyle kaldırıldı
        self.assertIn('RESET', out.getvalue())

    def test_list_hides_sessions_process_shows_them(self):
        called = []
        self.g['fetch_live'] = lambda i: {**i, 'status': 'test'}
        self.g['running_sessions'] = lambda: called.append(1) or []
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.g['dispatch'](['clsw', 'list'])
        self.assertEqual(called, [])
        self.assertNotIn('Açık oturumlar\n', out.getvalue())
        self.assertIn('clsw process', out.getvalue())
        for cmd in ('process', 'proc', 'oturum', 'ps'):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.g['dispatch'](['clsw', cmd])
            self.assertIn('Açık oturumlar', out.getvalue(), cmd)
            self.assertNotIn('Hesaplar', out.getvalue(), cmd)

    def test_resets_cell(self):
        from datetime import datetime, timezone
        cell = self.g['resets_cell']
        now = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
        grant = lambda **kw: {'resets_left': 1, 'ends_at': '2026-12-10T09:00:00+00:00',
                              'clears': ['five_hour', 'seven_day'], 'type': 'full', **kw}
        u = lambda *gs: {'cedar_ember': {'eligible': True, 'grants': list(gs)}}
        fable = grant(id='f', gives_back_weekly=True, ends_at='2027-03-09T07:59:00+00:00')
        self.assertEqual(cell(u(fable, grant(id='a')), now), '1 tam 10 Ara\n1 Fable 9 Mar')
        # aynı türler toplanır, en yakın bitiş gösterilir
        self.assertEqual(cell(u(grant(), grant(resets_left=2, ends_at='2026-11-30T09:00:00+00:00')), now),
                         '3 tam 30 Kas')
        # type yoksa clears'tan türetilir
        self.assertEqual(cell(u(grant(type=None, clears=['five_hour'])), now), '1 5 saat 10 Ara')
        self.assertEqual(cell(u(grant(type=None, clears=['seven_day'])), now), '1 Hafta 10 Ara')
        # kullanılmış, duraklatılmış, süresi geçmiş ve bozuk kayıtlar sayılmaz
        for bad in (grant(resets_left=0), grant(paused=True), grant(ends_at='2026-11-01T00:00:00+00:00'),
                    grant(resets_left=True), grant(resets_left='1'), 'x'):
            self.assertEqual(cell(u(bad), now), '-', bad)
        self.assertEqual(cell(u(grant(ends_at=None)), now), '1 tam')
        for missing in (None, {}, {'cedar_ember': None}, {'cedar_ember': {'eligible': False, 'grants': []}}):
            self.assertEqual(cell(missing, now), '-')

    def test_print_table_multiline(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.g['print_table'](['A', 'RESET', 'B'], [['x', '1 tam\n1 Fable (9 Mar)', 'ok'], ['yy', '-', 'z']])
        self.assertEqual(out.getvalue().splitlines(), [
            '  A   RESET            B',
            '  x   1 tam            ok',
            '      1 Fable (9 Mar)',
            '  yy  -                z',
        ])

    def test_cli_user_agent(self):
        ua = self.g['cli_user_agent']()   # test HOME'unda claude yok → sabit sürüm
        self.assertRegex(ua, r'^claude-cli/\d+\.\d+\.\d+ \(external, cli\)$')
        self.assertIn('cedar_ember=1', self.g['USAGE_PATH'])


class ActivateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.g = load(self.root)
        self.slots = self.g['SLOTS']

    def fake_live(self, **extra):
        self.g['fetch_live'] = lambda i: {**i, **extra}

    def info(self, token='t'):
        return {'slot': self.slots[1], 'exists': True, 'oauth': {'accessToken': token} if token else {}, 'email': 'x@y'}

    def test_needs_activation(self):
        na = self.g['needs_activation']
        self.g['slot_info'] = lambda s: self.info()
        cases = [
            ({'usage': {'five_hour': {'utilization': 0.0, 'resets_at': None}}}, True),
            ({'usage': {'five_hour': None}}, True),
            ({'usage': {}}, True),
            ({'usage': {'five_hour': {'utilization': 4.0, 'resets_at': '2026-11-18T18:20:00+00:00'}}}, False),
            ({'status': 'token süresi dolmuş', 'stale': True}, True),
            ({'status': 'token geçersiz'}, False),
            ({'status': 'limit bilgisi alınamadı (URLError)'}, False),
        ]
        for extra, want in cases:
            self.fake_live(**extra)
            self.assertEqual(bool(na(self.slots[1])), want, extra)
        self.g['slot_info'] = lambda s: self.info(token=None)
        self.fake_live(usage={'five_hour': None})
        self.assertIsNone(na(self.slots[1]))

    def test_claude_command_env(self):
        import os
        os.environ['CLAUDE_CONFIG_DIR'] = '/elsewhere'
        os.environ['CLAUDE_CODE_OAUTH_TOKEN'] = 'leak'
        try:
            cmd, env = self.g['claude_command'](self.slots[0])
            self.assertNotIn('CLAUDE_CONFIG_DIR', env)
            self.assertNotIn('CLAUDE_CODE_OAUTH_TOKEN', env)
            cmd, env = self.g['claude_command'](self.slots[2])
            self.assertEqual(env['CLAUDE_CONFIG_DIR'], self.slots[2]['dir'])
            self.assertIn('-p', cmd)
            self.assertEqual(cmd[cmd.index('-p') + 1], 'sadece ok yaz')
        finally:
            del os.environ['CLAUDE_CONFIG_DIR'], os.environ['CLAUDE_CODE_OAUTH_TOKEN']

    def test_activate_only_needed_slots(self):
        m = runpy.run_path(str(SCRIPT))
        g = m['activate'].__globals__
        sent = []
        g['needs_activation'] = lambda s: {'slot': s, 'email': s['name']} if s['name'] == 'claude3' else None
        g['send_first_message'] = lambda s: (sent.append(s['name']), (True, 'ok'))[1]
        g['window_after'] = lambda s: {'utilization': 1.0, 'resets_at': None}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            g['activate']([g['SLOTS'][0], g['SLOTS'][2]])
        self.assertEqual(sent, ['claude3'])
        self.assertIn('aktif', out.getvalue())

    def test_dispatch_returns_changed_slots(self):
        for i, e in ((0, 'one@example.test'), (2, 'three@example.test')):
            s = self.slots[i]
            Path(s['dir']).mkdir(exist_ok=True)
            Path(s['json']).write_text(json.dumps({'oauthAccount': {'emailAddress': e}}))
            Path(s['creds']).write_text(json.dumps({'claudeAiOauth': {'accessToken': e}}))
        got = []
        self.g['activate'] = lambda slots: got.append([s['name'] for s in slots])
        with contextlib.redirect_stdout(io.StringIO()):
            self.g['main'](['claudeswap', 'three'])
            self.g['main'](['claudeswap', 'three'])   # zaten claude'da → değişiklik yok
        self.assertEqual(got, [['claude', 'claude3'], []])



def iso(t):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace('+00:00', 'Z')


def asst(mid, ts, out_tokens, inp=0, tools=(), model='claude-opus-5-5', cwd='/x'):
    content = [{'type': 'tool_use', 'id': t, 'name': 'Agent', 'input': {}} for t in tools] or [{'type': 'text', 'text': 'ok'}]
    return {'type': 'assistant', 'timestamp': iso(ts), 'cwd': cwd, 'entrypoint': 'cli', 'requestId': 'req_' + mid,
            'message': {'id': mid, 'model': model, 'content': content,
                        'usage': {'input_tokens': inp, 'output_tokens': out_tokens, 'cache_read_input_tokens': 0,
                                  'cache_creation_input_tokens': 0,
                                  'cache_creation': {'ephemeral_5m_input_tokens': 0, 'ephemeral_1h_input_tokens': 0}}}}


def write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(r, ensure_ascii=False, separators=(',', ':')) + '\n' for r in records), encoding='utf-8')


class CostTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.g = load(self.root)
        self.g['USE_COLOR'] = False
        self.g['busy_session_ids'] = lambda: set()
        self.slots = self.g['SLOTS']
        self.t0 = __import__('time').time() - 3600

    def seed_account(self, index, email, uuid):
        s = self.slots[index]
        Path(s['dir']).mkdir(exist_ok=True)
        Path(s['json']).write_text(json.dumps({'oauthAccount': {'emailAddress': email, 'accountUuid': uuid}}))
        Path(s['creds']).write_text(json.dumps({'claudeAiOauth': {'accessToken': email}}))

    def seed_sessions(self):
        t0, cwd_a = self.t0, '/srv/ornek/bot'
        chan = ('<channel source="ornek" chat_id="-100" message_id="1" user="ali" user_id="42" '
                'ts="x">merhaba bot</channel>')
        a = Path(self.slots[0]['dir']) / 'projects' / '-bot' / 'sessA.jsonl'
        write_jsonl(a, [
            {'type': 'session-owner', 'accountUuid': 'U1', 'sessionId': 'sessA'},
            {'type': 'user', 'isMeta': True, 'origin': {'kind': 'channel', 'server': 'plugin:ornek-kanal'},
             'turnOrigin': 'peer', 'timestamp': iso(t0), 'cwd': cwd_a, 'entrypoint': 'cli', 'message': {'content': chan}},
            asst('msg_1', t0 + 1, 100, inp=1000, tools=['toolu_A'], cwd=cwd_a),
            asst('msg_1', t0 + 2, 100, inp=1000, tools=['toolu_A'], cwd=cwd_a),        # akış tekrarı: bir kez sayılır
            {'type': 'user', 'timestamp': iso(t0 + 3), 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'toolu_A'}]}},
            {'type': 'user', 'origin': {'kind': 'human'}, 'promptSource': 'typed', 'turnOrigin': 'human',
             'timestamp': iso(t0 + 60), 'cwd': cwd_a, 'entrypoint': 'cli', 'message': {'content': 'yerel iş'}},
            asst('msg_2', t0 + 61, 1000, cwd=cwd_a),
            {'type': 'user', 'isMeta': True, 'origin': {'kind': 'task-notification'}, 'turnOrigin': 'task_notification',
             'timestamp': iso(t0 + 120), 'cwd': cwd_a,
             'message': {'content': '<task-notification><tool-use-id>toolu_A</tool-use-id></task-notification>'}},
            asst('msg_3', t0 + 121, 500, cwd=cwd_a),
        ])
        sub = a.parent / 'sessA' / 'subagents' / 'agent-x.jsonl'
        write_jsonl(sub, [asst('msg_s1', t0 + 10, 2000)])
        (sub.parent / 'agent-x.meta.json').write_text(json.dumps({'toolUseId': 'toolu_A'}))
        b = Path(self.slots[1]['dir']) / 'projects' / '-is' / 'sessB.jsonl'
        pm = '/srv/ornek/is'
        write_jsonl(b, [
            {'type': 'session-owner', 'accountUuid': 'U2', 'sessionId': 'sessB'},
            {'type': 'user', 'promptSource': 'sdk', 'turnOrigin': 'sdk', 'timestamp': iso(t0 + 30), 'cwd': pm,
             'entrypoint': 'sdk-cli', 'message': {'content': 'Sürüm kararını ver; son satır tek JSON.'}},
            asst('msg_1', t0 + 31, 100, inp=1000, cwd=pm),                          # kopyalanmış geçmiş: sayılmaz
            asst('msg_b2', t0 + 32, 1000, model='claude-sonnet-5-5', cwd=pm),
        ])

    def test_message_cost(self):
        mc = self.g['message_cost']
        u = {'input_tokens': 1_000_000, 'output_tokens': 1_000_000, 'cache_read_input_tokens': 1_000_000,
             'cache_creation_input_tokens': 2_000_000,
             'cache_creation': {'ephemeral_5m_input_tokens': 1_000_000, 'ephemeral_1h_input_tokens': 1_000_000},
             'server_tool_use': {'web_search_requests': 2}}
        self.assertAlmostEqual(mc(u, 'claude-opus-5-5')[0], 4 + 20 + 0.2 + 5 + 8 + 0.02)
        self.assertAlmostEqual(mc(u, 'claude-haiku-5-5')[0], 0.1 + 0.5 + 0.01 + 0.125 + 0.2 + 0.02)
        old = {'input_tokens': 0, 'output_tokens': 0, 'cache_creation_input_tokens': 1_000_000}
        self.assertAlmostEqual(mc(old, 'claude-sonnet-5-5')[0], 2.5)          # ayrımsız yazma → 5 dk fiyatı
        usd, exact = mc(u, 'claude-opus-9-9')
        self.assertFalse(exact)
        self.assertGreater(usd, 0)
        self.assertEqual(mc(u, 'gpt-x'), (0.0, False))

    def test_turns_notification_subagent_and_dedupe(self):
        self.seed_sessions()
        rows = self.g['cost_rows'](self.t0 - 60)
        self.assertEqual([r['src'] for r in rows], ['kanal', 'p', 'terminal'])
        tg, p, tmux = rows
        self.assertAlmostEqual(tg['usd'], (1000 * 4 + 100 * 20 + 500 * 20 + 2000 * 20) / 1e6)
        self.assertEqual(tg['text'], 'merhaba bot')
        self.assertNotIn('tag', tg)                                         # kanal etiketindeki kişi bilgisi tutulmaz
        self.assertEqual(tg['accts'], ['U1'])
        self.assertAlmostEqual(tmux['usd'], 1000 * 20 / 1e6)
        self.assertAlmostEqual(p['usd'], 1000 * 10 / 1e6)                    # msg_1 kopyası sayılmadı
        self.assertEqual(p['job'], '/srv/ornek/is')
        self.assertEqual(p['slot'], 'claude2')
        # önbellekten aynı sonuç
        self.assertTrue((self.root / '.claudeswap' / 'cost-cache.json').exists())
        again = self.g['cost_rows'](self.t0 - 60)
        self.assertEqual([round(r['usd'], 9) for r in again], [round(r['usd'], 9) for r in rows])

    def test_tokens_follow_cost_rules(self):
        self.seed_sessions()
        tg, p, tmux = self.g['cost_rows'](self.t0 - 60)
        self.assertEqual(tg['tokens'], [1000, 100 + 500 + 2000, 0, 0])   # akış tekrarı bir kez, alt-agent dahil
        self.assertEqual(p['tokens'], [0, 1000, 0, 0])                   # kopyalanmış msg_1 sayılmaz
        self.assertEqual(tmux['tokens'], [0, 1000, 0, 0])
        self.assertEqual(self.g['usage_tokens']({'input_tokens': 3, 'output_tokens': 4, 'cache_read_input_tokens': 5,
                                                 'cache_creation_input_tokens': 6}), [3, 4, 5, 6])

    def test_collapse_and_session_rows(self):
        base = {'src': 'p', 'job': 'j', 'text': 'Sadece OK yaz.', 'usd': 0.01, 'accts': ['U1'], 'running': False,
                'exact': True, 'count': 1, 'models': {}, 'slot': 'claude', 'end': 0, 'channel': False}
        rows = [dict(base, ts=i, sid=f's{i}') for i in range(4)] + [dict(base, ts=9, sid='big', usd=1.0, text='iş')]
        out = self.g['collapse_rows'](rows)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]['count'], 4)
        self.assertAlmostEqual(out[0]['usd'], 0.04)
        self.seed_sessions()
        sess = self.g['session_rows'](self.g['cost_rows'](self.t0 - 60))
        self.assertEqual(len(sess), 2)
        self.assertEqual(sess[0]['n'], 2)
        self.assertEqual(sess[0]['src'], 'kanal')

    def test_swap_is_logged_and_shown(self):
        self.seed_account(0, 'one@example.test', 'U1')
        self.seed_account(2, 'three@example.test', 'U3')
        self.seed_sessions()
        with contextlib.redirect_stdout(io.StringIO()):
            self.g['main'](['clsw', 'claude3'])
        log = [json.loads(l) for l in (self.root / '.claudeswap' / 'swaps.jsonl').read_text().splitlines()]
        self.assertEqual(log[0]['profiller'], {'claude': ['one@example.test', 'three@example.test'],
                                               'claude3': ['three@example.test', 'one@example.test']})
        self.assertEqual(len(self.g['swap_events']()), 1)                   # yedek klasörü aynı değişimi tekrarlamaz
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.g['main'](['clsw', 'cost', 'hepsi'])
        text = out.getvalue()
        self.assertIn('HESAP DEĞİŞİKLİĞİ YAPILDI', text)
        self.assertIn('claude: one@example.test → three@example.test', text)
        self.assertIn('kanal', text)
        self.assertIn('claude -p', text)
        self.assertIn('/srv/ornek/is', text)
        self.assertIn('Toplam:', text)
        self.assertIn('one ', text)                                         # U1 → one@… hesap adı

    def test_swap_backfill_from_backup(self):
        import os
        d = self.root / '.claudeswap' / 'backups' / '20261101-120000'
        d.mkdir(parents=True)
        (d / 'claude.claude.json').write_text(json.dumps({'oauthAccount': {'emailAddress': 'a@x', 'accountUuid': 'UA'}}))
        (d / 'claude4.claude.json').write_text(json.dumps({'oauthAccount': {'emailAddress': 'b@x', 'accountUuid': 'UB'}}))
        os.utime(d, (1, 1))
        ev = self.g['swap_events']()
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]['profiller'], {'claude': ['a@x', 'b@x'], 'claude4': ['b@x', 'a@x']})
        self.assertEqual(self.g['acct_label']('UB'), 'b')
        book = json.loads((self.root / '.claudeswap' / 'accounts.json').read_text())
        self.assertIn('20261101-120000', book['yedekler'])

    def test_parse_cost_args(self):
        import time
        pa = self.g['parse_cost_args']
        o = pa([])
        self.assertEqual((o['label'], o['limit'], o['mode'], o['src']), ('son 24 saat', 60, 'istem', None))
        o = pa(['3g', 'hepsi', 'oturum', 'kanal'])
        self.assertAlmostEqual(o['since'], time.time() - 3 * 86400, delta=5)
        self.assertEqual((o['label'], o['limit'], o['mode'], o['src']), ('son 3 gün', None, 'oturum', {'kanal'}))
        o = pa(['dün', '200'])
        self.assertEqual(o['until'] - o['since'] in (82800, 86400, 90000), True)
        self.assertEqual(o['limit'], 200)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            pa(['saçma'])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            pa(['geçiş'])                                                   # kayıtlı geçiş yok

    def test_swap_range(self):
        log = self.root / '.claudeswap' / 'swaps.jsonl'
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(''.join(json.dumps({'ts': ts, 'profiller': {'claude': [a, b], 'claude3': [b, a]}}) + '\n'
                               for ts, a, b in [(1000.0, 'x@t', 'y@t'), (2000.0, 'y@t', 'z@t'), (3000.0, 'z@t', None)]))
        pa = self.g['parse_cost_args']
        o = pa(['geçiş'])
        self.assertEqual((o['since'], o['until']), (3000.0, None))
        self.assertIn('son hesap geçişinden beri', o['label'])
        self.assertIn('claude: z → giriş yok', o['label'])
        self.assertEqual(pa(['gecis2'])['since'], 2000.0)
        self.assertEqual(pa(['3geçiş', '200'])['since'], 1000.0)
        o = pa(['geçiş5'])                                                  # fazlası istenirse en eskisi
        self.assertEqual(o['since'], 1000.0)
        self.assertIn('yalnız 3 geçiş kayıtlı', o['label'])

    def test_truncation_shown_on_top(self):
        self.seed_sessions()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.g['main'](['clsw', 'cost', '1'])
        lines = out.getvalue().splitlines()
        self.assertIn('yalnız en yeni 1 tanesi gösteriliyor', lines[1])
        self.assertIn('clsw cost hepsi', lines[1])

    def test_short_model(self):
        sm = self.g['short_model']
        self.assertEqual(sm('claude-opus-5-5'), 'opus 5.5')
        self.assertEqual(sm('claude-opus-5'), 'opus 5')
        self.assertEqual(sm('claude-fable-5-2'), 'fable 5.2')



class RefreshTests(unittest.TestCase):
    """clsw list: süresi geçmiş access token'ı Claude Code gibi yeniler (kilit + CAS)."""

    def setUp(self):
        import time
        from unittest import mock
        self.time, self.mock = time, mock
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.g = load(self.root)
        self.slot = self.g['SLOTS'][1]
        Path(self.slot['dir']).mkdir()
        self.now_ms = int(time.time() * 1000)
        self.write(accessToken='old-at', refreshToken='old-rt', expiresAt=self.now_ms - 1000,
                   refreshTokenExpiresAt=self.now_ms + 10 * 86400000, scopes=['user:inference', 'user:profile'],
                   subscriptionType='pro', rateLimitTier='default_claude_ai')
        self.posts = []

    def write(self, **o):
        Path(self.slot['creds']).write_text(json.dumps({'claudeAiOauth': o, 'other': 1}))

    def creds(self):
        return json.loads(Path(self.slot['creds']).read_text())

    def respond(self, payload=None, code=200, during=None):
        test = self

        class R(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def urlopen(req, timeout=None):
            test.posts.append((req.full_url, json.loads(req.data), dict(req.headers)))
            if during:
                during()
            if code != 200:
                import urllib.error
                raise urllib.error.HTTPError(req.full_url, code, 'x', {}, io.BytesIO(json.dumps(payload).encode()))
            return R(json.dumps(payload).encode())
        return self.mock.patch('urllib.request.urlopen', urlopen)

    def test_refreshes_and_keeps_other_fields(self):
        with self.respond({'access_token': 'new-at', 'refresh_token': 'new-rt', 'expires_in': 28800,
                           'refresh_token_expires_in': 30 * 86400, 'scope': 'user:inference user:profile'}):
            self.assertIsNone(self.g['refresh_oauth'](self.slot))
        url, body, headers = self.posts[0]
        self.assertEqual(url, 'https://platform.claude.com/v1/oauth/token')
        self.assertEqual(body, {'grant_type': 'refresh_token', 'refresh_token': 'old-rt',
                                'client_id': '9d1c250a-e61b-44d9-88ed-5944d1962f5e',
                                'scope': 'user:inference user:profile'})
        c = self.creds()
        o = c['claudeAiOauth']
        self.assertEqual((o['accessToken'], o['refreshToken']), ('new-at', 'new-rt'))
        self.assertGreater(o['expiresAt'], self.now_ms + 28000 * 1000)
        self.assertEqual(o['refreshTokenExpiresAt'], self.now_ms + 10 * 86400000)   # uzatılmaz (min)
        self.assertEqual((o['subscriptionType'], o['rateLimitTier'], c['other']), ('pro', 'default_claude_ai', 1))
        self.assertFalse(Path(self.slot['dir'], '.oauth_refresh.lock').exists())
        self.assertFalse(Path(self.slot['dir'] + '.lock').exists())

    def test_busy_lock_is_left_alone(self):
        for lock in (Path(self.slot['dir'], '.oauth_refresh.lock'), Path(self.slot['dir'] + '.lock')):
            lock.mkdir()
            with self.respond({'access_token': 'x', 'expires_in': 1}):
                self.assertEqual(self.g['refresh_oauth'](self.slot), 'kilit dolu')
            lock.rmdir()
        self.assertEqual(self.posts, [])
        self.assertEqual(self.creds()['claudeAiOauth']['accessToken'], 'old-at')
        self.assertFalse(Path(self.slot['dir'], '.oauth_refresh.lock').exists())   # başkasının kilidine dokunmaz

    def test_cas_does_not_overwrite_sibling_write(self):
        sibling = lambda: self.write(accessToken='sib-at', refreshToken='sib-rt', expiresAt=self.now_ms + 9e6)
        with self.respond({'access_token': 'new-at', 'expires_in': 28800}, during=sibling):
            self.assertEqual(self.g['refresh_oauth'](self.slot), 'dosya yenileme sırasında değişti')
        self.assertEqual(self.creds()['claudeAiOauth']['accessToken'], 'sib-at')

    def test_invalid_grant_and_errors(self):
        with self.respond({'error': 'invalid_grant'}, code=400):
            self.assertEqual(self.g['refresh_oauth'](self.slot), 'token geçersiz')
        with self.respond({'error': 'x'}, code=500):
            self.assertEqual(self.g['refresh_oauth'](self.slot), 'yenileme hatası (500)')
        with self.respond({'nope': 1}):
            self.assertEqual(self.g['refresh_oauth'](self.slot), 'yenileme yanıtı bozuk')
        self.assertEqual(self.creds()['claudeAiOauth']['accessToken'], 'old-at')

    def test_refresh_if_stale(self):
        info = self.g['slot_info'](self.slot)
        with self.respond({'error': 'invalid_grant'}, code=400):
            out = self.g['refresh_if_stale'](info)
        self.assertEqual(self.g['fetch_live'](out)['status'], 'token geçersiz')
        with self.respond({'access_token': 'new-at', 'expires_in': 28800}):
            out = self.g['refresh_if_stale'](info)
        self.assertEqual(out['oauth']['accessToken'], 'new-at')
        n = len(self.posts)
        self.assertIs(self.g['refresh_if_stale'](out), out)          # geçerli token'a dokunmaz
        self.write(accessToken='a', refreshToken='r', expiresAt=self.now_ms - 1, refreshTokenExpiresAt=self.now_ms - 1)
        stale_login = self.g['slot_info'](self.slot)
        self.assertIs(self.g['refresh_if_stale'](stale_login), stale_login)   # giriş bitmiş: /login gerekir
        self.assertEqual(len(self.posts), n)


class UsageFallbackTests(unittest.TestCase):
    def setUp(self):
        import urllib.error
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.g = load(self.root)
        self.g['USE_COLOR'] = False
        s = self.g['SLOTS'][0]
        Path(s['dir']).mkdir(exist_ok=True)
        Path(s['creds']).write_text(json.dumps({'claudeAiOauth': {
            'accessToken': 'tok', 'refreshToken': 'r', 'expiresAt': 9e12, 'accountUuid': 'U-ayse'}}))
        Path(s['json']).write_text(json.dumps({'oauthAccount': {'emailAddress': 'ayse@example.test', 'accountUuid': 'U-ayse'}}))
        self.usage_calls = 0
        self.measures = []

        def api_get(token, path):
            if path == 'profile':
                return {'account': {'email': 'ayse@example.test', 'uuid': 'U-ayse'}}
            self.usage_calls += 1
            raise urllib.error.HTTPError('u', 429, 'Too Many Requests', {'Retry-After': '199'}, None)
        self.g['api_get'] = api_get
        self.g['measure_usage'] = lambda slot, token: self.measures.append(token) or {
            'seven_day': {'utilization': 1.0, 'resets_at': '2026-11-28T20:00:00+00:00'}}

    def test_429_uses_measurement_then_cache(self):
        info = self.g['fetch_live'](self.g['slot_info'](self.g['SLOTS'][0]))
        self.assertNotIn('status', info)
        self.assertEqual(info['usage']['seven_day']['utilization'], 1.0)
        self.assertEqual(info['usage_src'], 'olcum')
        self.assertEqual(self.measures, ['tok'])
        info2 = self.g['fetch_live'](self.g['slot_info'](self.g['SLOTS'][0]))   # 5 dk içinde: yeniden ölçülmez
        self.assertEqual(self.measures, ['tok'])
        self.assertEqual(info2['usage']['seven_day']['utilization'], 1.0)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.g['cmd_list']()
        self.assertIn('yedek ölçüm', out.getvalue())
        self.assertIn('%1', out.getvalue())

    def test_429_without_measurement_shows_error(self):
        self.g['measure_usage'] = lambda slot, token: None
        info = self.g['fetch_live'](self.g['slot_info'](self.g['SLOTS'][0]))
        self.assertEqual(info['status'], 'API hatası (429)')

    def test_api_success_is_cached_with_resets(self):
        self.g['api_get'] = lambda token, path: ({'account': {'email': 'ayse@example.test', 'uuid': 'U-ayse'}} if path == 'profile'
                                                 else {'five_hour': {'utilization': 5}, 'cedar_ember': {'grants': []}})
        self.g['fetch_live'](self.g['slot_info'](self.g['SLOTS'][0]))
        cache = json.loads((self.root / '.claudeswap' / 'usage-cache.json').read_text())
        self.assertEqual(cache['U-ayse']['src'], 'api')
        self.g['usage_cache_put']({'accountUuid': 'U-ayse'}, {'seven_day': {'utilization': 2}}, 'olcum')
        cache = json.loads((self.root / '.claudeswap' / 'usage-cache.json').read_text())
        self.assertEqual(cache['U-ayse']['usage']['cedar_ember'], {'grants': []})   # RESET hakları korunur


class FakeKeychain:
    """/usr/bin/security yerine: find-generic-password -w, add-generic-password (-i ile stdin'den), delete."""

    def __init__(self):
        self.items, self.calls, self.broken = {}, [], False

    def __call__(self, args, stdin=None):
        self.calls.append((list(args), stdin))
        if self.broken:
            return subprocess.CompletedProcess(args, 36, b'', b'User interaction is not allowed.')
        if args[0] == '-i':
            import shlex
            parts = shlex.split(stdin.decode())
            kv = dict(zip(parts[2::2], parts[3::2])) if parts[1] == '-U' else {}
            self.items[(kv['-a'], kv['-s'])] = bytes.fromhex(kv['-X'])
            return subprocess.CompletedProcess(args, 0, b'', b'')
        kv = dict(zip(args[1::2], args[2::2]))
        key = (kv.get('-a'), kv.get('-s'))
        if args[0] == 'find-generic-password':
            if key not in self.items:
                return subprocess.CompletedProcess(args, 44, b'', b'could not be found')
            return subprocess.CompletedProcess(args, 0, self.items[key] + b'\n', b'')
        if args[0] == 'delete-generic-password':
            if self.items.pop(key, None) is None:
                return subprocess.CompletedProcess(args, 44, b'', b'')
            return subprocess.CompletedProcess(args, 0, b'', b'')
        raise AssertionError(args)


class KeychainTests(unittest.TestCase):
    """macOS: giriş Anahtar Zinciri'nde (Claude Code'un servis adıyla); clsw okur, taşır, siler."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.g = load(self.root)
        self.kc = FakeKeychain()
        self.g['_security'] = self.kc
        self.g['_keychain_account'] = lambda: 'kullanici'
        mk = self.g['_slot']
        self.slots = self.g['SLOTS'] = [
            mk('claude' if i == 1 else f'claude{i}', str(self.root / f'.c{i}'), str(self.root / f'.c{i}/.claude.json'),
               keychain=f'svc-{i}', config_env=True) for i in range(1, 11)]

    def seed(self, i, email, where='keychain'):
        s = self.slots[i]
        Path(s['dir']).mkdir(exist_ok=True)
        Path(s['json']).write_text(json.dumps({'oauthAccount': {'emailAddress': email}}))
        data = json.dumps({'claudeAiOauth': {'accessToken': email}}).encode()
        if where == 'keychain':
            self.kc.items[('kullanici', s['keychain'])] = data
        else:
            Path(s['creds']).write_bytes(data)

    def token(self, i):
        return (json.loads(self.g['read_creds'](self.slots[i]) or b'{}').get('claudeAiOauth') or {}).get('accessToken')

    def call(self, *args):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.g['main'](['clsw', *args])

    def test_service_name_matches_claude_code(self):
        import hashlib
        from unittest import mock
        ks = self.g['keychain_service']
        with mock.patch('sys.platform', 'darwin'):
            self.assertEqual(ks('/u/.claude2'), 'Claude Code-credentials-' + hashlib.sha256(b'/u/.claude2').hexdigest()[:8])
            self.assertEqual(ks(None), 'Claude Code-credentials')
        with mock.patch('sys.platform', 'linux'):
            self.assertIsNone(ks('/u/.claude2'))

    def test_swap_moves_keychain_logins(self):
        self.seed(0, 'one@example.test')
        self.seed(2, 'three@example.test')
        self.call('claude3')
        self.assertEqual(self.token(0), 'three@example.test')
        self.assertEqual(self.token(2), 'one@example.test')
        self.assertFalse(Path(self.slots[0]['creds']).exists())              # dosyaya yazılmadı
        for args, stdin in self.kc.calls:                                     # token komut satırında görünmez
            self.assertNotIn(b'example', b' '.join(a.encode() for a in args))
        self.call('claude3')                                                   # geri al
        self.assertEqual(self.token(0), 'one@example.test')

    def test_swap_with_empty_profile_clears_stale_file(self):
        self.seed(0, 'one@example.test')
        Path(self.slots[1]['dir']).mkdir()
        Path(self.slots[1]['creds']).write_text('{"claudeAiOauth": {"accessToken": "eski"}}')
        self.kc.items[('kullanici', 'svc-2')] = b'{}'
        self.call('claude2')
        self.assertEqual(self.token(1), 'one@example.test')                  # eski dosya değil, Anahtar Zinciri
        self.assertIsNone(self.token(0))
        self.assertEqual(self.kc.items[('kullanici', 'svc-1')], b'{}')        # çıkış yapılmış kayıt aynen taşındı
        self.assertFalse(Path(self.slots[0]['creds']).exists())

    def test_swap_into_profile_without_any_login(self):
        self.seed(0, 'one@example.test')
        Path(self.slots[3]['dir']).mkdir()
        Path(self.slots[3]['creds']).write_text('{"claudeAiOauth": {"accessToken": "eski"}}')   # kalıntı dosya
        self.kc.items.pop(('kullanici', 'svc-4'), None)
        self.call('claude4')                                                   # claude4 yalnız eski dosya: dolu sayılır
        self.assertEqual(self.token(3), 'one@example.test')
        self.assertEqual(self.token(0), 'eski')
        self.call('claude4')
        self.assertEqual(self.token(0), 'one@example.test')
        Path(self.slots[4]['dir']).mkdir()
        self.call('claude5')                                                   # hiç girişi yok
        self.assertIsNone(self.token(0))
        self.assertNotIn(('kullanici', 'svc-1'), self.kc.items)              # kayıt silindi, eski dosya da yok
        self.assertFalse(Path(self.slots[0]['creds']).exists())
        self.assertEqual(self.token(4), 'one@example.test')

    def test_locked_keychain_falls_back_to_file(self):
        self.seed(0, 'one@example.test', where='file')
        self.seed(2, 'three@example.test', where='file')
        self.kc.broken = True                                                  # ör. SSH oturumunda kilitli
        self.call('claude3')
        self.assertEqual(self.token(0), 'three@example.test')
        self.assertEqual(json.loads(Path(self.slots[2]['creds']).read_text())['claudeAiOauth']['accessToken'],
                         'one@example.test')

    def test_keychain_wins_over_stale_file(self):
        self.seed(0, 'yeni@example.test')
        Path(self.slots[0]['creds']).write_text('{"claudeAiOauth": {"accessToken": "eski"}}')
        self.assertEqual(self.token(0), 'yeni@example.test')
        self.assertEqual(self.g['creds_where'](self.slots[0]), 'Anahtar Zinciri')

    def test_hex_output_is_decoded(self):
        self.kc.items[('kullanici', 'svc-1')] = b'{"a": "\xc3\xa7"}'
        orig = self.kc.__call__

        def hex_out(args, stdin=None):
            r = orig(args, stdin)
            if args[0] == 'find-generic-password' and r.returncode == 0:
                r = subprocess.CompletedProcess(args, 0, r.stdout.rstrip(b'\n').hex().encode() + b'\n', b'')
            return r
        self.g['_security'] = hex_out
        self.assertEqual(json.loads(self.g['keychain_read']('svc-1')), {'a': 'ç'})

    def test_home_profile_follows_config_dir(self):
        import os, time
        f = self.g['_home_uses_config_dir']
        d, home_json = self.root / '.claude', self.root / '.claude.json'
        d.mkdir()
        self.assertTrue(f(str(d), str(home_json), {'CLAUDE_CONFIG_DIR': str(d)}))
        self.assertFalse(f(str(d), str(home_json), {'CLAUDE_CONFIG_DIR': str(self.root / 'baska')}))
        self.assertFalse(f(str(d), str(home_json), {}))                      # iç dosya yok
        home_json.write_text('{}')
        (d / '.claude.json').write_text('{}')
        os.utime(home_json, (time.time() - 100, time.time() - 100))
        self.assertTrue(f(str(d), str(home_json), {}))                       # iç dosya daha yeni
        os.utime(home_json, (time.time() + 100, time.time() + 100))
        self.assertFalse(f(str(d), str(home_json), {}))


class HelpTests(unittest.TestCase):
    def test_help_colored_only_on_terminal_and_text_kept(self):
        g = runpy.run_path(str(SCRIPT))['main'].__globals__
        g['USE_COLOR'] = False
        self.assertEqual(g['render_help'](g['HELP']), g['HELP'])
        g['USE_COLOR'] = True
        out = g['render_help'](g['HELP'])
        self.assertIn('\033[', out)
        self.assertEqual(g['ANSI'].sub('', out), g['HELP'])                 # renkler dışında metin aynı
        self.assertIn('\033[32mclsw list', out)                            # komutlar yeşil


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.g = load(self.root)

    def test_credentials_written_private_even_if_old_file_was_loose(self):
        import os, stat
        s = self.g['SLOTS'][1]
        Path(s['dir']).mkdir()
        Path(s['creds']).write_text('{}')
        os.chmod(s['creds'], 0o644)
        self.g['write_creds'](s, b'{"claudeAiOauth": {"accessToken": "x"}}')
        if os.name != 'nt':
            self.assertEqual(stat.S_IMODE(os.stat(s['creds']).st_mode), 0o600)

    def test_cached_prompt_text_is_masked(self):
        secret = 'Bearer ' + 'A1b2C3d4' * 5
        p = self.root / '.claude' / 'projects' / '-x' / 's.jsonl'
        write_jsonl(p, [{'type': 'user', 'origin': {'kind': 'human'}, 'promptSource': 'typed',
                         'timestamp': '2026-01-01T00:00:00Z', 'message': {'content': 'anahtar: ' + secret}},
                        asst('m1', 1767225601, 1)])
        ent = self.g['parse_session'](str(p))
        self.assertNotIn('A1b2C3d4', ent['turns'][0]['text'])
        self.assertIn('[gizli]', ent['turns'][0]['text'])

    def test_child_claude_env_drops_other_tokens(self):
        from unittest import mock
        with mock.patch.dict('os.environ', {'GH_TOKEN': 'gh', 'NPM_TOKEN': 'np', 'ANTHROPIC_API_KEY': 'k'}):
            _cmd, env = self.g['claude_command'](self.g['SLOTS'][1])
        for k in ('GH_TOKEN', 'NPM_TOKEN', 'ANTHROPIC_API_KEY'):
            self.assertNotIn(k, env)

    def test_update_never_sends_credentials_and_needs_digest(self):
        from unittest import mock
        seen = []

        def fake(req, timeout=None):
            seen.append((req.full_url, dict(req.header_items())))
            return contextlib.closing(io.BytesIO(json.dumps({'tag_name': 'v9.0.0', 'assets': []}).encode()))
        with mock.patch.dict('os.environ', {'GH_TOKEN': 'gizli', 'GITHUB_TOKEN': 'gizli'}), \
                mock.patch.object(self.g['urllib'].request, 'urlopen', fake):
            rel, err = self.g['latest_release']()
            with mock.patch.dict('os.environ', {'CLSW_RELEASES_API': 'http://ayna.example.com/r.json'}):
                self.assertIsNotNone(self.g['latest_release']()[1])           # https olmayan ayna reddedilir
        self.assertIsNone(err)
        self.assertEqual(len(seen), 1)
        self.assertFalse(any(k.lower() == 'authorization' for k in seen[0][1]))
        with self.assertRaises(RuntimeError):                                 # özetsiz arşiv kurulmaz
            self.g['download_asset']({'asset': {'browser_download_url': 'https://example.com/a.tar.gz'}},
                                     str(self.root / 'a'))
        with self.assertRaises(RuntimeError):
            self.g['download_asset']({'asset': {'browser_download_url': 'http://example.com/a.tar.gz',
                                                'digest': 'sha256:' + '0' * 64}}, str(self.root / 'a'))


class WebHostTests(unittest.TestCase):
    def test_host_header_checked(self):
        import cost_web
        h = cost_web.Handler.__new__(cost_web.Handler)
        port = cost_web.PORT
        for host, ok in ((f'127.0.0.1:{port}', True), (f'localhost:{port}', True), (f'[::1]:{port}', True),
                         ('127.0.0.1', True), (f'kotu.example.com:{port}', False), ('127.0.0.1:1', False), ('', False)):
            h.headers = {'Host': host}
            self.assertEqual(h.host_ok(), ok, host)


class ListCellTests(unittest.TestCase):
    def setUp(self):
        self.g = runpy.run_path(str(SCRIPT))['main'].__globals__
        self.g['USE_COLOR'] = False

    def test_expires_at_cell(self):
        g, now = self.g, 1_000_000.0
        cell = lambda o: g['expires_cell'](o, now)
        t = now + 25 * 86400
        self.assertEqual(cell({'refreshToken': 'r', 'refreshTokenExpiresAt': t * 1000}), g['clock'](t))
        self.assertTrue(cell({'refreshToken': 'r', 'refreshTokenExpiresAt': (now - 60) * 1000}).endswith('(dolmuş)'))
        self.assertEqual(cell({}), '-')                                       # giriş yok
        self.assertEqual(cell({'refreshToken': 'r'}), '?')

    def test_system_certs_used_only_when_own_path_missing(self):
        from unittest import mock
        import ssl, os
        g = self.g
        fake = ssl.DefaultVerifyPaths('/yok/cert.pem', '/yok/certs', 'SSL_CERT_FILE', '/yok/cert.pem',
                                      'SSL_CERT_DIR', '/yok/certs')
        env = {}
        with mock.patch('ssl.get_default_verify_paths', return_value=fake), \
                mock.patch('os.path.isfile', side_effect=lambda f: f == '/etc/ssl/certs/ca-certificates.crt'), \
                mock.patch('os.name', 'posix'):
            self.assertEqual(g['use_system_certs'](env), '/etc/ssl/certs/ca-certificates.crt')
        self.assertEqual(env['SSL_CERT_FILE'], '/etc/ssl/certs/ca-certificates.crt')
        self.assertIsNone(g['use_system_certs']({'SSL_CERT_FILE': '/x'}))     # kullanıcı ayarına dokunulmaz

    def test_update_box(self):
        g = self.g
        g['__version__'] = '1.1.0'
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            g['show_release']({'version': (1, 1, 1), 'notes': '- düzeltme\n- yeni sütun'})
        lines = out.getvalue().splitlines()
        self.assertTrue(lines[0].startswith('*****') and lines[-1] == lines[0])
        self.assertTrue(all(l.startswith('*') and l.endswith('*') for l in lines))
        self.assertEqual(len({len(l) for l in lines}), 1)                    # kutu düzgün
        self.assertIn('v1.1.0  →  v1.1.1', out.getvalue())
        self.assertIn('- yeni sütun', out.getvalue())


if __name__ == '__main__':
    unittest.main()
