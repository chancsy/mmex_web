"""Reports: month-end balances, net worth, income vs expenses, categories,
asset-valued accounts, their pages, settings and API.

    python -m unittest tests.test_reports -v
"""

import math
import os
import shutil
import sqlite3
import tempfile
import unittest

from engine import core
from engine import reports as R
from tests import fixtures as fx
from tests.test_entry import csrf
from tests.test_queue import QueueTestsBase

BANK, CASH, CASH_USD, CLOSED = (fx.ACCOUNT_IDS[k] for k in (fx.BANK, fx.CASH_EUR, fx.CASH_USD, fx.CLOSED))
# Seed rows (January 2026, Bank): -12.50, -8.00 (R), -100 to Cash (EUR), +3.21 interest, a void -50.
BANK_JAN = 1000 - 12.5 - 8.0 - 100 + 3.21


def setup_rates(path):
    """Base USD, currency history on, EUR at 1.10 (Jan) and 1.20 (Feb);
    plus a split withdrawal and an asset."""
    conn = sqlite3.connect(path)
    conn.executemany('INSERT INTO INFOTABLE_V1 (INFONAME, INFOVALUE) VALUES (?, ?)',
                     [('BASECURRENCYID', str(fx.USD)), ('USECURRENCYHISTORY', 'TRUE')])
    conn.executemany('INSERT INTO CURRENCYHISTORY_V1 (CURRENCYID, CURRDATE, CURRVALUE, CURRUPDTYPE) VALUES (?, ?, ?, 1)',
                     [(fx.EUR, '2026-01-31', 1.1), (fx.EUR, '2026-02-28', 1.2)])
    # Feb: a 30.00 withdrawal from Cash (EUR), split 20 Food:Takeaway + 10 Income:Interest.
    conn.execute(
        'INSERT INTO CHECKINGACCOUNT_V1 (TRANSID, ACCOUNTID, TOACCOUNTID, PAYEEID, TRANSCODE, TRANSAMOUNT, STATUS, '
        "TRANSACTIONNUMBER, NOTES, CATEGID, TRANSDATE, FOLLOWUPID, TOTRANSAMOUNT, LASTUPDATEDTIME, DELETEDTIME, COLOR) "
        "VALUES (6001, ?, -1, ?, 'Withdrawal', 30, '', '', 'split', -1, '2026-02-10T00:00:00', -1, 30, "
        "'2026-02-10T00:00:00', '', -1)", (CASH, fx.DINER))
    conn.executemany('INSERT INTO SPLITTRANSACTIONS_V1 (TRANSID, CATEGID, SPLITTRANSAMOUNT) VALUES (6001, ?, ?)',
                     [(fx.TAKEAWAY, 20), (fx.INTEREST, 10)])
    conn.execute("INSERT INTO ASSETS_V1 (ASSETID, STARTDATE, ASSETNAME, ASSETSTATUS, CURRENCYID, VALUECHANGEMODE, "
                 "VALUE, VALUECHANGE, NOTES, VALUECHANGERATE, ASSETTYPE) VALUES "
                 "(1, '2026-01-01', 'Car', 'Open', ?, 'Percentage', 500, 'Depreciates', '', 10, 'Automobile')", (fx.EUR,))
    conn.commit()
    conn.close()


