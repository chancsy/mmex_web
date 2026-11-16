"""Reports: month-end balances, net worth, income vs expenses, categories,
MMEX asset values, their pages, settings and API.

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


D = 10 / 36500     # the test assets' daily rate (10% a year, depreciating)
ROW = ('INSERT INTO CHECKINGACCOUNT_V1 (TRANSID, ACCOUNTID, TOACCOUNTID, PAYEEID, TRANSCODE, TRANSAMOUNT, STATUS, '
       "TRANSACTIONNUMBER, NOTES, CATEGID, TRANSDATE, FOLLOWUPID, TOTRANSAMOUNT, LASTUPDATEDTIME, DELETEDTIME, COLOR) "
       "VALUES (?, ?, ?, -1, ?, ?, ?, '', '', -1, ?, -1, ?, '2026-01-01T00:00:00', '', -1)")
CAR_ACCT, BOATS_ACCT = 10, 11


def setup_assets(path):
    """After setup_rates: Asset accounts "Car" (holds the asset Car by its
    name) and "Boats" (holds the asset Dinghy by its type), and an asset
    "Land" with no account. Dinghy's linked rows: Buy 300 (Jan 10, after a
    plain transfer of 300 into Boats), Revalue to 250 (Jan 20), Sell 50
    (Feb 5, into Cash (USD)), and a void Buy that doesn't count."""
    conn = sqlite3.connect(path)
    conn.executemany('INSERT INTO ACCOUNTLIST_V1 (ACCOUNTID, ACCOUNTNAME, ACCOUNTTYPE, STATUS, FAVORITEACCT, CURRENCYID, '
                     "INITIALBAL, INITIALDATE) VALUES (?, ?, 'Asset', 'Open', 'FALSE', ?, 0, '2026-01-01')",
                     [(CAR_ACCT, 'Car', fx.USD), (BOATS_ACCT, 'Boats', fx.USD)])
    conn.executemany("INSERT INTO ASSETS_V1 (ASSETID, STARTDATE, ASSETNAME, ASSETSTATUS, CURRENCYID, VALUECHANGEMODE, "
                     "VALUE, VALUECHANGE, NOTES, VALUECHANGERATE, ASSETTYPE) VALUES "
                     "(?, '2026-01-01', ?, 'Open', -1, 'Percentage', ?, ?, '', 10, ?)",
                     [(2, 'Dinghy', 999, 'Depreciates', 'Boats'), (3, 'Land', 100, 'Appreciates', 'Property')])
    conn.executemany(ROW, [(7001, CASH_USD, BOATS_ACCT, 'Transfer', 300, '', '2026-01-09T00:00:00', 300),
                           (7002, BOATS_ACCT, -1, 'Withdrawal', 300, '', '2026-01-10T00:00:00', 300),
                           (7003, BOATS_ACCT, BOATS_ACCT, 'Transfer', 250, '', '2026-01-20T00:00:00', 250),
                           (7004, CASH_USD, -1, 'Deposit', 50, '', '2026-02-05T00:00:00', 50),
                           (7005, BOATS_ACCT, -1, 'Withdrawal', 40, 'V', '2026-02-06T00:00:00', 40)])
    conn.executemany("INSERT INTO TRANSLINK_V1 (CHECKINGACCOUNTID, LINKTYPE, LINKRECORDID) VALUES (?, 'Asset', 2)",
                     [(7002,), (7003,), (7004,), (7005,)])
    conn.commit()
    conn.close()


