"""Engine behaviour on DBs built from the official v21 schema, in both the
fresh shape and the upgraded ("legacy") shape.

    python -m unittest -v
"""

import datetime
import os
import re
import shutil
import sqlite3
import tempfile
import time
import unittest

from engine import BackupPolicy, GuardError, SchemaError, apply, preview, undo
from engine import batch, core
from tests import fixtures as fx
from tests.fixtures import BANK, CASH_USD, CASH_EUR, CLOSED

DAY = '2099-01-15'  # far from the seed rows so duplicate checks can't hit them
NOTE = 'engine selftest'


def state(path) -> dict:
    """fx.dump, with tag links compared by (row, tag): restoring a row's tags
    gives the links new TAGLINKIDs, which MMEX never looks at."""
    out = fx.dump(path)
    out['TAGLINK_V1'] = sorted(r[1:] for r in fx.q(path, 'SELECT TAGLINKID, REFTYPE, REFID, TAGID FROM TAGLINK_V1'))
    return out


def base_ops():
    return [
        {'op': 'add', 'date': DAY, 'type': 'Withdrawal', 'account': BANK, 'payee': 'Diner',
         'category': 'Food:Takeaway', 'amount': 4.0, 'notes': NOTE},
        {'op': 'add', 'date': DAY, 'type': 'Deposit', 'account': BANK, 'payee': 'Diner',
         'category': 'Income:Interest', 'amount': 1.23, 'notes': NOTE, 'status': 'R'},
        {'op': 'add', 'date': DAY, 'type': 'Transfer', 'account': BANK, 'to_account': CASH_EUR,
         'amount': 50, 'notes': NOTE},
        {'op': 'add', 'date': DAY, 'type': 'Transfer', 'account': BANK, 'to_account': CASH_USD,
         'amount': 10, 'to_amount': 34.56, 'notes': NOTE},
        {'op': 'add', 'date': DAY, 'type': 'Withdrawal', 'account': BANK, 'payee': 'Selftest Payee Zz',
         'create_payee': True, 'category': 'Food:Takeaway', 'amount': 7.77, 'notes': NOTE,
         'tags': ['trip'], 'status': 'F'},
    ]


