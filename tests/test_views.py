import os
import shutil
import sqlite3
import tempfile
import unittest

from engine import BackupPolicy, apply, batch, core, views
from tests import fixtures as fx
from tests.fixtures import BANK, CASH_USD, CASH_EUR, CLOSED


class ViewsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='mmex_views_test_')
        self.db = fx.build_db(os.path.join(self.tmp, 'test.mmb'), legacy=True)
        ops = [
            {'op': 'add', 'date': '2026-02-01', 'type': 'Transfer', 'account': BANK, 'to_account': CASH_USD,
             'amount': 10, 'to_amount': 33.0, 'notes': 'fx'},
            {'op': 'add', 'date': '2026-02-01', 'type': 'Withdrawal', 'account': CASH_USD, 'payee': 'Diner',
             'category': 'Food:Takeaway', 'amount': 5.5, 'tags': ['trip', 'reno'], 'status': 'R'},
            {'op': 'add', 'date': '2026-02-02', 'type': 'Withdrawal', 'account': BANK, 'payee': 'Diner',
             'category': 'Food:Takeaway', 'amount': 1.0, 'notes': 'to delete'},
        ]
        result = apply(self.db, {'ops': ops}, BackupPolicy(os.path.join(self.tmp, 'bak')))
        self.assertTrue(result.ok, result.error)
        doomed = result.journal['entries'][-1]['row']['TRANSID']
        self.assertTrue(apply(self.db, {'ops': [{'op': 'delete', 'id': doomed}]}, None).ok)
        self.conn, self.schema, self.ref = batch.read(self.db)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp)

    def acct(self, name):
        return self.ref.account(name, 't', allow_closed=True)['id']

    def test_accounts_match_engine_balance(self):
        rows = views.accounts(self.conn, self.ref, include_closed=True)
        self.assertEqual({r['name'] for r in rows}, {BANK, CASH_EUR, CASH_USD, CLOSED})
        for r in rows:
            self.assertAlmostEqual(r['balance'], core.balance(self.conn, r['id']), places=2)
        bank = next(r for r in rows if r['name'] == BANK)
        self.assertAlmostEqual(bank['reconciled'], 1000 - 8.0, places=2, msg='initial plus R rows only')
        usd = next(r for r in rows if r['name'] == CASH_USD)
        self.assertAlmostEqual(usd['reconciled'], -5.5, places=2)
        self.assertNotIn(CLOSED, {r['name'] for r in views.accounts(self.conn, self.ref)})

    def test_running_balance_ends_each_day_at_engine_balance(self):
        for name in (BANK, CASH_EUR, CASH_USD):
            reg = views.register(self.conn, self.ref, self.acct(name), limit=10_000, newest_first=False)
            self.assertAlmostEqual(reg['balance'], core.balance(self.conn, self.acct(name)), places=2)
            last_by_day = {}
            for row in reg['rows']:
                last_by_day[row['date']] = row['balance']
            for day, bal in last_by_day.items():
                self.assertAlmostEqual(bal, core.balance(self.conn, self.acct(name), day), places=2, msg=f'{name} {day}')

    def test_register_directions_void_deleted(self):
        reg = views.register(self.conn, self.ref, self.acct(BANK), limit=10_000, newest_first=False)
        notes = [r['notes'] for r in reg['rows']]
        self.assertNotIn('to delete', notes, 'deleted rows never appear')
        void = next(r for r in reg['rows'] if r['void'])
        before = reg['rows'][reg['rows'].index(void) - 1]
        self.assertEqual(void['balance'], before['balance'], 'a Void row does not move the balance')
        out = next(r for r in reg['rows'] if r['notes'] == 'fx')
        self.assertEqual((out['kind'], out['amount'], out['other']), ('Transfer out', -10.0, f'→ {CASH_USD}'))
        usd = views.register(self.conn, self.ref, self.acct(CASH_USD), limit=10_000, newest_first=False)
        incoming = next(r for r in usd['rows'] if r['notes'] == 'fx')
        self.assertEqual((incoming['kind'], incoming['amount'], incoming['other']), ('Transfer in', 33.0, f'← {BANK}'))
        spent = next(r for r in usd['rows'] if r['kind'] == 'Withdrawal')
        self.assertEqual(spent['tags'], ['reno', 'trip'])

    def test_register_filters_and_paging(self):
        acct = self.acct(BANK)
        full = views.register(self.conn, self.ref, acct, limit=10_000)
        self.assertGreater(full['rows'][0]['date'], full['rows'][-1]['date'], 'newest first by default')
        only_r = views.register(self.conn, self.ref, acct, status='R')
        self.assertTrue(only_r['rows'] and all(r['status'] == 'R' for r in only_r['rows']))
        self.assertEqual(only_r['balance'], full['balance'], 'filters never change the balance')
        unrec = views.register(self.conn, self.ref, acct, status='unreconciled')
        self.assertTrue(all(r['status'] not in ('R', 'V') for r in unrec['rows']))
        dated = views.register(self.conn, self.ref, acct, date_from='2026-01-06', date_to='2026-01-07')
        self.assertEqual(sorted(r['date'] for r in dated['rows']), ['2026-01-06', '2026-01-07'])
        text = views.register(self.conn, self.ref, acct, text='FX')
        self.assertEqual([r['notes'] for r in text['rows']], ['fx'])
        page = views.register(self.conn, self.ref, acct, limit=2, offset=1)
        self.assertEqual([r['id'] for r in page['rows']], [r['id'] for r in full['rows'][1:3]])
        self.assertEqual(page['total'], full['total'])
        with self.assertRaises(ValueError):
            views.register(self.conn, self.ref, acct, status='nope')
        with self.assertRaises(KeyError):
            views.register(self.conn, self.ref, 999)

    def test_recent(self):
        added = views.recent(self.conn, self.ref, 'added', limit=3)
        self.assertEqual([r['notes'] for r in added][:1], [''], 'newest added first (the R withdrawal)')
        self.assertNotIn('to delete', [r['notes'] for r in added])
        deleted = views.recent(self.conn, self.ref, 'deleted')
        self.assertEqual([r['notes'] for r in deleted], ['to delete'])
        self.assertTrue(deleted[0]['deleted'])
        edited = views.recent(self.conn, self.ref, 'edited', limit=100)
        self.assertGreaterEqual(len(edited), 7)
        with self.assertRaises(ValueError):
            views.recent(self.conn, self.ref, 'nope')

    def test_unrounded_amounts_do_not_drift(self):
        """Old cross-currency rows can hold unrounded amounts. The running
        balance must sum the stored values, not per-row rounded ones."""
        self.conn.close()
        conn = sqlite3.connect(self.db)
        for i in range(3):
            conn.execute(
                'INSERT INTO CHECKINGACCOUNT_V1 (TRANSID, ACCOUNTID, TOACCOUNTID, PAYEEID, TRANSCODE, TRANSAMOUNT, STATUS, '
                "TRANSACTIONNUMBER, NOTES, CATEGID, TRANSDATE, FOLLOWUPID, TOTRANSAMOUNT, LASTUPDATEDTIME, DELETEDTIME, COLOR) "
                "VALUES (?, 3, 2, -1, 'Transfer', 2.5, '', '', 'raw', ?, '2026-03-01T00:00:00', -1, 0.7549, '', '', -1)",
                (9000 + i, fx.TRANSFER))
        conn.commit()
        conn.close()
        self.conn, self.schema, self.ref = batch.read(self.db)
        acct = self.acct(CASH_EUR)
        reg = views.register(self.conn, self.ref, acct, limit=10_000, newest_first=False)
        self.assertAlmostEqual(reg['balance'], core.balance(self.conn, acct), places=2)
        self.assertEqual(reg['rows'][-1]['balance'], core.balance(self.conn, acct, '2026-03-01'))
        self.assertEqual(reg['rows'][-1]['amount'], 0.75, 'each row still shows its rounded amount')

    def test_currencies(self):
        cur = views.currencies(self.conn)
        self.assertEqual((cur[fx.EUR]['prefix'], cur[fx.EUR]['decimals']), ('€', 2))
        self.assertEqual(cur[fx.USD]['symbol'], 'USD')


if __name__ == '__main__':
    unittest.main()
