import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

from engine import BackupPolicy, apply
from tests import fixtures as fx
from tests.fixtures import BANK, CASH_USD
from web import create_app, secrets_file
from web.auth import LoginThrottle
from web.config import ConfigError, Settings

PASSWORD = 'correct horse battery'
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def csrf(html) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


class WebTestsBase(unittest.TestCase):
    prefix = ''

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='mmex_web_test_')
        self.db = fx.build_db(os.path.join(self.tmp, 'money.mmb'), legacy=True)
        apply(self.db, {'ops': [{'op': 'add', 'date': '2026-02-01', 'type': 'Transfer', 'account': BANK,
                                 'to_account': CASH_USD, 'amount': 10, 'to_amount': 33.0, 'notes': 'fx <b>x</b>'}]},
              BackupPolicy(os.path.join(self.tmp, 'bak')))
        self.data = os.path.join(self.tmp, 'data')
        secrets_file.create_or_update(os.path.join(self.data, 'secrets.json'), PASSWORD)
        self.settings = Settings(db_path=self.db, data_dir=self.data, cookie_secure=False, url_prefix=self.prefix)
        self.app = create_app(self.settings)
        self.client = self.app.test_client()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def url(self, path):
        return self.prefix + path

    def login(self, password=PASSWORD, next_=None):
        page = self.client.get(self.url('/login'))
        q = f'?next={next_}' if next_ else ''
        return self.client.post(self.url('/login') + q, data={'password': password, 'csrf_token': csrf(page.text)})


class WebTests(WebTestsBase):
    def test_pages_need_login(self):
        for path in ('/', '/account/1', '/recent'):
            r = self.client.get(self.url(path))
            self.assertEqual(r.status_code, 302, path)
            self.assertIn('/login', r.headers['Location'])
        self.assertEqual(self.client.get(self.url('/healthz')).text, 'ok')

    def test_favicon_without_login(self):
        r = self.client.get(self.url('/favicon.ico'))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data[:4], b'\x00\x00\x01\x00')
        r.close()

    def test_login_logout(self):
        self.assertEqual(self.login('wrong password!!').status_code, 401)
        r = self.login(next_='/account/1')
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers['Location'], self.url('/account/1'))
        page = self.client.get(self.url('/'))
        self.assertEqual(page.status_code, 200)
        self.assertIn(BANK, page.text)
        self.client.post(self.url('/logout'), data={'csrf_token': csrf(page.text)})
        self.assertEqual(self.client.get(self.url('/')).status_code, 302)

    def test_login_never_redirects_off_site(self):
        for target in ('//evil.example/x', 'https://evil.example/', '/\\evil.example'):
            self.client.get(self.url('/logout'))
            with self.client.session_transaction() as s:
                s.clear()
            r = self.login(next_=target)
            self.assertEqual(r.headers['Location'], self.url('/'), target)

    def test_csrf_required(self):
        r = self.client.post(self.url('/login'), data={'password': PASSWORD})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.client.get(self.url('/')).status_code, 302)

    def test_throttle_after_five_failures(self):
        for _ in range(5):
            self.assertEqual(self.login('wrong password!!').status_code, 401)
        self.assertEqual(self.login().status_code, 429, 'even the right password waits')

    def test_password_change_logs_sessions_out(self):
        self.login()
        self.assertEqual(self.client.get(self.url('/')).status_code, 200)
        secrets_file.create_or_update(os.path.join(self.data, 'secrets.json'), 'a different password')
        app2 = create_app(self.settings)
        client2 = app2.test_client()
        cookie = self.client.get_cookie('mmex_web_session')
        client2.set_cookie('mmex_web_session', cookie.value)
        self.assertEqual(client2.get(self.url('/')).status_code, 302)

    def test_pages_render_and_escape(self):
        self.login()
        accounts = self.client.get(self.url('/'))
        self.assertIn('€', accounts.text)
        reg = self.client.get(self.url('/account/1'))
        self.assertEqual(reg.status_code, 200)
        self.assertIn(f'→ {CASH_USD}', reg.text)
        self.assertIn('fx &lt;b&gt;x&lt;/b&gt;', reg.text, 'notes are HTML-escaped')
        self.assertNotIn('<b>x</b>', reg.text)
        filtered = self.client.get(self.url('/account/1?status=R'))
        self.assertNotIn('fx &lt;b&gt;', filtered.text)
        self.assertEqual(self.client.get(self.url('/account/1?status=nope')).status_code, 400)
        self.assertEqual(self.client.get(self.url('/account/1?from=2026-13-01')).status_code, 400)
        self.assertEqual(self.client.get(self.url('/account/999')).status_code, 404)
        for by in ('added', 'edited', 'deleted'):
            self.assertEqual(self.client.get(self.url(f'/recent?by={by}')).status_code, 200)
        self.assertIn(f'← {BANK}', self.client.get(self.url('/account/3')).text)

    def test_login_over_https_like_a_browser(self):
        """Behind the proxy the app sees HTTPS, where Flask-WTF also checks the
        Referer. Browsers only send it if Referrer-Policy allows same-origin."""
        app = create_app(Settings(db_path=self.db, data_dir=self.data, cookie_secure=True, trusted_proxies=1))
        client = app.test_client()
        page = client.get('/login', base_url='https://money.example')
        self.assertEqual(page.headers['Referrer-Policy'], 'same-origin')
        r = client.post('/login', base_url='https://money.example', headers={'Referer': 'https://money.example/login'},
                        data={'password': PASSWORD, 'csrf_token': csrf(page.text)})
        self.assertEqual(r.status_code, 302)
        cookie = client.get_cookie('mmex_web_session', domain='money.example')
        self.assertTrue(cookie.secure and cookie.http_only)
        self.assertEqual(client.get('/', base_url='https://money.example').status_code, 200)
        missing = client.post('/logout', base_url='https://money.example', data={'csrf_token': csrf(page.text)})
        self.assertEqual(missing.status_code, 400, 'a POST without a Referer is refused over HTTPS')

    def test_static_urls_are_versioned(self):
        self.login()
        page = self.client.get(self.url('/')).text
        css = re.search(r'href="([^"]*/static/app\.css\?v=[0-9a-f]{12})"', page).group(1)
        with self.client.get(css) as r:
            self.assertEqual(r.status_code, 200)
            self.assertIn('immutable', r.headers['Cache-Control'])
        for url in ('/static/app.css', '/static/app.css?v=000000000000'):
            with self.client.get(self.url(url)) as r:
                self.assertEqual(r.headers['Cache-Control'], 'no-cache', url)
        with self.client.get(self.url('/static/../__init__.py?v=1')) as r:
            self.assertEqual(r.status_code, 404)

    def test_security_headers(self):
        r = self.client.get(self.url('/login'))
        self.assertIn("default-src 'self'", r.headers['Content-Security-Policy'])
        self.assertEqual(r.headers['X-Frame-Options'], 'DENY')
        self.assertEqual(r.headers['Cache-Control'], 'no-store')
        self.login()
        cookie = self.client.get_cookie('mmex_web_session')
        self.assertTrue(cookie.http_only)

    def test_warning_banners(self):
        self.login()
        fx.set_info(self.db, 'ISUSED', 'TRUE')
        self.assertIn('ISUSED', self.client.get(self.url('/')).text)
        fx.set_info(self.db, 'ISUSED', 'FALSE')
        conn = __import__('sqlite3').connect(self.db)
        conn.execute('PRAGMA user_version = 20')
        conn.close()
        self.assertIn("can&#39;t be written", self.client.get(self.url('/')).text)

    def test_missing_db(self):
        self.login()
        os.rename(self.db, self.db + '.gone')
        self.assertEqual(self.client.get(self.url('/')).status_code, 503)


