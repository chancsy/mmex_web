"""Accounts page: base-currency totals, favourites first, hidden accounts
under "Others", collapsible groups; and the Settings page.

    python -m unittest tests.test_accounts_page -v
"""

import os
import re
import shutil
import sqlite3
import tempfile
import unittest

from engine import views
from engine.core import Ref
from tests import fixtures as fx
from tests.fixtures import ACCOUNT_IDS, BANK, CASH_USD, CASH_EUR, USD, EUR
from tests.test_web import PASSWORD, csrf
from web import create_app, secrets_file
from web.config import Settings
from web.pages import account_groups

BANK_ID, CASH_EUR_ID, CASH_USD_ID = ACCOUNT_IDS[BANK], ACCOUNT_IDS[CASH_EUR], ACCOUNT_IDS[CASH_USD]


class AccountsPageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='mmex_web_accts_')
        self.db = fx.build_db(os.path.join(self.tmp, 'money.mmb'))
        conn = sqlite3.connect(self.db)
        # Base EUR; USD at 0.25 by its fixed rate, 0.30 by history (latest up to today wins).
        conn.executemany('INSERT INTO INFOTABLE_V1 (INFONAME, INFOVALUE) VALUES (?, ?)',
                         [('BASECURRENCYID', str(EUR)), ('USECURRENCYHISTORY', 'TRUE')])
        conn.execute('UPDATE CURRENCYFORMATS_V1 SET BASECONVRATE = 0.25 WHERE CURRENCYID = ?', (USD,))
        conn.executemany('INSERT INTO CURRENCYHISTORY_V1 (CURRENCYID, CURRDATE, CURRVALUE, CURRUPDTYPE) VALUES (?, ?, ?, 2)',
                         [(USD, '2026-01-31', 0.28), (USD, '2026-02-28', 0.30), (USD, '2999-01-01', 9.99)])
        conn.execute("INSERT INTO CHECKINGACCOUNT_V1 (TRANSID, ACCOUNTID, TOACCOUNTID, PAYEEID, TRANSCODE, TRANSAMOUNT, "
                     "STATUS, CATEGID, TRANSDATE, TOTRANSAMOUNT, DELETEDTIME) VALUES "
                     "(6001, 3, -1, 2000, 'Deposit', 100, '', 1004, '2026-02-01', 100, '')")
        conn.commit()
        conn.close()
        self.data = os.path.join(self.tmp, 'data')
        secrets_file.create_or_update(os.path.join(self.data, 'secrets.json'), PASSWORD)
        self.app = create_app(Settings(db_path=self.db, data_dir=self.data, cookie_secure=False))
        self.client = self.app.test_client()
        page = self.client.get('/login')
        self.client.post('/login', data={'password': PASSWORD, 'csrf_token': csrf(page.text)})

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def conn(self):
        return sqlite3.connect(f'file:{self.db}?mode=ro', uri=True)

    def test_rates(self):
        conn = self.conn()
        rates = views.base_rates(conn, today='2026-11-12')
        self.assertEqual((rates[EUR], rates[USD]), (1, 0.30))
        self.assertEqual(views.base_rates(conn, today='2025-06-01')[USD], 0.28)   # nothing earlier: the earliest after
        conn.close()
        c = sqlite3.connect(self.db)
        c.execute("UPDATE INFOTABLE_V1 SET INFOVALUE = 'FALSE' WHERE INFONAME = 'USECURRENCYHISTORY'")
        c.commit()
        c.close()
        conn = self.conn()
        self.assertEqual(views.base_rates(conn)[USD], 0.25)
        conn.close()

    def test_groups_totals_hidden_favourites(self):
        conn = self.conn()
        rows = views.accounts(conn, Ref(conn), include_closed=True)
        conn.close()
        # Bank 1000 + 3.21 interest - 12.5 - 8 - 100 transfer (void row excluded) = 882.71
        # Cash EUR +100; Cash USD 100 USD at 0.30 = 30 EUR; closed card not counted.
        groups, grand = account_groups(rows, {'favourites_first': False, 'hidden_accounts': [CASH_USD_ID]})
        self.assertEqual([g['label'] for g in groups], ['Bank Accounts', 'Credit Card Accounts', 'Cash Accounts'])
        cash = groups[2]
        self.assertEqual([a['name'] for a in cash['shown']], [CASH_EUR])
        self.assertEqual([a['name'] for a in cash['others']], [CASH_USD])
        self.assertAlmostEqual(cash['balance'], 130.0)
        self.assertAlmostEqual(cash['others_balance'], 30.0)
        self.assertAlmostEqual(grand['balance'], 882.71 + 130.0)
        self.assertAlmostEqual(groups[1]['balance'], 0)
        # Cash (USD) sorts before Cash (EUR) by name, unless Cash (EUR) is a favourite and favourites go first.
        fav = [dict(a, favorite=a['name'] == CASH_EUR) for a in rows]
        groups, _ = account_groups(fav, {'favourites_first': False, 'hidden_accounts': []})
        self.assertEqual([a['name'] for a in groups[2]['shown']], [CASH_EUR, CASH_USD])
        groups, _ = account_groups(fav, {'favourites_first': True, 'hidden_accounts': []})
        self.assertEqual([a['name'] for a in groups[2]['shown']], [CASH_EUR, CASH_USD])

    def test_pages(self):
        page = self.client.get('/').text
        self.assertIn('Total of accounts', page)
        self.assertIn('<details class="card group" data-group="Cash" open>', page)
        self.assertNotIn('class="others"', page)
        self.assertIn('id="toggle-all"', page)
        settings = self.client.get('/settings')
        self.assertEqual(settings.status_code, 200)
        self.assertIn(f'name="hidden" value="{CASH_USD_ID}"', settings.text)
        r = self.client.post('/settings', data={'csrf_token': csrf(settings.text), 'favourites_first': '1',
                                                'hidden': [str(CASH_USD_ID), '999', 'x']})
        self.assertEqual(r.status_code, 302)
        prefs = self.app.config['MMEX_STORE'].prefs()
        self.assertEqual({k: prefs[k] for k in ('favourites_first', 'hidden_accounts', 'hide_balances')},
                         {'favourites_first': True, 'hidden_accounts': [CASH_USD_ID], 'hide_balances': False})
        page = self.client.get('/').text
        self.assertIn('Others (1)', page)
        self.assertIn('checked', self.client.get('/settings').text)
        # Settings work while the app is read-only (no backup dir here) and need CSRF and login.
        self.assertEqual(self.client.post('/settings', data={'favourites_first': '1'}).status_code, 400)
        self.assertEqual(self.app.test_client().get('/settings').status_code, 302)


    def test_assets_and_balance(self):
        def figures(page):
            texts = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', page))
            found = {k: re.search(k + r' €?(-?[0-9,]+\.[0-9]{2})', texts) for k in ('Total of accounts', 'Assets', 'Balance')}
            self.assertTrue(all(found.values()), texts[:600])
            return {k: float(m.group(1).replace(',', '')) for k, m in found.items()}

        # No assets: Assets 0.00, Balance the same as Total of accounts.
        f = figures(self.client.get('/').text)
        self.assertEqual(f, {'Total of accounts': 1012.71, 'Assets': 0.0, 'Balance': 1012.71})
        # Two assets, neither in an account (MMEX adds every asset): 1000 that
        # doesn't change, and 365 USD losing 10% a year since 2026-01-01.
        c = sqlite3.connect(self.db)
        c.executemany("INSERT INTO ASSETS_V1 (ASSETID, STARTDATE, ASSETNAME, ASSETSTATUS, CURRENCYID, VALUECHANGEMODE, "
                      "VALUE, VALUECHANGE, NOTES, VALUECHANGERATE, ASSETTYPE) VALUES "
                      "(?, '2026-01-01', ?, 'Open', ?, 'Percentage', ?, ?, '', ?, 'Property')",
                      [(1, 'Flat', -1, 1000, 'None', 0), (2, 'Van', USD, 365, 'Depreciates', 10)])
        c.commit()
        c.close()
        from web.pages import assets_total
        conn = self.conn()
        self.assertAlmostEqual(assets_total(conn, today='2026-01-01'), 1000 + 365 * 0.28)   # USD's earliest rate
        conn.close()
        f = figures(self.client.get('/').text)
        self.assertGreater(f['Assets'], 1000)
        self.assertLess(f['Assets'], 1000 + 365 * 0.30)
        self.assertEqual(f['Total of accounts'], 1012.71)
        self.assertAlmostEqual(f['Balance'], f['Total of accounts'] + f['Assets'], delta=0.011)

    def test_eye_hides_balances(self):
        page = self.client.get(f'/account/{BANK_ID}').text
        self.assertIn('<html lang="en">', page)
        self.assertIn('aria-label="Hide balances"', page)
        self.assertIn('<span class="secret"><span class="v">', page)
        # Transaction amounts are never hidden; balances are.
        self.assertRegex(page, r'class="amt num [a-z]+">-?€?[0-9]')
        token = csrf(page)
        r = self.client.post('/settings/balances', data={'csrf_token': token, 'hide': '1'},
                             headers={'X-Requested-With': 'fetch'})
        self.assertEqual(r.get_json(), {'hide_balances': True})
        page = self.client.get(f'/account/{BANK_ID}').text
        self.assertIn('<html lang="en" class="masked">', page)
        self.assertIn('aria-label="Show balances"', page)
        self.assertIn('name="hide" value="0"', page)
        # Without script: the form posts and comes back to the page.
        r = self.client.post('/settings/balances', data={'csrf_token': token, 'hide': '0', 'next': f'/account/{BANK_ID}'})
        self.assertEqual(r.headers['Location'], f'/account/{BANK_ID}')
        self.assertFalse(self.app.config['MMEX_STORE'].prefs()['hide_balances'])
        # The settings form keeps it in step, and the toggle needs CSRF and login.
        self.assertEqual(self.client.post('/settings/balances', data={'hide': '1'}).status_code, 400)
        self.assertEqual(self.app.test_client().post('/settings/balances', data={'hide': '1'}).status_code, 400)
        self.assertFalse(self.app.config['MMEX_STORE'].prefs()['hide_balances'])
        # Not logged in: no eye, never masked.
        self.assertNotIn('eye-form', self.app.test_client().get('/login').text)


if __name__ == '__main__':
    unittest.main()

    def test_show_all_in_base(self):
        def row(page, name):
            return re.search(r'<li[^>]*>\s*<a href="/account/\d+">\s*<span class="name">[^<]*' + re.escape(name) + r'.*?</li>',
                             page, re.S).group(0)
        page = self.client.get('/').text
        self.assertIn('>Show all in EUR</a>', page)
        self.assertIn('100.00', row(page, CASH_USD))                     # own currency
        page = self.client.get('/?base=1').text
        self.assertIn('>Show own currencies</a>', page)
        mine = row(page, CASH_USD)
        num = re.search(r'<span class="num[^"]*">(.*?)</span>\s*<span class="sub">', mine, re.S).group(1)
        self.assertIn('30.00', num)                                      # 100 USD at 0.30, the latest rate
        self.assertIn('100.00', re.search(r'<span class="num own[^"]*"[^>]*>(.*?)</span>', mine, re.S).group(1))   # own amount, under it
        self.assertIn('href="/?closed=1&amp;base=1"', page)              # Show closed keeps it
        self.assertIn('href="/?closed=1"', self.client.get('/?closed=1&base=1').text)   # and Show own keeps closed