class MonthTests(unittest.TestCase):
    def test_months(self):
        self.assertEqual(R.month_end('2024-02'), '2024-02-29')
        self.assertEqual(R.month_end('2026-12'), '2026-12-31')
        self.assertEqual((R.shift('2026-01', -1), R.shift('2025-12', 1), R.shift('2026-05', -17)),
                         ('2025-12', '2026-01', '2024-12'))
        self.assertEqual(R.months('2026-02', 3), ['2025-12', '2026-01', '2026-02'])
        self.assertTrue(R.valid_month('2026-02'))
        for bad in ('2026-13', '2026-1', '202602', None, ''):
            self.assertFalse(R.valid_month(bad), bad)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='mmex_reports_')
        self.path = fx.build_db(os.path.join(self.tmp, 'money.mmb'), legacy=True)
        setup_rates(self.path)
        self.conn = sqlite3.connect(f'file:{self.path}?mode=ro', uri=True)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp)

    def test_balances_match_core(self):
        ms = ['2025-12', '2026-01', '2026-02']
        bal = R.balances(self.conn, ms)
        self.assertEqual(bal[BANK], {'2025-12': 1000, '2026-01': round(BANK_JAN, 2), '2026-02': round(BANK_JAN, 2)})
        self.assertEqual(bal[CASH]['2026-01'], 100)
        self.assertEqual(bal[CASH]['2026-02'], 70)
        for aid, row in bal.items():
            for ym in ms:
                self.assertAlmostEqual(row[ym], core.balance(self.conn, aid, R.month_end(ym)), places=2)

    def test_rates(self):
        rates = R.Rates(self.conn)
        self.assertEqual(rates.base, fx.USD)
        self.assertEqual(rates.on(fx.USD, '2026-01-15'), 1.0)
        self.assertEqual(rates.on(fx.EUR, '2025-06-01'), 1.1, 'before the first rate: the earliest one')
        self.assertEqual(rates.on(fx.EUR, '2026-02-27'), 1.1)
        self.assertEqual(rates.on(fx.EUR, '2026-02-28'), 1.2)
        self.assertEqual(rates.on(fx.EUR, '2030-01-01'), 1.2)

    def test_net_worth(self):
        cfg = {'nonliquid': {CASH}, 'investment': {BANK}}
        snap = R.snapshot(self.conn, ['2025-12', '2026-01', '2026-02'], cfg)
        jan, feb = R.net_worth(snap)
        self.assertAlmostEqual(jan['total'], (BANK_JAN + 100) * 1.1)
        self.assertAlmostEqual(jan['liquid'], BANK_JAN * 1.1)
        self.assertAlmostEqual(jan['investment'], BANK_JAN * 1.1)
        self.assertAlmostEqual(feb['total'], (BANK_JAN + 70) * 1.2)
        self.assertAlmostEqual(feb['d_total'], (BANK_JAN + 70) * 1.2 - (BANK_JAN + 100) * 1.1)
        # Rate moved 1.1 -> 1.2 on what was held at January's end.
        self.assertAlmostEqual(feb['fx'], (BANK_JAN + 100) * 0.1)
        # Ignored accounts leave every figure; investment needs liquid.
        snap = R.snapshot(self.conn, ['2026-01', '2026-02'], {'ignored': {BANK}, 'investment': {CASH}, 'nonliquid': {CASH}})
        row = R.net_worth(snap)[0]
        self.assertAlmostEqual(row['total'], 70 * 1.2)
        self.assertEqual(row['investment'], 0)

    def test_asset_valued_account(self):
        asset = R.assets(self.conn)[1]
        self.assertEqual((asset['name'], asset['change'], asset['rate'], asset['start']), ('Car', 'Depreciates', 10.0, '2026-01-01'))
        self.assertEqual(R.asset_factor(asset, '2025-12-31'), 1.0)
        self.assertAlmostEqual(R.asset_factor(asset, '2026-02-28'), math.exp(-10 / 36500 * 58))
        self.assertAlmostEqual(R.asset_factor(dict(asset, change='Appreciates'), '2026-02-28'), math.exp(10 / 36500 * 58))
        self.assertEqual(R.asset_factor(dict(asset, change='None'), '2026-02-28'), 1.0)
        snap = R.snapshot(self.conn, ['2026-02'], {'assets': {str(BANK): 1}})
        self.assertAlmostEqual(snap['accounts'][BANK]['own']['2026-02'], round(BANK_JAN * math.exp(-10 / 36500 * 58), 2))

    def test_income_expense_and_categories(self):
        ref = core.Ref(self.conn)
        lines = R.flows(self.conn, ref, '2026-01-01', '2026-02-28')
        jan, feb = R.income_expense(lines, ['2026-01', '2026-02'])
        # January in EUR at 1.10 (before the first rate: the earliest); the void row and the transfer don't count.
        self.assertAlmostEqual(jan['income'], 3.21 * 1.1)
        self.assertAlmostEqual(jan['expenses'], 20.5 * 1.1)
        self.assertAlmostEqual(jan['net'], (3.21 - 20.5) * 1.1)
        # February: one split withdrawal, both lines expenses.
        self.assertAlmostEqual(feb['expenses'], 30 * 1.1)
        self.assertEqual(feb['income'], 0)
        self.assertIsNone(feb['rate'])
        tops = {t['name']: t for t in R.by_category(lines, ['2026-01', '2026-02'])}
        self.assertAlmostEqual(tops['Food']['months']['2026-02'], -20 * 1.1)
        self.assertEqual([k['name'] for k in tops['Food']['children']], ['Takeaway'])
        self.assertAlmostEqual(tops['Income']['total'], (3.21 - 10) * 1.1)
        # Ignored accounts are left out.
        lines = R.flows(self.conn, ref, '2026-01-01', '2026-02-28', ignored={BANK, CASH})
        self.assertEqual(lines, [])


