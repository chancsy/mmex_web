"""The pages that write: add, edit, delete, history and undo.

    python -m unittest tests.test_entry -v
"""

import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import unittest

from tests import fixtures as fx
from tests.fixtures import ACCOUNT_IDS, BANK, CASH_USD, CASH_EUR
from tests.test_web import PASSWORD, csrf
from web import create_app, secrets_file
from web.config import Settings

BANK_ID, CASH_EUR_ID, CASH_USD_ID = ACCOUNT_IDS[BANK], ACCOUNT_IDS[CASH_EUR], ACCOUNT_IDS[CASH_USD]


class EntryTestsBase(unittest.TestCase):
    backups = True

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='mmex_web_entry_')
        self.db = fx.build_db(os.path.join(self.tmp, 'money.mmb'), legacy=True)
        self.data = os.path.join(self.tmp, 'data')
        self.bak = os.path.join(self.tmp, 'bak')
        secrets_file.create_or_update(os.path.join(self.data, 'secrets.json'), PASSWORD)
        self.settings = Settings(db_path=self.db, data_dir=self.data, cookie_secure=False,
                                 backup_dir=self.bak if self.backups else '', backup_min_interval_min=0)
        self.app = create_app(self.settings)
        self.client = self.app.test_client()
        page = self.client.get('/login')
        self.client.post('/login', data={'password': PASSWORD, 'csrf_token': csrf(page.text)})

    def tearDown(self):
        for root, dirs, files in os.walk(self.tmp):
            for name in dirs + files:
                os.chmod(os.path.join(root, name), 0o700)
        os.chmod(self.tmp, 0o700)
        shutil.rmtree(self.tmp)

    def token(self):
        return csrf(self.client.get('/').text)

    def add(self, **fields):
        data = {'type': 'Withdrawal', 'account': str(BANK_ID), 'payee': 'Diner', 'category': 'Food:Takeaway',
                'amount': '4.50', 'date': '2099-03-01', 'notes': '', 'status': ''}
        data.update(fields)
        data['csrf_token'] = self.token()
        return self.client.post('/new', data=data)

    def rows(self, where='1', *args):
        return fx.q(self.db, f'SELECT TRANSID, TRANSCODE, TRANSAMOUNT, TOTRANSAMOUNT, NOTES, STATUS, DELETEDTIME, '
                             f'PAYEEID, TRANSDATE FROM CHECKINGACCOUNT_V1 WHERE {where} ORDER BY TRANSID', *args)

    def newest(self):
        return self.rows("NOTES <> 'seed'")[-1]


class ReadOnlyTests(EntryTestsBase):
    backups = False

    def test_no_backup_dir_means_read_only(self):
        page = self.client.get('/')
        self.assertIn('MMEX_BACKUP_DIR', page.text)
        self.assertNotIn('+ New', page.text)
        self.assertEqual(self.client.get('/new').status_code, 403)
        before = fx.sha(self.db)
        self.assertEqual(self.add().status_code, 403)
        self.assertEqual(fx.sha(self.db), before)


class ReadOnlyFileTests(EntryTestsBase):
    def test_unwritable_db_means_read_only(self):
        os.chmod(self.db, stat.S_IRUSR)
        if os.access(self.db, os.W_OK):
            self.skipTest('running as root: file permissions are not enforced')
        page = self.client.get('/account/1')
        self.assertIn('isn&#39;t writable', page.text)
        self.assertEqual(self.client.get('/new').status_code, 403)