DINGHY_FEB = (250 * math.exp(-16 * D) - 50) * math.exp(-23 * D)
CAR_FEB_EUR = 500 * math.exp(-58 * D)


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
        # Investment's change leaves out the 100 moved from Bank (investment) to Cash:
        # what's left is January's interest less spending, then February's rate move.
        self.assertAlmostEqual(jan['moved_investment'], -100 * 1.1)
        self.assertAlmostEqual(jan['d_investment'], (3.21 - 20.5) * 1.1)
        self.assertEqual(feb['moved_investment'], 0)
        self.assertAlmostEqual(feb['d_investment'], BANK_JAN * 0.1)
        # By year: levels at the year's last month, changes and fx added up.
        (y,) = R.by_year([jan, feb])
        self.assertEqual((y['month'], y['end']), ('2026', '2026-02'))
        self.assertAlmostEqual(y['total'], feb['total'])
        self.assertAlmostEqual(y['d_total'], feb['total'] - R.net_worth(R.snapshot(self.conn, ['2025-11', '2025-12'], cfg))[0]['total'])
        for k in ('d_liquid', 'd_investment', 'moved_investment', 'fx'):
            self.assertAlmostEqual(y[k], jan[k] + feb[k], msg=k)
        # Ignored accounts leave every figure; an investment can be not liquid.
        snap = R.snapshot(self.conn, ['2026-01', '2026-02'], {'ignored': {BANK}, 'investment': {CASH}, 'nonliquid': {CASH}})
        row = R.net_worth(snap)[0]
        self.assertAlmostEqual(row['total'], 70 * 1.2)
        self.assertEqual(row['liquid'], 0)
        self.assertAlmostEqual(row['investment'], 70 * 1.2)

    def test_asset_values(self):
        setup_assets(self.path)
        found = R.assets(self.conn)
        car, dinghy = found[1], found[2]
        self.assertEqual((car['name'], car['change'], car['rate'], car['start'], car['currency'], car['links']),
                         ('Car', 'Depreciates', 10.0, '2026-01-01', fx.EUR, []))
        # Unlinked: VALUE grown from the start date; 0 before it.
        self.assertEqual(R.asset_value(car, '2025-12-31'), 0)
        self.assertAlmostEqual(R.asset_value(car, '2026-02-28'), CAR_FEB_EUR)
        self.assertAlmostEqual(R.asset_value(dict(car, change='Appreciates'), '2026-02-28'), 500 * math.exp(58 * D))
        self.assertEqual(R.asset_value(dict(car, change='None'), '2026-02-28'), 500)
        rates = R.Rates(self.conn)
        self.assertAlmostEqual(R.asset_value(car, '2026-02-28', rates), CAR_FEB_EUR * 1.2, msg='in the base currency')
        # Linked: VALUE ignored; Buy, Revalue, Sell, growth between; the void row skipped.
        self.assertEqual([x[1] for x in dinghy['links']], [7002, 7003, 7004])
        self.assertEqual(R.asset_value(dinghy, '2026-01-09', rates), 0, 'linked, nothing yet')
        self.assertAlmostEqual(R.asset_value(dinghy, '2026-01-15', rates), 300 * math.exp(-5 * D))
        self.assertAlmostEqual(R.asset_value(dinghy, '2026-01-20', rates), 250)
        self.assertAlmostEqual(R.asset_value(dinghy, '2026-02-28', rates), DINGHY_FEB)
        # Accounts hold assets by name, else by type; Land has no account.
        snap = R.snapshot(self.conn, ['2026-01', '2026-02'], {})
        accts = snap['accounts']
        self.assertEqual([x['name'] for x in accts[CAR_ACCT]['assets']], ['Car'])
        self.assertEqual([x['name'] for x in accts[BOATS_ACCT]['assets']], ['Dinghy'])
        self.assertEqual([x['name'] for x in snap['unlinked']], ['Land'])
        self.assertEqual(accts[BANK]['assets'], [])
        self.assertAlmostEqual(accts[CAR_ACCT]['own']['2026-02'], round(CAR_FEB_EUR * 1.2, 2))
        self.assertEqual(accts[BOATS_ACCT]['cash']['2026-02'], 0, 'the transfer in paid for the Buy')
        self.assertAlmostEqual(accts[BOATS_ACCT]['own']['2026-02'], round(DINGHY_FEB, 2))
        # Buying and selling the asset isn't income or spending.
        lines = R.flows(self.conn, core.Ref(self.conn), '2026-01-01', '2026-02-28')
        jan, feb = R.income_expense(lines, ['2026-01', '2026-02'])
        self.assertAlmostEqual(jan['expenses'], 20.5 * 1.1)
        self.assertEqual(feb['income'], 0)

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
        self.assertNotIn('>Transfers</th>', nw, 'no investment accounts chosen')
        inc = self.client.get('/reports/income?end=2026-02&n=3').text
        self.assertIn('>2026-01</th>', inc)
        self.assertIn('Average', inc)
        cats = self.client.get('/reports/categories?year=2026').text
        self.assertIn('Takeaway', cats)
        self.assertIn('data-start-shut', cats)
        acc = self.client.get('/reports/accounts?by=month&end=2026-02&n=3').text
        self.assertIn('Bank Accounts', acc)
        self.assertEqual(self.client.get('/reports/accounts?by=year&n=3').status_code, 200)
        nwy = self.client.get('/reports/net-worth?by=year&n=3')
        self.assertEqual(nwy.status_code, 200)
        self.assertIn('<th>Year</th>', nwy.text)
        for bad in ('/reports/net-worth?n=7', '/reports/net-worth?by=year&n=4', '/reports/income?end=2026-13', '/reports/month-end?month=x',
                    '/reports/accounts?by=week', '/reports/categories?year=26'):
            self.assertEqual(self.client.get(bad).status_code, 400, bad)

    def test_charts(self):
        import json, re
        from web import reports as web_reports
        page = self.client.get('/settings').text
        # Chart lines: named ones kept (max 5), unnamed dropped, unknown accounts ignored.
        self.client.post('/settings', data={'csrf_token': csrf(page),
                                            'line_0_name': 'Bank + Cash', 'line_0_add_to': 'none',
                                            'line_0_accounts': [str(BANK), str(CASH), '999'],
                                            'line_1_name': 'Liquid + Cash', 'line_1_add_to': 'liquid',
                                            'line_1_accounts': [str(CASH), str(BANK)],
                                            'line_2_name': '', 'line_2_accounts': str(BANK)})
        self.assertEqual(self.app.config['MMEX_STORE'].prefs()['chart_lines'],
                         [{'name': 'Bank + Cash', 'add_to': 'none', 'accounts': sorted([BANK, CASH])},
                          {'name': 'Liquid + Cash', 'add_to': 'liquid', 'accounts': sorted([BANK, CASH])}])
        self.assertIn('2 extra lines: Bank + Cash, Liquid + Cash', self.client.get('/settings').text)
        def chart(url):
            text = self.client.get(url).text
            return json.loads(re.search(r'<script type="application/json" class="chart-data">(.*?)</script>', text, re.S).group(1))
        nw = chart('/reports/net-worth?end=2026-02&n=3')
        self.assertEqual(nw['kind'], 'line')
        self.assertEqual(nw['money']['decimals'], 2)
        by = {s['name']: s for s in nw['series']}
        self.assertEqual([by[k]['slot'] for k in ('Total', 'Liquid', 'Bank + Cash', 'Liquid + Cash')], [1, 2, 4, 5])
        # Asset/loan default aside, Bank and Cash are both liquid: "Liquid + Cash" adds nothing twice.
        self.assertEqual(by['Liquid + Cash']['values'], by['Liquid']['values'])
        # The All range starts at the first month with data.
        allr = chart('/reports/net-worth?end=2026-02&n=all')
        with sqlite3.connect(self.db) as conn:
            first = R.first_month(conn)
        self.assertEqual(allr['x'][0], first)
        self.assertEqual(self.client.get('/reports/income?n=all').status_code, 400, 'All is Net worth only')
        inc = chart('/reports/income?end=2026-02&n=3')
        self.assertEqual((inc['kind'], len(inc['bars']['values']), len(inc['line']['values'])), ('bars', 3, 3))
        self.assertEqual(chart('/reports/accounts?by=year&n=3')['kind'], 'stack')
        self.assertIn('class="spark"', self.client.get('/reports/categories?end=2026-02&n=3').text)
        # Helpers: the rolling average only counts months with data; sparklines skip flat rows.
        rows = [{'month': '2026-01', 'net': 10.0}, {'month': '2026-02', 'net': 20.0}]
        self.assertEqual(web_reports._rolling(rows, ['2026-01', '2026-02'], '2026-01'), [10.0, 15.0])
        self.assertIsNone(web_reports._spark_points([0, 0, 0]))
        self.assertEqual(web_reports._spark_points({'a': 1.0}, ['a', 'b'])['zero'], 15)

    def test_settings(self):
        page = self.client.get('/settings').text
        self.assertIn('id="accounts"', page)
        self.assertIn('Reports not set up yet', page)
        # Both investment and not liquid: allowed.
        self.client.post('/settings', data={'csrf_token': csrf(page), 'nonliquid': str(BANK), 'investment': str(BANK)})
        prefs = self.app.config['MMEX_STORE'].prefs()
        self.assertEqual((prefs['nonliquid_accounts'], prefs['investment_accounts']), ([BANK], [BANK]))
        # One Save covers hidden accounts and the report flags together.
        self.client.post('/settings', data={'csrf_token': csrf(page), 'ignored': str(CASH_USD), 'hidden': str(CASH),
                                            'nonliquid': str(CASH), 'investment': str(BANK)})
        prefs = self.app.config['MMEX_STORE'].prefs()
        self.assertEqual((prefs['report_ignored_accounts'], prefs['nonliquid_accounts'], prefs['investment_accounts'],
                          prefs['hidden_accounts']), ([CASH_USD], [CASH], [BANK], [CASH]))
        self.assertIn('1 hidden · 1 ignored · 1 not liquid · 1 investment', self.client.get('/settings').text)
        page = self.client.get('/reports/month-end?month=2026-02').text
        self.assertIn('Ignored (not in totals)', page)
        self.assertIn('>not liquid</span>', page)
        self.assertNotIn('transfers)', page, 'nothing moved into Investment in February')
        jan = self.client.get('/reports/month-end?month=2026-01').text
        self.assertIn('transfers)', jan)           # Bank (investment) sent 100 to Cash (EUR)
        self.assertNotIn('>Transfers</th>', self.client.get('/reports/net-worth?end=2026-02&n=3').text, 'column hidden')

    def test_asset_accounts(self):
        setup_assets(self.db)
        page = self.client.get('/reports/month-end?month=2026-02').text
        self.assertIn('>incl. asset</span>', page)
        self.assertIn('Not counted: MMEX asset with no asset account of the same name: Land.', page)
        body = self.api('get', '/reports/month-end?month=2026-02', token=self.recon).get_json()
        boats = next(a for a in body['accounts'] if a['id'] == BOATS_ACCT)
        self.assertEqual((boats['cash'], boats['assets'], boats['balance']), (0, ['Dinghy'], round(DINGHY_FEB, 2)))

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