class ReportPageTests(QueueTestsBase):
    def setUp(self):
        super().setUp()
        setup_rates(self.db)

    def test_pages(self):
        r = self.client.get('/reports/')
        self.assertEqual(r.status_code, 200)
        self.assertIn('href="/reports/net-worth"', r.text)
        page = self.client.get('/reports/month-end?month=2026-02').text
        self.assertIn('Month-end balances', page)
        self.assertIn('‹ 2026-01', page)
        self.assertIn('€70.00', page)             # Cash (EUR) in its own currency
        self.assertIn('-€30.00', page)            # its change from January
        nw = self.client.get('/reports/net-worth?end=2026-02&n=3').text
        self.assertIn('>2026-02</a>', nw)
        self.assertIn('From rates', nw)
        inc = self.client.get('/reports/income?end=2026-02&n=3').text
        self.assertIn('>2026-01</th>', inc)
        self.assertIn('Average', inc)
        cats = self.client.get('/reports/categories?year=2026').text
        self.assertIn('Takeaway', cats)
        self.assertIn('data-start-shut', cats)
        acc = self.client.get('/reports/accounts?by=month&end=2026-02&n=3').text
        self.assertIn('Bank Accounts', acc)
        self.assertEqual(self.client.get('/reports/accounts?by=year&n=3').status_code, 200)
        for bad in ('/reports/net-worth?n=7', '/reports/income?end=2026-13', '/reports/month-end?month=x',
                    '/reports/accounts?by=week', '/reports/categories?year=26'):
            self.assertEqual(self.client.get(bad).status_code, 400, bad)

    def test_settings(self):
        page = self.client.get('/settings').text
        self.assertIn('id="reports"', page)
        self.assertIn('Car (depreciates 10.0%)', page)
        self.assertIn('Not saved yet', page)
        # Both investment and not liquid: refused, nothing saved.
        r = self.client.post('/settings/reports', data={'csrf_token': csrf(page), 'nonliquid': str(BANK),
                                                        'investment': str(BANK)}, follow_redirects=True)
        self.assertIn('investment accounts must be liquid', r.text)
        self.assertIsNone(self.app.config['MMEX_STORE'].prefs()['nonliquid_accounts'])
        self.client.post('/settings/reports', data={'csrf_token': csrf(page), 'ignored': str(CASH_USD),
                                                    'nonliquid': str(CASH), 'investment': str(BANK),
                                                    f'asset_{CLOSED}': '1', f'asset_{BANK}': '99'})
        prefs = self.app.config['MMEX_STORE'].prefs()
        self.assertEqual((prefs['report_ignored_accounts'], prefs['nonliquid_accounts'], prefs['investment_accounts']),
                         ([CASH_USD], [CASH], [BANK]))
        self.assertEqual(prefs['asset_valued_accounts'], {str(CLOSED): 1}, 'an unknown asset id is dropped')
        page = self.client.get('/reports/month-end?month=2026-02').text
        self.assertIn('Ignored (not in totals)', page)
        self.assertIn('>not liquid</span>', page)

    def test_api_month_end(self):
        r = self.api('get', '/reports/month-end?month=2026-02', token=self.recon)
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        cash = next(a for a in body['accounts'] if a['id'] == CASH)
        self.assertEqual((cash['balance'], cash['change'], cash['currency'], cash['rate']), (70, -30, 'EUR', 1.2))
        self.assertEqual(body['base'], 'USD')
        self.assertAlmostEqual(body['total'], round((BANK_JAN + 70) * 1.2, 2), places=2)
        self.assertEqual(self.api('get', '/reports/month-end?month=bad', token=self.recon).status_code, 400)
        self.assertEqual(self.client.get('/api/reports/month-end?month=2026-02').status_code, 401)


if __name__ == '__main__':
    unittest.main()