class EngineTestsBase:
    legacy = False

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='mmex_engine_test_')
        self.db = fx.build_db(os.path.join(self.tmp, 'test.mmb'), legacy=self.legacy)
        self.bak = os.path.join(self.tmp, 'bak')
        self.policy = BackupPolicy(self.bak, keep=5, min_interval_min=0)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def bal(self, account):
        conn, schema, ref = batch.read(self.db)
        try:
            return core.balance(conn, ref.account(account, 't', allow_closed=True)['id'])
        finally:
            conn.close()

    def apply_ok(self, ops, **kw):
        result = apply(self.db, {'ops': ops}, self.policy, **kw)
        self.assertTrue(result.ok, result.error)
        return result

    # -- preview ------------------------------------------------------------

    def test_preview_writes_nothing(self):
        before = fx.sha(self.db)
        result = preview(self.db, {'ops': base_ops()})
        self.assertTrue(result.ok, result.error)
        self.assertEqual(fx.sha(self.db), before)
        self.assertFalse(os.path.exists(self.db + '-journal'))
        self.assertFalse(os.path.exists(self.bak))
        self.assertEqual(len([e for e in result.events if e['kind'] == 'add']), 5)
        bank = next(b for b in result.balances if b['account'] == BANK)
        self.assertAlmostEqual(bank['after'] - bank['before'], -4 + 1.23 - 50 - 10 - 7.77, places=2)

    def test_preview_reports_refusal(self):
        result = preview(self.db, {'ops': [dict(base_ops()[0], payee='No Such Payee Qq')]})
        self.assertFalse(result.ok)
        self.assertIn('no payee "No Such Payee Qq"', result.error)

    # -- add ----------------------------------------------------------------

    def test_add_rows_shapes_balances(self):
        original = fx.dump(self.db)
        b = {a: self.bal(a) for a in (BANK, CASH_EUR, CASH_USD)}
        result = self.apply_ok(base_ops())
        self.assertTrue(result.backup_taken and os.path.isfile(result.backup))
        self.assertEqual(fx.dump(result.backup), original, 'backup holds the DB as it was before the write')
        self.assertAlmostEqual(self.bal(BANK), b[BANK] - 4 + 1.23 - 50 - 10 - 7.77, places=2)
        self.assertAlmostEqual(self.bal(CASH_EUR), b[CASH_EUR] + 50, places=2)
        self.assertAlmostEqual(self.bal(CASH_USD), b[CASH_USD] + 34.56, places=2)

        cols = [r[1] for r in sqlite3.connect(self.db).execute('PRAGMA table_info(CHECKINGACCOUNT_V1)')]
        self.assertEqual('SUBCATEGID' in cols, self.legacy)
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        rows = conn.execute('SELECT * FROM CHECKINGACCOUNT_V1 WHERE NOTES = ? ORDER BY TRANSID', (NOTE,)).fetchall()
        conn.close()
        self.assertEqual(len(rows), 5)
        now_us = time.time_ns() // 1000
        for r in rows:
            self.assertLess(abs(now_us - r['TRANSID']), 300_000_000, 'ID is creation time in UTC microseconds')
            self.assertEqual(r['TRANSDATE'], f'{DAY}T00:00:00')
            self.assertEqual((r['FOLLOWUPID'], r['COLOR'], r['DELETEDTIME'], r['TRANSACTIONNUMBER']), (-1, -1, '', ''))
            self.assertRegex(r['LASTUPDATEDTIME'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$')
            updated = datetime.datetime.strptime(r['LASTUPDATEDTIME'], '%Y-%m-%dT%H:%M:%S')
            utc_now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            self.assertLess(abs((updated - utc_now).total_seconds()), 300, 'LASTUPDATEDTIME is UTC')
            if self.legacy:
                self.assertIsNone(r['SUBCATEGID'])
            if r['TRANSCODE'] == 'Transfer':
                self.assertEqual((r['PAYEEID'], r['CATEGID']), (-1, fx.TRANSFER))
                self.assertNotEqual(r['TOACCOUNTID'], -1)
            else:
                self.assertEqual(r['TOACCOUNTID'], -1)
                self.assertEqual(r['TOTRANSAMOUNT'], r['TRANSAMOUNT'])
        tagged = fx.q(self.db, "SELECT COUNT(*) FROM TAGLINK_V1 t JOIN CHECKINGACCOUNT_V1 c ON c.TRANSID = t.REFID "
                               "WHERE c.NOTES = ? AND t.REFTYPE = 'Transaction'", NOTE)[0][0]
        self.assertEqual(tagged, 1)
        payee = fx.q(self.db, "SELECT ACTIVE, CATEGID FROM PAYEE_V1 WHERE PAYEENAME = 'Selftest Payee Zz'")
        self.assertEqual(payee, [(1, fx.TAKEAWAY)])
        self.assertEqual(result.journal['db_fingerprint'], result.db_fingerprint)
        self.assertEqual([e['op'] for e in result.journal['entries']], ['add'] * 4 + ['create_payee', 'add'])

    # -- refusals -------------------------------------------------------------

    def test_refusals_write_nothing(self):
        self.apply_ok(base_ops()[:1])
        ops = base_ops()
        cases = [
            ('duplicate add', [ops[0]], 'looks like a duplicate'),
            ('unknown payee', [dict(ops[0], payee='No Such Payee Qq', amount=1)], 'no payee'),
            ('unknown category', [dict(ops[0], category='Food:Nope', amount=2)], 'no category'),
            ('unknown account', [dict(ops[0], account='Nope (EUR)', amount=3)], 'no account named'),
            ('closed account', [dict(ops[0], account=CLOSED, amount=3)], 'is Closed'),
            ('cross-currency transfer without to_amount', [{k: v for k, v in ops[3].items() if k != 'to_amount'} | {'amount': 11}],
             'changes currency'),
            ('negative amount', [dict(ops[0], amount=-4)], 'must be positive'),
            ('bad date', [dict(ops[0], date='2099-02-30')], 'not a YYYY-MM-DD date'),
            ('unknown tag', [dict(ops[0], amount=5, tags=['nope'])], 'no tag'),
            ('failing check rolls back earlier ops', [dict(ops[0], amount=9.99),
                                                      {'op': 'check', 'account': BANK, 'as_of': DAY, 'balance': 1.0}],
             'a check failed'),
        ]
        for label, case_ops, message in cases:
            with self.subTest(label):
                snap = fx.sha(self.db)
                result = apply(self.db, {'ops': case_ops}, self.policy)
                self.assertFalse(result.ok)
                self.assertIn(message, result.error)
                self.assertEqual(fx.sha(self.db), snap)
        result = self.apply_ok([dict(ops[0], allow_duplicate=True)])
        self.assertEqual(len(result.journal['entries']), 1)

    def test_inactive_payee_and_category_warn(self):
        result = preview(self.db, {'ops': [{'op': 'add', 'date': DAY, 'type': 'Withdrawal', 'account': BANK,
                                            'payee': 'Old Shop', 'category': 'Retired', 'amount': 2}]})
        self.assertTrue(result.ok, result.error)
        self.assertEqual(len([e for e in result.events if e['kind'] == 'warning']), 2)
        self.assertTrue(any('warning: payee "Old Shop"' in line for line in result.lines))

    # -- update / delete / flag / check ---------------------------------------

    def test_update_delete_flag_check(self):
        self.apply_ok(base_ops())
        dup = self.apply_ok([dict(base_ops()[0], allow_duplicate=True)])
        ids = [r[0] for r in fx.q(self.db, 'SELECT TRANSID FROM CHECKINGACCOUNT_V1 WHERE NOTES = ? ORDER BY TRANSID', NOTE)]
        w_id, d_id, t_id, x_id, p_id, dup_id = ids
        bad = apply(self.db, {'ops': [{'op': 'update', 'id': w_id, 'expect': {'amount': 5}, 'set': {'amount': 6}}]}, self.policy)
        self.assertFalse(bad.ok)
        self.assertIn('expected amount', bad.error)
        final = round(self.bal(BANK) - 0.5 - 1.23 + 4.0, 2)
        result = self.apply_ok([
            {'op': 'update', 'id': w_id, 'expect': {'amount': 4.0, 'payee': 'Diner'},
             'set': {'amount': 4.5, 'notes': NOTE + ' edited', 'tags': ['trip']}},
            {'op': 'update', 'id': x_id, 'set': {'notes': NOTE + ' xfer edited'}},
            {'op': 'delete', 'id': d_id, 'expect': {'type': 'Deposit'}},
            {'op': 'delete', 'id': dup_id},
            {'op': 'flag', 'account': BANK, 'from': DAY, 'to': DAY, 'status': 'R'},
            {'op': 'check', 'account': BANK, 'as_of': DAY, 'balance': final},
        ])
        self.assertTrue(any(line.startswith('PASS check') for line in result.lines))
        self.assertAlmostEqual(self.bal(BANK), final, places=2)
        self.assertEqual(fx.q(self.db, 'SELECT TOTRANSAMOUNT FROM CHECKINGACCOUNT_V1 WHERE TRANSID = ?', x_id)[0][0], 34.56,
                         'a notes-only update keeps the cross-currency to_amount')
        self.assertNotEqual(fx.q(self.db, 'SELECT DELETEDTIME FROM CHECKINGACCOUNT_V1 WHERE TRANSID = ?', d_id)[0][0], '')
        statuses = dict(fx.q(self.db, 'SELECT TRANSID, STATUS FROM CHECKINGACCOUNT_V1 WHERE TRANSID IN (?,?,?,?)',
                             w_id, t_id, x_id, p_id))
        self.assertEqual((statuses[w_id], statuses[t_id], statuses[x_id], statuses[p_id]), ('R', 'R', 'R', 'F'))
        update = next(e for e in result.events if e['kind'] == 'update')
        self.assertEqual(sorted(update['changed']), ['amount', 'notes', 'tags'])
        self.assertIn(dup.journal['entries'][0]['row']['TRANSID'], [dup_id])

    def test_balance_excludes_void_and_counts_incoming_transfers(self):
        # seed: 1000 - 12.50 - 8.00 - 100 + 3.21; the 50.00 Void row doesn't count
        self.assertAlmostEqual(self.bal(BANK), 882.71, places=2)
        self.assertAlmostEqual(self.bal(CASH_EUR), 100.0, places=2)

    # -- undo -------------------------------------------------------------------

    def test_undo(self):
        original = fx.dump(self.db)
        j1 = self.apply_ok(base_ops()).journal
        ids = [r[0] for r in fx.q(self.db, 'SELECT TRANSID FROM CHECKINGACCOUNT_V1 WHERE NOTES = ? ORDER BY TRANSID', NOTE)]
        j2 = self.apply_ok([{'op': 'update', 'id': ids[0], 'set': {'amount': 4.5, 'tags': ['reno']}},
                            {'op': 'delete', 'id': ids[1]},
                            {'op': 'flag', 'account': BANK, 'from': DAY, 'to': DAY, 'status': 'R'}]).journal
        snap = fx.sha(self.db)
        dry = undo(self.db, j2)
        self.assertTrue(dry.ok, dry.error)
        self.assertEqual(fx.sha(self.db), snap, 'undo preview writes nothing')
        self.assertTrue(undo(self.db, j2, backups=self.policy, commit=True).ok)
        conn = sqlite3.connect(self.db)
        conn.execute('UPDATE CHECKINGACCOUNT_V1 SET NOTES = ? WHERE TRANSID = ?', ('changed in MMEX', ids[0]))
        conn.commit()
        conn.close()
        snap = fx.sha(self.db)
        refused = undo(self.db, j1, backups=self.policy, commit=True)
        self.assertFalse(refused.ok)
        self.assertIn('changed since', refused.error)
        self.assertEqual(fx.sha(self.db), snap)
        conn = sqlite3.connect(self.db)
        conn.execute('UPDATE CHECKINGACCOUNT_V1 SET NOTES = ? WHERE TRANSID = ?', (NOTE, ids[0]))
        conn.commit()
        conn.close()
        self.assertTrue(undo(self.db, j1, backups=self.policy, commit=True).ok)
        self.assertEqual(fx.dump(self.db), original, 'after undoing everything the DB matches the original')
        self.assertFalse(undo(self.db, dict(j1, undone_at_utc='2099-01-01T00:00:00')).ok)

    def test_undo_backup_interval(self):
        j = self.apply_ok(base_ops()).journal
        policy = BackupPolicy(self.bak, keep=5, min_interval_min=30)
        before = policy.existing(self.db)
        result = undo(self.db, j, backups=policy, commit=True, force_backup=False)
        self.assertTrue(result.ok, result.error)
        self.assertFalse(result.backup_taken, 'a backup minutes old covers the undo')
        self.assertEqual(policy.existing(self.db), before)
        redo = undo(self.db, result.journal, backups=policy, commit=True)
        self.assertTrue(redo.backup_taken, 'by default undo still backs up first')

    def test_update_to_a_new_payee(self):
        self.apply_ok(base_ops())
        w_id = fx.q(self.db, 'SELECT TRANSID FROM CHECKINGACCOUNT_V1 WHERE NOTES = ? ORDER BY TRANSID', NOTE)[0][0]
        bad = apply(self.db, {'ops': [{'op': 'update', 'id': w_id, 'set': {'payee': 'Update Payee Zz'}}]}, self.policy)
        self.assertFalse(bad.ok)
        self.assertIn('create_payee', bad.error)
        result = self.apply_ok([{'op': 'update', 'id': w_id, 'set': {'payee': 'Update Payee Zz'}, 'create_payee': True}])
        self.assertTrue(any('new payee "Update Payee Zz"' in line for line in result.lines))
        payee_id = fx.q(self.db, 'SELECT PAYEEID FROM PAYEE_V1 WHERE PAYEENAME = ?', 'Update Payee Zz')[0][0]
        self.assertEqual(fx.q(self.db, 'SELECT PAYEEID FROM CHECKINGACCOUNT_V1 WHERE TRANSID = ?', w_id)[0][0], payee_id)
        self.assertEqual([e['op'] for e in result.journal['entries']], ['create_payee', 'update'])

    def test_undo_then_redo_then_undo(self):
        """An undo's own journal reverses it (redo), and that one's reverses
        the redo: every state in the chain matches the one before."""
        original = state(self.db)
        j_add = self.apply_ok(base_ops() + [
            {'op': 'add', 'date': DAY, 'type': 'Deposit', 'account': CASH_EUR, 'payee': 'Redo Payee Qq',
             'create_payee': True, 'category': 'Income:Interest', 'amount': 3.0, 'notes': NOTE,
             'tags': ['RedoTag2099', 'trip'], 'create_tags': True}]).journal
        after_add = state(self.db)
        ids = [r[0] for r in fx.q(self.db, 'SELECT TRANSID FROM CHECKINGACCOUNT_V1 WHERE NOTES = ? ORDER BY TRANSID', NOTE)]
        j_edit = self.apply_ok([{'op': 'update', 'id': ids[0], 'set': {'amount': 9.5, 'tags': ['reno']}},
                                {'op': 'delete', 'id': ids[1]},
                                {'op': 'flag', 'account': BANK, 'from': DAY, 'to': DAY, 'status': 'R'}]).journal
        after_edit = state(self.db)

        u1 = undo(self.db, j_edit, backups=self.policy, commit=True)
        self.assertTrue(u1.ok, u1.error)
        self.assertEqual(state(self.db), after_add)
        r1 = undo(self.db, u1.journal, backups=self.policy, commit=True)       # redo the edit
        self.assertTrue(r1.ok, r1.error)
        self.assertEqual(state(self.db), after_edit)
        self.assertTrue(undo(self.db, r1.journal, backups=self.policy, commit=True).ok)
        self.assertEqual(state(self.db), after_add)

        u2 = undo(self.db, j_add, backups=self.policy, commit=True)
        self.assertTrue(u2.ok, u2.error)
        self.assertEqual(state(self.db), original)
        self.assertEqual(fx.q(self.db, "SELECT COUNT(*) FROM TAG_V1 WHERE TAGNAME = 'RedoTag2099'")[0][0], 0)
        r2 = undo(self.db, u2.journal, backups=self.policy, commit=True)       # redo the adds, payee and tag too
        self.assertTrue(r2.ok, r2.error)
        self.assertEqual(state(self.db), after_add)
        self.assertEqual(fx.q(self.db, "SELECT COUNT(*) FROM TAG_V1 WHERE TAGNAME = 'RedoTag2099'")[0][0], 1)
        self.assertTrue(undo(self.db, r2.journal, backups=self.policy, commit=True).ok)
        self.assertEqual(state(self.db), original)

        # A redo refuses when the row it would put back has reappeared.
        conn = sqlite3.connect(self.db)
        conn.execute('INSERT INTO CHECKINGACCOUNT_V1 (TRANSID, ACCOUNTID, PAYEEID, TRANSCODE, TRANSAMOUNT, TRANSDATE) '
                     "VALUES (?, 1, -1, 'Withdrawal', 1, '2099-01-01')", (ids[0],))
        conn.commit()
        conn.close()
        refused = undo(self.db, u2.journal, backups=self.policy, commit=True)
        self.assertFalse(refused.ok)
        self.assertIn('exists again', refused.error)

    def test_change_type(self):
        w = self.apply_ok(base_ops()[:1]).events[0]['row']['id']        # Withdrawal 4.00 Bank, Diner
        bank = self.bal(BANK)
        self.apply_ok([{'op': 'update', 'id': w, 'set': {'type': 'Deposit'}}])
        self.assertAlmostEqual(self.bal(BANK), bank + 8.0)
        # To Transfer: needs to_account; the payee goes, TOTRANSAMOUNT follows the amount.
        r = apply(self.db, {'ops': [{'op': 'update', 'id': w, 'set': {'type': 'Transfer'}}]}, self.policy)
        self.assertFalse(r.ok)
        self.assertIn('needs to_account', r.error)
        cash = self.bal(CASH_EUR)
        self.apply_ok([{'op': 'update', 'id': w, 'set': {'type': 'Transfer', 'to_account': CASH_EUR}}])
        row = fx.q(self.db, 'SELECT TRANSCODE, PAYEEID, TOACCOUNTID, TOTRANSAMOUNT FROM CHECKINGACCOUNT_V1 WHERE TRANSID = ?', w)[0]
        self.assertEqual(row, ('Transfer', -1, fx.ACCOUNT_IDS[CASH_EUR], 4.0))
        self.assertAlmostEqual(self.bal(CASH_EUR), cash + 4.0)
        # Across currencies, to_amount is required.
        r = apply(self.db, {'ops': [{'op': 'update', 'id': w, 'set': {'to_account': CASH_USD}}]}, self.policy)
        self.assertFalse(r.ok)
        # Back to Withdrawal: needs a payee; to_account is dropped.
        r = apply(self.db, {'ops': [{'op': 'update', 'id': w, 'set': {'type': 'Withdrawal'}}]}, self.policy)
        self.assertFalse(r.ok)
        self.assertIn('needs a payee', r.error)
        self.apply_ok([{'op': 'update', 'id': w, 'set': {'type': 'Withdrawal', 'payee': 'Diner', 'category': 'Food:Takeaway'}}])
        row = fx.q(self.db, 'SELECT TRANSCODE, PAYEEID, TOACCOUNTID, TOTRANSAMOUNT FROM CHECKINGACCOUNT_V1 WHERE TRANSID = ?', w)[0]
        self.assertEqual(row, ('Withdrawal', fx.DINER, -1, 4.0))
        self.assertAlmostEqual(self.bal(BANK), bank)
        self.assertAlmostEqual(self.bal(CASH_EUR), cash)

    def test_create_tags_and_undo(self):
        add = {'op': 'add', 'date': DAY, 'type': 'Withdrawal', 'account': BANK, 'payee': 'Diner',
               'category': 'Food:Takeaway', 'amount': 2.0, 'notes': NOTE, 'tags': ['trip', 'NewTag2099']}
        refused = preview(self.db, {'ops': [add]})
        self.assertFalse(refused.ok)
        self.assertIn('no tag "NewTag2099"', refused.error)
        for bad in ('two words', '&x', '|x', 'a,b', ''):
            r = preview(self.db, {'ops': [dict(add, tags=[bad], create_tags=True)]})
            self.assertFalse(r.ok, bad)
        j = self.apply_ok([dict(add, create_tags=True)]).journal
        self.assertEqual(fx.q(self.db, "SELECT COUNT(*) FROM TAG_V1 WHERE TAGNAME = 'NewTag2099'")[0][0], 1)
        # 'newtag2099' is the same tag (TAGNAME is COLLATE NOCASE): no second one.
        self.apply_ok([dict(add, amount=3.0, tags=['newtag2099'], create_tags=True)])
        self.assertEqual(fx.q(self.db, "SELECT COUNT(*) FROM TAG_V1 WHERE TAGNAME = 'NewTag2099'")[0][0], 1)
        r = undo(self.db, j, backups=self.policy, commit=True)
        self.assertTrue(r.ok, r.error)
        self.assertIn('kept tag "NewTag2099" (now used by 1 row(s))', r.lines)

    def test_expect_mismatch_is_stale(self):
        result = self.apply_ok(base_ops()[:1])
        trans_id = result.events[0]['row']['id']
        r = apply(self.db, {'ops': [{'op': 'update', 'id': trans_id, 'expect': {'amount': 9.99}, 'set': {'notes': 'x'}}]},
                  self.policy)
        self.assertFalse(r.ok)
        self.assertTrue(r.stale)
        r = apply(self.db, {'ops': [{'op': 'update', 'id': trans_id, 'set': {'payee': 'Nobody'}}]}, self.policy)
        self.assertFalse(r.ok)
        self.assertFalse(r.stale)

    def test_undo_refuses_another_db(self):
        journal = self.apply_ok(base_ops()[:1]).journal
        other = fx.build_db(os.path.join(self.tmp, 'other.mmb'), legacy=not self.legacy)
        result = undo(other, journal, backups=self.policy, commit=True)
        self.assertFalse(result.ok)
        self.assertIn('different DB', result.error)

    # -- guards ---------------------------------------------------------------

    def test_in_use_and_conflict_guards(self):
        fx.set_info(self.db, 'ISUSED', 'TRUE')
        snap = fx.sha(self.db)
        with self.assertRaises(GuardError):
            apply(self.db, {'ops': base_ops()[:1]}, self.policy)
        self.assertEqual(fx.sha(self.db), snap)
        result = self.apply_ok(base_ops()[:1], refuse_on=())
        self.assertIn('ISUSED', result.warnings[0])
        fx.set_info(self.db, 'ISUSED', 'FALSE')
        open(os.path.join(self.tmp, 'test.sync-conflict-20990101-000000-ABCDEFG.mmb'), 'w').close()
        with self.assertRaises(GuardError):
            apply(self.db, {'ops': [dict(base_ops()[0], date='2099-03-02')]}, self.policy)
        self.assertIn('conflict', preview(self.db, {'ops': base_ops()[:1]}).warnings[0])

    def test_expect_fingerprint(self):
        fp = preview(self.db, {'ops': base_ops()[:1]}).db_fingerprint
        with self.assertRaises(GuardError):
            apply(self.db, {'ops': base_ops()[:1]}, self.policy, expect_fingerprint='0' * 16)
        self.assertTrue(apply(self.db, {'ops': base_ops()[:1]}, self.policy, expect_fingerprint=fp).ok)

    # -- schema -----------------------------------------------------------------

    def test_unsupported_version_refused(self):
        old = fx.build_db(os.path.join(self.tmp, 'v20.mmb'), legacy=self.legacy, version=20)
        snap = fx.sha(old)
        with self.assertRaises(SchemaError):
            preview(old, {'ops': base_ops()[:1]})
        with self.assertRaises(SchemaError):
            apply(old, {'ops': base_ops()[:1]}, self.policy)
        self.assertEqual(fx.sha(old), snap)
        self.assertFalse(os.path.exists(self.bak), 'no backup for a refused DB')

    def test_missing_column_refused(self):
        conn = sqlite3.connect(self.db)
        conn.execute('ALTER TABLE CHECKINGACCOUNT_V1 DROP COLUMN COLOR')
        conn.commit()
        conn.close()
        with self.assertRaisesRegex(SchemaError, 'lacks'):
            apply(self.db, {'ops': base_ops()[:1]}, self.policy)

    def test_wal_refused(self):
        conn = sqlite3.connect(self.db)
        conn.execute('PRAGMA journal_mode = WAL')
        conn.close()
        with self.assertRaisesRegex(SchemaError, 'WAL'):
            apply(self.db, {'ops': base_ops()[:1]}, self.policy)


class FreshSchemaTests(EngineTestsBase, unittest.TestCase):
    legacy = False


class LegacySchemaTests(EngineTestsBase, unittest.TestCase):
    """The shape of a DB upgraded from older MMEX versions (extra SUBCATEGID columns)."""
    legacy = True


if __name__ == '__main__':
    unittest.main()