class PrefixedWebTests(WebTestsBase):
    prefix = '/money'

    def test_prefix(self):
        self.assertEqual(self.client.get('/login').status_code, 404)
        self.assertEqual(self.client.get('/healthz').text, 'ok', 'healthcheck works without the prefix')
        r = self.login()
        self.assertEqual(r.headers['Location'], '/money/')
        page = self.client.get('/money/')
        self.assertIn('href="/money/account/1"', page.text)
        self.assertIn('/money/static/app.css', page.text)


class SetupAndConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='mmex_web_setup_')

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_setup_command_writes_600_and_keeps_tokens(self):
        env = dict(os.environ, APP_DATA_DIR=self.tmp, PYTHONDONTWRITEBYTECODE='1')
        run = lambda *a, pw=PASSWORD: subprocess.run([sys.executable, '-m', 'web', 'setup', '--password-stdin', *a],
                                                     input=pw + '\n', capture_output=True, text=True, cwd=ROOT, env=env)
        self.assertEqual(run().returncode, 0)
        path = os.path.join(self.tmp, 'secrets.json')
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        first = secrets_file.load(path)
        with open(path) as fh:
            self.assertNotIn(PASSWORD, fh.read())
        self.assertNotEqual(run(pw='short').returncode, 0)
        self.assertEqual(run(pw='another long password').returncode, 0)
        second = secrets_file.load(path)
        self.assertEqual(first['session_key'], second['session_key'], 'password change keeps the session key')
        self.assertNotEqual(first['password_hash'], second['password_hash'])
        run('--rotate', pw='another long password')
        self.assertNotEqual(secrets_file.load(path)['session_key'], first['session_key'])

    def test_app_refuses_to_start_without_secrets(self):
        with self.assertRaisesRegex(secrets_file.SecretsError, 'python -m web setup'):
            create_app(Settings(db_path='x.mmb', data_dir=self.tmp))

    def test_settings_from_env(self):
        with self.assertRaises(ConfigError):
            Settings.from_env({})
        s = Settings.from_env({'MMEX_DB_PATH': 'a', 'APP_DATA_DIR': 'b', 'MMEX_WEB_URL_PREFIX': '/m/',
                               'MMEX_WEB_COOKIE_SECURE': '0'})
        self.assertEqual((s.url_prefix, s.cookie_secure, s.trusted_proxies), ('/m', False, 0))
        with self.assertRaises(ConfigError):
            Settings.from_env({'MMEX_DB_PATH': 'a', 'APP_DATA_DIR': 'b', 'MMEX_WEB_URL_PREFIX': 'm'})

    def test_throttle_window(self):
        now = [0.0]
        t = LoginThrottle(limit=2, window=60, clock=lambda: now[0])
        t.failed()
        t.failed()
        self.assertGreater(t.wait_seconds(), 0)
        now[0] = 61
        self.assertEqual(t.wait_seconds(), 0)
        t.failed()
        t.succeeded()
        self.assertEqual(t.wait_seconds(), 0)



if __name__ == '__main__':
    unittest.main()