class EntryTests(EntryTestsBase):
    def test_form_renders_with_pickers(self):
        page = self.client.get(f'/new?account={CASH_EUR_ID}')
        self.assertEqual(page.status_code, 200)
        self.assertIn('<option value="Diner">', page.text)
        self.assertIn('<option value="Food:Takeaway">', page.text)
        self.assertNotIn('<option value="Retired">', page.text)      # inactive category
        self.assertNotIn('<option value="Old Shop">', page.text)     # inactive payee
        self.assertIn(f'<option value="{CASH_EUR_ID}" selected>', page.text)
        self.assertIn('value="trip"', page.text)
        data = json.loads(re.search(r'id="entry-data">(.*?)</script>', page.text, re.S).group(1))
        self.assertEqual(data['payees']['diner'], 'Food:Takeaway')
        self.assertNotIn(str(ACCOUNT_IDS['Old Card']), data['currency'])   # closed account not offered

    def test_add_withdrawal(self):
        r = self.add(notes='lunch', tags='trip', status='F')
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers['Location'], f'/account/{BANK_ID}')
        row = self.newest()
        self.assertEqual(row[1:6], ('Withdrawal', 4.5, 4.5, 'lunch', 'F'))
        self.assertEqual(len(os.listdir(self.bak)), 1)
        history = self.client.get('/history').text
        self.assertIn('Added 2099-03-01 Withdrawal', history)
        self.assertIn('#trip', self.client.get(f'/account/{BANK_ID}').text)
        recent = self.client.get('/recent').text
        self.assertIn('<span class="src">app</span>', recent)
        self.assertIn('<span class="src">other</span>', recent)

    def test_save_and_add_another_keeps_account_type_date(self):
        r = self.add(save_new='1', type='Deposit', category='Income:Interest')
        self.assertEqual(r.status_code, 302)
        self.assertIn(f'account={BANK_ID}', r.headers['Location'])
        self.assertIn('type=Deposit', r.headers['Location'])
        self.assertIn('date=2099-03-01', r.headers['Location'])

    def test_next_returns_to_the_page(self):
        r = self.add(next='/account/1?status=F')
        self.assertEqual(r.headers['Location'], '/account/1?status=F')
        r = self.add(next='//evil.example/', amount='5.55')
        self.assertEqual(r.headers['Location'], '/')

    def test_form_errors_write_nothing(self):
        before = fx.sha(self.db)
        cases = [({'amount': 'abc'}, 'positive amount'), ({'amount': '0'}, 'more than zero'),
                 ({'amount': '1.234'}, 'up to 2 decimals'), ({'date': ''}, 'pick a date'),
                 ({'payee': ''}, 'enter a payee'), ({'category': 'Nope'}, 'No category'),
                 ({'payee': 'Brand New Cafe'}, 'Create new payee'), ({'account': '999'}, 'pick an account'),
                 ({'account': str(ACCOUNT_IDS['Old Card'])}, 'is Closed'),
                 ({'new_tags': '&bad'}, 'not a valid tag name'), ({'status': 'V'}, 'Status: pick Unflagged')]
        for fields, message in cases:
            r = self.add(notes='typed <b>notes</b>', **fields)
            self.assertEqual(r.status_code, 422, fields)
            text = r.text.replace('&#34;', '"').replace('&#39;', "'")
            self.assertIn(message, text, fields)
            self.assertIn('typed &lt;b&gt;notes&lt;/b&gt;', r.text, fields)   # the form keeps what was typed
        self.assertEqual(fx.sha(self.db), before)

    def test_new_payee_and_tag(self):
        r = self.add(payee='Brand New Cafe', create_payee='1', new_tags='2099Trip', tags='trip')
        self.assertEqual(r.status_code, 302)
        self.assertEqual(fx.q(self.db, "SELECT COUNT(*) FROM PAYEE_V1 WHERE PAYEENAME = 'Brand New Cafe'")[0][0], 1)
        self.assertEqual(fx.q(self.db, "SELECT COUNT(*) FROM TAG_V1 WHERE TAGNAME = '2099Trip'")[0][0], 1)
        self.assertIn('#2099Trip', self.client.get(f'/account/{BANK_ID}').text)

    def test_near_duplicate_needs_confirmation(self):
        self.assertEqual(self.add().status_code, 302)
        r = self.add(date='2099-03-03', payee='Old Shop')
        self.assertEqual(r.status_code, 200)
        self.assertIn('Possible duplicate', r.text)
        self.assertIn('name="confirm_duplicate"', r.text)
        self.assertEqual(len(self.rows("NOTES <> 'seed'")), 1)
        # Same day and payee: the engine's own duplicate rule, also overridden by confirming.
        r = self.add(confirm_duplicate='1')
        self.assertEqual(r.status_code, 302)
        self.assertEqual(len(self.rows("NOTES <> 'seed'")), 2)
        # Five days away is not a near duplicate.
        self.assertEqual(self.add(date='2099-03-06').status_code, 302)

    def test_transfers(self):
        r = self.add(type='Transfer', to_account=str(CASH_EUR_ID), payee='', category='', amount='20')
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.newest()[1:4], ('Transfer', 20.0, 20.0))
        r = self.add(type='Transfer', to_account=str(CASH_USD_ID), payee='', category='', amount='10')
        self.assertEqual(r.status_code, 422)
        self.assertIn('Amount received', r.text)
        r = self.add(type='Transfer', to_account=str(CASH_USD_ID), payee='', category='', amount='10', to_amount='33.10')
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.newest()[1:4], ('Transfer', 10.0, 33.1))
        r = self.add(type='Transfer', to_account=str(BANK_ID), payee='', category='')
        self.assertIn('two different accounts', r.text)

    def edit_page(self, trans_id):
        page = self.client.get(f'/txn/{trans_id}/edit')
        self.assertEqual(page.status_code, 200)
        orig = re.search(r'name="orig" value="([^"]+)"', page.text).group(1)
        return page, orig.replace('&#34;', '"').replace('&amp;', '&').replace('&#39;', "'")

    def post_edit(self, trans_id, orig, **fields):
        data = {'type': 'Withdrawal', 'account': str(BANK_ID), 'payee': 'Diner', 'category': 'Food:Takeaway',
                'amount': '4.50', 'date': '2099-03-01', 'notes': '', 'status': '', 'orig': orig,
                'csrf_token': self.token()}
        data.update(fields)
        return self.client.post(f'/txn/{trans_id}/edit', data=data)

    def test_edit(self):
        self.add(tags='trip')
        trans_id = self.newest()[0]
        page, orig = self.edit_page(trans_id)
        self.assertIn('value="4.50"', page.text)
        self.assertNotIn('name="save_new"', page.text)
        r = self.post_edit(trans_id, orig, amount='5.25', notes='fixed', tags='trip')
        self.assertEqual(r.status_code, 302)
        row = self.newest()
        self.assertEqual((row[2], row[4]), (5.25, 'fixed'))
        self.assertIn('Saved changes to amount, notes', self.client.get('/').text)
        # Unchanged form: nothing written.
        page, orig = self.edit_page(trans_id)
        before = fx.sha(self.db)
        r = self.post_edit(trans_id, orig, amount='5.25', notes='fixed', tags='trip')
        self.assertEqual(r.status_code, 200)
        self.assertIn('Nothing changed, so nothing was saved', r.text)
        self.assertIn('value="5.25"', r.text)
        self.assertEqual(fx.sha(self.db), before)
        # Tags off, status Void.
        r = self.post_edit(trans_id, orig, amount='5.25', notes='fixed', status='V')
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.newest()[5], 'V')
        self.assertEqual(fx.q(self.db, 'SELECT COUNT(*) FROM TAGLINK_V1 WHERE REFID = ?', trans_id)[0][0], 0)

    def test_edit_to_a_new_payee(self):
        self.add()
        trans_id = self.newest()[0]
        page, orig = self.edit_page(trans_id)
        before = fx.sha(self.db)
        r = self.post_edit(trans_id, orig, payee='Brand New Stall')
        self.assertEqual(r.status_code, 422)
        self.assertIn('Create new payee', r.text)
        self.assertEqual(fx.sha(self.db), before)
        r = self.post_edit(trans_id, orig, payee='Brand New Stall', create_payee='1')
        self.assertEqual(r.status_code, 302)
        payee_id = fx.q(self.db, 'SELECT PAYEEID FROM PAYEE_V1 WHERE PAYEENAME = ?', 'Brand New Stall')[0][0]
        self.assertEqual(self.newest()[7], payee_id)

    def test_edit_refuses_a_stale_form(self):
        self.add()
        trans_id = self.newest()[0]
        _, orig = self.edit_page(trans_id)
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE CHECKINGACCOUNT_V1 SET NOTES = 'changed on the desktop' WHERE TRANSID = ?", (trans_id,))
        conn.commit()
        conn.close()
        r = self.post_edit(trans_id, orig, amount='9.99')
        self.assertEqual(r.status_code, 409)
        self.assertIn('changed since you opened it', r.text)
        self.assertEqual(self.newest()[2], 4.5)
        r = self.client.post(f'/txn/{trans_id}/delete', data={'orig': orig, 'csrf_token': self.token()})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.newest()[6], '')

    def test_edit_cross_currency_transfer(self):
        self.add(type='Transfer', to_account=str(CASH_USD_ID), payee='', category='', amount='10', to_amount='33.10')
        trans_id = self.newest()[0]
        _, orig = self.edit_page(trans_id)
        r = self.post_edit(trans_id, orig, type='Transfer', to_account=str(CASH_USD_ID), payee='', category='Transfer',
                           amount='10', to_amount='34.00')
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.newest()[2:4], (10.0, 34.0))

    def test_edit_changes_type(self):
        self.add()
        trans_id = self.newest()[0]
        page, orig = self.edit_page(trans_id)
        self.assertIn('name="type" value="Transfer"', page.text)
        r = self.post_edit(trans_id, orig, type='Transfer', to_account='')
        self.assertEqual(r.status_code, 422)
        self.assertIn('To account: pick an account', r.text)
        r = self.post_edit(trans_id, orig, type='Transfer', to_account=str(CASH_EUR_ID), payee='', category='')
        self.assertEqual(r.status_code, 302)
        row = fx.q(self.db, 'SELECT TRANSCODE, PAYEEID, TOACCOUNTID, CATEGID FROM CHECKINGACCOUNT_V1 WHERE TRANSID = ?', trans_id)[0]
        self.assertEqual(row, ('Transfer', -1, CASH_EUR_ID, fx.TRANSFER))
        page, orig = self.edit_page(trans_id)
        r = self.post_edit(trans_id, orig, type='Deposit', payee='', category='Income:Interest')
        self.assertEqual(r.status_code, 422)
        self.assertIn('enter a payee', r.text)
        r = self.post_edit(trans_id, orig, type='Deposit', payee='Diner', category='Income:Interest')
        self.assertEqual(r.status_code, 302)
        row = fx.q(self.db, 'SELECT TRANSCODE, PAYEEID, TOACCOUNTID, CATEGID FROM CHECKINGACCOUNT_V1 WHERE TRANSID = ?', trans_id)[0]
        self.assertEqual(row, ('Deposit', fx.DINER, -1, fx.INTEREST))
        # The whole chain undoes.
        store = self.app.config['MMEX_STORE']
        for w in store.list()[:2]:
            self.client.post(f'/history/{w["id"]}/undo', data={'csrf_token': self.token()})
        self.assertEqual(fx.q(self.db, 'SELECT TRANSCODE, PAYEEID FROM CHECKINGACCOUNT_V1 WHERE TRANSID = ?', trans_id)[0],
                         ('Withdrawal', fx.DINER))

    def test_delete_and_undo(self):
        self.add()
        trans_id = self.newest()[0]
        page = self.client.get(f'/txn/{trans_id}/delete')
        self.assertIn('Delete this transaction?', page.text)
        orig = re.search(r'name="orig" value="([^"]+)"', page.text).group(1).replace('&#34;', '"')
        r = self.client.post(f'/txn/{trans_id}/delete', data={'orig': orig, 'csrf_token': csrf(page.text)})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(self.newest()[6])
        self.assertIn("In MMEX's trash", self.client.get(f'/txn/{trans_id}/edit').text.replace('&#39;', "'"))
        store = self.app.config['MMEX_STORE']
        delete_write = store.list()[0]
        self.assertEqual(delete_write['action'], 'delete')
        page = self.client.get(f'/history/{delete_write["id"]}/undo')
        self.assertIn('restored row', page.text)
        r = self.client.post(f'/history/{delete_write["id"]}/undo', data={'csrf_token': csrf(page.text)})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.newest()[6], '')
        self.assertTrue(store.get(delete_write['id'])['undone_at_utc'])
        self.assertIn('Already reversed', self.client.get(f'/history/{delete_write["id"]}/undo').text)
        undo_write = store.list()[0]
        self.assertEqual(undo_write['action'], 'undo')
        # Redo: undoing the undo deletes it again; undoing the redo restores it again.
        page = self.client.get(f'/history/{undo_write["id"]}/undo')
        self.assertIn('Redo this change?', page.text)
        self.client.post(f'/history/{undo_write["id"]}/undo', data={'csrf_token': csrf(page.text)})
        self.assertTrue(self.newest()[6])
        redo_write = store.list()[0]
        self.assertEqual((redo_write['action'], redo_write['summary'][:7]), ('redo', 'Redid: '))
        self.assertIn('redone', self.client.get('/history').text)
        self.client.post(f'/history/{redo_write["id"]}/undo', data={'csrf_token': self.token()})
        self.assertEqual(self.newest()[6], '')
        self.assertEqual(store.list()[0]['action'], 'undo')

    def test_undo_add_refuses_after_a_later_edit(self):
        self.add()
        trans_id = self.newest()[0]
        add_write = self.app.config['MMEX_STORE'].list()[0]
        _, orig = self.edit_page(trans_id)
        self.post_edit(trans_id, orig, amount='6.00')
        page = self.client.get(f'/history/{add_write["id"]}/undo')
        self.assertIn("Can't undo: transaction", page.text)
        self.assertIn('has been changed since', page.text)
        self.assertNotIn('TRANSAMOUNT', page.text)
        r = self.client.post(f'/history/{add_write["id"]}/undo', data={'csrf_token': csrf(page.text)})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(len(self.rows("NOTES <> 'seed'")), 1)
        # Undo the edit first, then the add.
        store = self.app.config['MMEX_STORE']
        edit_write = store.list()[0]
        self.client.post(f'/history/{edit_write["id"]}/undo', data={'csrf_token': self.token()})
        self.client.post(f'/history/{add_write["id"]}/undo', data={'csrf_token': self.token()})
        self.assertEqual(len(self.rows("NOTES <> 'seed'")), 0)

    def test_writes_need_csrf_and_login(self):
        r = self.client.post('/new', data={'type': 'Withdrawal', 'amount': '1'})
        self.assertEqual(r.status_code, 400)
        other = self.app.test_client()
        self.assertEqual(other.get('/new').status_code, 302)
        self.assertEqual(other.get('/history').status_code, 302)

    def test_edit_link_in_register(self):
        self.add()
        trans_id = self.newest()[0]
        page = self.client.get(f'/account/{BANK_ID}?status=all')
        self.assertIn(f'href="/txn/{trans_id}/edit?next=/account/{BANK_ID}?status%3Dall"', page.text)


