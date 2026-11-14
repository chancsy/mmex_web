"""Currency history: rounding, xe.com table parsing, writes, and the Rates
pages (the download is faked; no network).

    python -m unittest tests.test_rates -v
"""

import datetime
import os
import re
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

from engine import rates
from tests import fixtures as fx
from tests.fixtures import USD, EUR
from tests.test_web import PASSWORD, csrf
from web import create_app, secrets_file
from web.config import Settings

XE_SAMPLE = ('<table><tr><th><a href="/c/EUR">EUR</a></th><td>Euro</td><td>0.31535037762796286</td>'
             '<td>3.1710759553291483</td></tr><tr><th><a href="/c/KRW">KRW</a></th><td>South Korean Won</td>'
             '<td>328.818324375595</td><td>0.0030411930414733913</td></tr>'
             # Some rows don't link their code, and some codes are 4 letters.
             '<tr><th scope="row">CNH</th><td>Chinese Yuan Renminbi Offshore</td><td>1.6413131423969411</td>'
             '<td>0.6092682585479209</td></tr><tr><th scope="row">USDT</th><td>Tether</td><td>0.2467</td><td>4.0532</td></tr>'
             '<tr><th>Currency</th><td>Name</td><td>Units per USD</td><td>USD per unit</td></tr></table>')
KRW = 78


class RateUnitTests(unittest.TestCase):
    def test_round_rate(self):
        self.assertEqual(rates.round_rate('3.1710759553291483'), 3.171076)
        self.assertEqual(rates.round_rate('0.0030411930414733913'), 0.003041)
        self.assertEqual(rates.round_rate('1.0000005'), 1.000001)      # half up, from the text
        self.assertEqual(rates.round_rate('1,234.5'), 1234.5)
        for bad in ('abc', '0', '-1', '0.0000001', 'nan', 'inf'):
            with self.assertRaises(rates.RateError, msg=bad):
                rates.round_rate(bad)
        for bad in ('abc', '0', '-1', 'nan', 'inf'):
            with self.assertRaises(rates.RateError, msg=bad):
                rates.parse_rate(bad)
        self.assertEqual(rates.parse_rate('3.1710759553291483'), 3.1710759553291483)
        self.assertEqual(rates.parse_rate('0.0000001'), 1e-07)   # tiny but positive: fine unrounded

    def test_parse_xe_table(self):
        self.assertEqual(rates.parse_xe_table(XE_SAMPLE), {'EUR': '3.1710759553291483', 'KRW': '0.0030411930414733913',
                                                           'CNH': '0.6092682585479209', 'USDT': '4.0532'})
        self.assertEqual(rates.parse_xe_rows(XE_SAMPLE)[2], {'code': 'CNH', 'name': 'Chinese Yuan Renminbi Offshore',
                                                             'units_per': '1.6413131423969411', 'per_unit': '0.6092682585479209'})
        self.assertEqual(rates.parse_xe_table('<html>blocked</html>'), {})

    def test_month_end(self):
        self.assertEqual(rates.month_end(2028, 2).isoformat(), '2028-02-29')
        self.assertTrue(rates.is_month_end('2026-10-31'))
        self.assertFalse(rates.is_month_end('2026-10-30'))

    def test_no_download_for_a_day_not_over(self):
        with self.assertRaises(rates.RateError):
            rates.fetch_xe('USD', datetime.date.today().isoformat())


class RatePageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='mmex_web_rates_')
        self.db = fx.build_db(os.path.join(self.tmp, 'money.mmb'))
        conn = sqlite3.connect(self.db)
        # Base USD; an EUR account is open (fixture), and an old long-precision EUR rate exists.
        conn.execute("INSERT INTO INFOTABLE_V1 (INFONAME, INFOVALUE) VALUES ('BASECURRENCYID', ?)", (str(USD),))
        conn.execute('INSERT INTO CURRENCYHISTORY_V1 (CURRHISTID, CURRENCYID, CURRDATE, CURRVALUE, CURRUPDTYPE) '
                     "VALUES (1, ?, '2024-12-31', 3.29934567891234, 1)", (EUR,))
        conn.commit()
        conn.close()
        self.data = os.path.join(self.tmp, 'data')
        secrets_file.create_or_update(os.path.join(self.data, 'secrets.json'), PASSWORD)
        self.bak = os.path.join(self.tmp, 'bak')
        self.app = create_app(Settings(db_path=self.db, data_dir=self.data, cookie_secure=False, backup_dir=self.bak,
                                       backup_min_interval_min=0))
        self.client = self.app.test_client()
        page = self.client.get('/login')
        self.client.post('/login', data={'password': PASSWORD, 'csrf_token': csrf(page.text)})

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def rows(self):
        return fx.q(self.db, 'SELECT CURRENCYID, CURRDATE, CURRVALUE, CURRUPDTYPE FROM CURRENCYHISTORY_V1 ORDER BY CURRDATE, CURRENCYID')

    def test_list_and_month_picker(self):
        page = self.client.get('/rates').text
        self.assertIn('2024-12-31', page)
        self.assertIn('3.299346', page)
        self.assertIn('>EUR</th>', page)
        self.assertNotIn('>USD</th>', page)       # the base currency has no rate
        # Every month-end since the first rate shows, the empty ones with a Download button.
        self.assertIn('id="d2025-01-31"', page)
        self.assertIn('action="/rates/2025-01-31"', page)
        self.assertNotIn('action="/rates/2024-12-31"', page)  # has EUR already (the open-account currency)
        r = self.client.get('/rates?month=2028-02')
        self.assertEqual(r.headers['Location'], '/rates/2028-02-29')

    def test_download(self):
        with mock.patch('engine.rates.fetch_xe', return_value=rates.parse_xe_rows(XE_SAMPLE)) as fetch:
            page = self.client.get('/rates/2026-10-31')
            self.assertIn('Download from xe.com', page.text)
            r = self.client.post('/rates/2026-10-31', data={'csrf_token': csrf(page.text), 'action': 'download'})
            fetch.assert_called_once_with('USD', '2026-10-31')
        self.assertEqual(r.status_code, 302)
        # xe.com's figure unrounded, as the ledger sheet and MMEX's online update keep it.
        self.assertIn((EUR, '2026-10-31', 3.1710759553291483, rates.ONLINE), self.rows())
        self.assertEqual(len(os.listdir(self.bak)), 1)
        self.assertIn('EUR 3.1710759553291483', self.client.get('/rates/2026-10-31').text)
        # Not in the History page: rates aren't journalled.
        self.assertEqual(self.app.config['MMEX_STORE'].count(), 0)

    def test_download_from_the_list_returns_to_it(self):
        with mock.patch('engine.rates.fetch_xe', return_value=rates.parse_xe_rows(XE_SAMPLE)):
            page = self.client.get('/rates').text
            r = self.client.post('/rates/2025-01-31', data={'csrf_token': csrf(page), 'action': 'download', 'back': 'list'})
        self.assertEqual(r.headers['Location'], '/rates#d2025-01-31')
        self.assertIn((EUR, '2025-01-31', 3.1710759553291483, rates.ONLINE), self.rows())

    def test_download_replaces_a_rounded_rate(self):
        conn = sqlite3.connect(self.db)
        conn.execute('INSERT INTO CURRENCYHISTORY_V1 (CURRHISTID, CURRENCYID, CURRDATE, CURRVALUE, CURRUPDTYPE) '
                     "VALUES (2, ?, '2026-10-31', 3.171076, 1)", (EUR,))
        conn.commit()
        conn.close()
        with mock.patch('engine.rates.fetch_xe', return_value=rates.parse_xe_rows(XE_SAMPLE)):
            page = self.client.get('/rates/2026-10-31').text
            self.client.post('/rates/2026-10-31', data={'csrf_token': csrf(page), 'action': 'download'})
            self.assertIn('(was 3.171076)', self.client.get('/rates/2026-10-31').text)
            self.assertIn((EUR, '2026-10-31', 3.1710759553291483, rates.ONLINE), self.rows())
            # Same again: nothing to change.
            self.client.post('/rates/2026-10-31', data={'csrf_token': csrf(page), 'action': 'download'})
            self.assertIn('match what', self.client.get('/rates/2026-10-31').text)

    def test_download_by_fetch_answers_json(self):
        """The list's in-place Download: JSON for the row, no redirect, no flash left behind."""
        with mock.patch('engine.rates.fetch_xe', return_value=rates.parse_xe_rows(XE_SAMPLE)):
            page = self.client.get('/rates').text
            r = self.client.post('/rates/2025-01-31', data={'csrf_token': csrf(page), 'action': 'download', 'back': 'list'},
                                 headers={'X-Requested-With': 'fetch'})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json['ok'])
        self.assertFalse(r.json['missing'])
        self.assertEqual(r.json['values'][str(EUR)], {'value': 3.1710759553291483, 'type': 'online', 'shown': '3.171076'})
        self.assertIn('Downloaded from xe.com for 2025-01-31', r.json['messages'][0]['text'])
        self.assertNotIn('Downloaded from xe.com', self.client.get('/rates').text)   # not also flashed
        with mock.patch('engine.rates.fetch_xe', side_effect=rates.RateError('could not reach xe.com: timed out')):
            r = self.client.post('/rates/2025-02-28', data={'csrf_token': csrf(page), 'action': 'download'},
                                 headers={'X-Requested-With': 'fetch'})
        self.assertFalse(r.json['ok'])
        self.assertTrue(r.json['missing'])
        self.assertEqual(r.json['values'], {})

    def test_one_visit_saves_the_whole_table(self):
        store = self.app.config['MMEX_STORE']
        with mock.patch('engine.rates.fetch_xe', return_value=rates.parse_xe_rows(XE_SAMPLE)) as fetch:
            page = self.client.get('/rates/2026-10-31').text
            self.client.post('/rates/2026-10-31', data={'csrf_token': csrf(page), 'action': 'download'})
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(len(store.xe_table('USD', '2026-10-31')['rates']), 4)   # every currency, not just EUR
            page = self.client.get('/rates/2026-10-31').text
            self.assertIn('Refresh from xe.com', page)
            # Delete the stored EUR rate, then fill again: from the saved table, no visit.
            self.client.post('/rates/2026-10-31', data={'csrf_token': csrf(page), 'action': 'save', f'rate_{EUR}': ''})
            self.client.post('/rates/2026-10-31', data={'csrf_token': csrf(page), 'action': 'download'})
            self.assertEqual(fetch.call_count, 1)
            self.assertIn('Filled from the saved xe.com table for 2026-10-31', self.client.get('/rates/2026-10-31').text)
            self.assertIn((EUR, '2026-10-31', 3.1710759553291483, rates.ONLINE), self.rows())
            # Refresh: a visit, whatever is saved.
            self.client.post('/rates/2026-10-31', data={'csrf_token': csrf(page), 'action': 'refresh'})
            self.assertEqual(fetch.call_count, 2)

    def test_fill_all_and_kept_currencies(self):
        store = self.app.config['MMEX_STORE']
        store.save_xe_table('USD', '2026-09-30', rates.parse_xe_rows(XE_SAMPLE))
        store.save_xe_table('USD', '2026-08-31', rates.parse_xe_rows(XE_SAMPLE))
        page = self.client.get('/rates').text
        self.assertIn('Fill 2 month-end(s) from saved tables', page)
        self.assertIn('title="From the saved xe.com table: no visit to xe.com">Fill</button>', page)
        with mock.patch('engine.rates.fetch_xe') as fetch:
            self.client.post('/rates/fill', data={'csrf_token': csrf(page)})
            fetch.assert_not_called()
        self.assertIn((EUR, '2026-09-30', 3.1710759553291483, rates.ONLINE), self.rows())
        self.assertNotIn(KRW, [r[0] for r in self.rows()])            # no KRW account: not kept
        # Keep KRW (Settings): its saved month-ends fill at once, nothing else changes.
        page = self.client.get('/settings').text
        self.assertIn(f'name="keep_currency" value="{KRW}"', page)
        self.assertIn('not on xe.com', page)                            # e.g. GBP isn't in the sample table
        self.client.post('/settings', data={'csrf_token': csrf(page), 'keep_currency': str(KRW)})
        self.assertEqual(self.app.config['MMEX_STORE'].prefs()['rate_keep_currencies'], [KRW])
        self.assertIn((KRW, '2026-08-31', 0.0030411930414733913, rates.ONLINE), self.rows())
        self.assertIn((KRW, '2026-09-30', 0.0030411930414733913, rates.ONLINE), self.rows())
        self.assertIn('Filled 2 rate(s) on 2 month-end(s)', self.client.get('/settings').text)

    def test_download_failure_writes_nothing(self):
        before = fx.sha(self.db)
        with mock.patch('engine.rates.fetch_xe', side_effect=rates.RateError('could not reach xe.com: timed out')):
            page = self.client.get('/rates/2026-10-31')
            self.client.post('/rates/2026-10-31', data={'csrf_token': csrf(page.text), 'action': 'download'})
        self.assertIn('Nothing downloaded for 2026-10-31: could not reach xe.com', self.client.get('/rates/2026-10-31').text)
        self.assertEqual(fx.sha(self.db), before)

    def test_edit_keep_and_delete(self):
        page = self.client.get('/rates/2024-12-31').text
        value = re.search(rf'name="rate_{EUR}"[^>]*value="([^"]*)"', page).group(1)
        self.assertEqual(value, '3.29934567891234')
        # Untouched: stays as stored, more than 6 decimals and all.
        self.client.post('/rates/2024-12-31', data={'csrf_token': csrf(page), 'action': 'save', f'rate_{EUR}': value})
        self.assertEqual(self.rows(), [(EUR, '2024-12-31', 3.29934567891234, 1)])
        # Typed: kept as typed (full precision, e.g. pasted from the ledger) and marked Manual.
        self.client.post('/rates/2024-12-31', data={'csrf_token': csrf(page), 'action': 'save', f'rate_{EUR}': '3.30000049'})
        self.assertEqual(self.rows(), [(EUR, '2024-12-31', 3.30000049, rates.MANUAL)])
        # Bad input: nothing saved.
        self.client.post('/rates/2024-12-31', data={'csrf_token': csrf(page), 'action': 'save', f'rate_{EUR}': 'x'})
        self.assertEqual(self.rows(), [(EUR, '2024-12-31', 3.30000049, rates.MANUAL)])
        # Cleared: deleted.
        self.client.post('/rates/2024-12-31', data={'csrf_token': csrf(page), 'action': 'save', f'rate_{EUR}': ''})
        self.assertEqual(self.rows(), [])

    def test_read_only_and_today(self):
        today = datetime.date.today().isoformat()
        page = self.client.get(f'/rates/{today}').text
        self.assertNotIn('Download from xe.com', page)
        ro = create_app(Settings(db_path=self.db, data_dir=self.data, cookie_secure=False)).test_client()
        p = ro.get('/login')
        ro.post('/login', data={'password': PASSWORD, 'csrf_token': csrf(p.text)})
        page = ro.get('/rates/2024-12-31').text
        self.assertIn('disabled', page)
        r = ro.post('/rates/2024-12-31', data={'csrf_token': csrf(page), 'action': 'save', f'rate_{EUR}': ''})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(len(self.rows()), 1)


if __name__ == '__main__':
    unittest.main()