if __name__ == '__main__':
    unittest.main()


class TwinTests(EntryTestsBase):
    """The Twin type: one purchase as a Transfer to the To account plus a
    Withdrawal with the real payee in the expense account."""

    HOUSEHOLD_ID = 5

    def setUp(self):
        super().setUp()
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO ACCOUNTLIST_V1 (ACCOUNTID, ACCOUNTNAME, ACCOUNTTYPE, STATUS, FAVORITEACCT, CURRENCYID, "
                     "INITIALBAL, INITIALDATE) SELECT 5, 'Household', 'Cash', 'Open', 'FALSE', CURRENCYID, 0, '2026-01-01' "
                     "FROM ACCOUNTLIST_V1 WHERE ACCOUNTID = ?", (BANK_ID,))
        conn.commit()
        conn.close()

    def twin(self, **fields):
        data = {'type': 'Twin', 'account': str(BANK_ID), 'to_account': str(CASH_EUR_ID),
                'expense_account': str(self.HOUSEHOLD_ID), 'payee': 'Walmart', 'create_payee': '1',
                'category': 'Food:Takeaway', 'amount': '8.90', 'date': '2099-03-01', 'notes': '', 'status': ''}
        data.update(fields)
        return self.add(**data)

    def test_saves_both_rows_and_one_undo_removes_both(self):
        page = self.client.get('/new?type=Twin').text
        self.assertIn('value="Twin" checked', page)
        self.assertIn('name="expense_account"', page)
        before = len(self.rows())
        r = self.twin()
        self.assertEqual(r.status_code, 302)
        rows = fx.q(self.db, 'SELECT TRANSCODE, ACCOUNTID, TOACCOUNTID, TRANSAMOUNT, TOTRANSAMOUNT, NOTES, STATUS, '
                             'substr(TRANSDATE, 1, 10) FROM CHECKINGACCOUNT_V1 ORDER BY TRANSID DESC LIMIT 2')
        self.assertEqual(sorted(rows), sorted([
            ('Transfer', BANK_ID, CASH_EUR_ID, 8.9, 8.9, 'Walmart, Household -> Cash (EUR)', '', '2099-03-01'),
            ('Withdrawal', self.HOUSEHOLD_ID, -1, 8.9, 8.9, '', '', '2099-03-01'),
        ]))
        payee = fx.q(self.db, "SELECT p.PAYEENAME FROM CHECKINGACCOUNT_V1 t JOIN PAYEE_V1 p ON p.PAYEEID = t.PAYEEID "
                              "WHERE t.ACCOUNTID = ? ORDER BY t.TRANSID DESC LIMIT 1", self.HOUSEHOLD_ID)
        self.assertEqual(payee, [('Walmart',)])
        store = self.app.config['MMEX_STORE']
        self.assertEqual(store.count(), 1)                                   # one History entry
        self.assertIn('Saved twin: 2099-03-01 Walmart 8.90', self.client.get('/').text)
        w = store.list()[0]
        self.client.post(f'/history/{w["id"]}/undo', data={'csrf_token': self.token()})
        self.assertEqual(len(self.rows('DELETEDTIME IS NULL OR DELETEDTIME = ""')), before)

    def test_remembers_to_and_expense_accounts(self):
        self.twin()
        prefs = self.app.config['MMEX_STORE'].prefs()
        self.assertEqual((prefs['twin_to_account'], prefs['twin_expense_account']), (CASH_EUR_ID, self.HOUSEHOLD_ID))
        page = self.client.get('/new?type=Twin').text
        self.assertIn(f'<option value="{CASH_EUR_ID}" selected>', re.search(r'name="to_account">.*?</select>', page, re.S).group(0))
        self.assertIn('<option value="5" selected>Household</option>', re.search(r'name="expense_account">.*?</select>', page, re.S).group(0))

    def test_pattern_and_checks(self):
        page = self.client.get('/settings').text
        self.assertNotIn('clearing', page.lower())
        self.client.post('/settings', data={'csrf_token': csrf(page), 'twin_notes': '{payee} for {expense_account} via {to_account}'})
        self.twin(payee='Dollar Tree')
        self.assertEqual(self.rows("NOTES LIKE 'Dollar Tree%'")[0][4], 'Dollar Tree for Household via Cash (EUR)')
        # Same amount nearby: a duplicate warning naming the account, nothing saved until confirmed.
        n = len(self.rows())
        r = self.twin(payee='Dollar Tree', date='2099-03-02')
        self.assertIn('Possible duplicate', r.text)
        self.assertIn('Household: 2099-03-01', r.text)
        self.assertEqual(len(self.rows()), n)
        self.twin(payee='Dollar Tree', date='2099-03-02', confirm_duplicate='1')
        self.assertEqual(len(self.rows()), n + 2)
        # Three different accounts, one currency; every account picked.
        self.assertIn('must be three different accounts', self.twin(expense_account=str(CASH_EUR_ID)).text)
        self.assertIn('must use the same currency', self.twin(expense_account=str(CASH_USD_ID), amount='1.23').text)
        self.assertIn('Expense account: pick an account', self.twin(expense_account='').text)
        # Notes typed (or changed) in the form win over the pattern.
        self.twin(payee='Dollar Tree', date='2099-04-01', amount='3.33', transfer_notes='Dollar Tree, gift -> Cash')
        self.assertEqual(self.rows("TRANSAMOUNT = 3.33 AND TRANSCODE = 'Transfer'")[0][4], 'Dollar Tree, gift -> Cash')
        # Edit doesn't offer Twin: once saved, each row is ordinary.
        tid = self.rows("NOTES LIKE 'Dollar Tree%'")[0][0]
        self.assertNotIn('value="Twin"', self.client.get(f'/txn/{tid}/edit').text)

    def test_top_new_button_starts_in_the_account_being_viewed(self):
        page = self.client.get(f'/account/{CASH_EUR_ID}').text
        nav = re.search(r'<nav>.*?</nav>', page, re.S).group(0)
        self.assertIn(f'href="/new?account={CASH_EUR_ID}&amp;next=/account/{CASH_EUR_ID}"', nav)
        self.assertIn('href="/new"', re.search(r'<nav>.*?</nav>', self.client.get('/').text, re.S).group(0))

    def test_name_nav_and_theme(self):
        page = self.client.get('/').text
        self.assertIn('<title>Accounts · MMEX Web UI</title>', page)
        self.assertIn('>MMEX Web UI</a>', page)
        nav = re.search(r'<nav>.*?</nav>', page, re.S).group(0)
        self.assertNotIn('>Accounts</a>', nav)                       # the title links there
        self.assertNotIn('data-theme', page.split('<head>')[0])        # no choice yet: follow the system
        r = self.client.post('/settings/theme', data={'csrf_token': self.token(), 'theme': 'dark', 'next': '/recent'})
        self.assertEqual(r.headers['Location'], '/recent')
        self.assertIn('mmex_web_theme=dark', r.headers['Set-Cookie'])
        self.assertIn('<html lang="en" data-theme="dark">', self.client.get('/').text)
        r = self.client.post('/settings/theme', data={'csrf_token': self.token(), 'theme': 'light'},
                             headers={'X-Requested-With': 'fetch'})
        self.assertEqual(r.json, {'theme': 'light'})
        self.assertIn('data-theme="light"', self.client.get('/').text)


class FlagTests(EntryTestsBase):
    """The flag button on each row: unflagged -> R -> F -> unflagged, with
    taps close together grouped into one History entry and one undo."""

    def tap(self, trans_id, status, fetch=True, account=BANK_ID):
        headers = {'X-Requested-With': 'fetch'} if fetch else {}
        return self.client.post(f'/txn/{trans_id}/flag', headers=headers,
                                data={'status': status, 'account': str(account), 'next': f'/account/{BANK_ID}',
                                      'csrf_token': self.token()})

    def status(self, trans_id):
        return fx.q(self.db, 'SELECT STATUS FROM CHECKINGACCOUNT_V1 WHERE TRANSID = ?', trans_id)[0][0] or ''

    def test_cycle_and_totals(self):
        self.add(amount='10.00')
        t = self.newest()[0]
        page = self.client.get(f'/account/{BANK_ID}')
        self.assertIn(f'action="/txn/{t}/flag"', page.text)
        self.assertIn('data-total="reconciled"', page.text)
        res = self.tap(t, '').get_json()
        self.assertTrue(res['ok'])
        self.assertEqual((self.status(t), res['button']['status'], res['button']['label']), ('R', 'R', 'R'))
        self.assertEqual(set(res['totals']), {'balance', 'reconciled', 'unreconciled'})
        self.assertNotEqual(res['totals']['reconciled']['text'], '')
        self.assertEqual(self.tap(t, 'R').get_json()['button']['status'], 'F')
        self.assertEqual(self.status(t), 'F')
        self.assertEqual(self.tap(t, 'F').get_json()['button']['label'], '·')
        self.assertEqual(self.status(t), '')

    def test_stale_tap_changes_nothing(self):
        self.add()
        t = self.newest()[0]
        self.tap(t, '')                                  # now R
        snap = fx.sha(self.db)
        res = self.tap(t, '').get_json()                 # a second page still showing it unflagged
        self.assertFalse(res['ok'])
        self.assertIn('changed since', res['error'])
        self.assertEqual(res['button']['status'], 'R')
        self.assertEqual(fx.sha(self.db), snap)

    def test_bad_status_and_plain_post(self):
        self.add()
        t = self.newest()[0]
        self.assertEqual(self.tap(t, 'V').status_code, 400)
        r = self.tap(t, '', fetch=False)
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers['Location'].endswith(f'/account/{BANK_ID}'))
        self.assertEqual(self.status(t), 'R')

    def test_taps_group_into_one_undo(self):
        self.add(amount='1.00')
        self.add(amount='2.00')
        self.add(amount='3.00')
        a, b, c = [r[0] for r in self.rows("NOTES <> 'seed'")[-3:]]
        store = self.app.config['MMEX_STORE']
        n = store.count()
        self.tap(a, '')            # a: R
        self.tap(b, '')            # b: R
        self.tap(b, 'R')           # b: F
        self.tap(c, '')            # c: R
        self.tap(c, 'R')
        self.tap(c, 'F')           # c: back to unflagged
        self.assertEqual(store.count(), n + 1, 'one history entry for the run')
        w = store.list()[0]
        self.assertEqual(w['action'], 'flag')
        self.assertEqual(w['summary'], f'Set status on 2 row(s) in {BANK}: 1 to R, 1 to F')
        self.assertEqual(store.last_source([a, b, c]), {a: 'you', b: 'you', c: 'you'})
        self.assertIn('Undo', self.client.get('/history').text)
        r = self.client.post(f'/history/{w["id"]}/undo', data={'csrf_token': self.token()})
        self.assertEqual(r.status_code, 302)
        self.assertEqual([self.status(x) for x in (a, b, c)], ['', '', ''])
        # After the undo a new tap starts a new entry.
        self.tap(a, '')
        self.assertEqual(store.count(), n + 3)
        self.assertEqual(store.list()[0]['summary'], f'Set status on 1 row(s) in {BANK}: 1 to R')

    def test_other_write_or_time_gap_starts_new_entry(self):
        import datetime
        self.add()
        t = self.newest()[0]
        store = self.app.config['MMEX_STORE']
        self.tap(t, '')
        n = store.count()
        self.add(amount='7.00')                         # another write in between
        self.tap(t, 'R')
        self.assertEqual(store.count(), n + 2)
        later = (datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
                 + datetime.timedelta(minutes=16))
        writer = self.app.config['MMEX_WRITER']
        result, _ = writer.flag(t, 'F', '', now=later)
        self.assertTrue(result.ok)
        self.assertEqual(store.count(), n + 3, 'more than 15 minutes after the last tap')

    def test_void_rows_have_no_button(self):
        self.add(status='')
        t = self.newest()[0]
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE CHECKINGACCOUNT_V1 SET STATUS = 'V' WHERE TRANSID = ?", (t,))
        conn.commit()
        conn.close()
        page = self.client.get(f'/account/{BANK_ID}').text
        self.assertNotIn(f'action="/txn/{t}/flag"', page)
