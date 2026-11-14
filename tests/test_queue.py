"""The queue: the engine's `absent` check, API tokens, the JSON API, and the
Queue page (approve, reject, edit).

    python -m unittest tests.test_queue -v
"""

import os
import re
import tempfile
import unittest

from engine import BackupPolicy, apply, preview
from tests import fixtures as fx
from tests.fixtures import BANK, CASH_EUR
from tests.test_entry import EntryTestsBase
from web import secrets_file
from web.writer import preview_digest

BILL = [{'op': 'absent', 'account': BANK, 'from': '2026-03-01', 'to': '2026-03-31', 'type': 'Withdrawal',
         'payee': 'Diner'},
        {'op': 'add', 'date': '2026-03-10', 'type': 'Withdrawal', 'account': BANK, 'payee': 'Diner',
         'category': 'Food:Takeaway', 'amount': 42.5, 'notes': 'bill 202603'}]


class AbsentOpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='mmex_absent_')
        self.db = fx.build_db(os.path.join(self.tmp, 'money.mmb'), legacy=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp)

    def test_passes_when_nothing_in_range(self):
        r = preview(self.db, {'ops': BILL})
        self.assertTrue(r.ok and r.checks_ok, r.error)
        self.assertTrue(r.lines[0].startswith('PASS absent: no Withdrawal in Bank (EUR) 2026-03-01..2026-03-31 to Diner'))

    def test_fails_when_the_month_already_has_one(self):
        ops = [dict(BILL[0], **{'from': '2026-01-01', 'to': '2026-01-31'})]
        r = preview(self.db, {'ops': ops})
        self.assertTrue(r.ok)
        self.assertFalse(r.checks_ok)
        found = r.events[0]['found']
        # 5001 and 5002 (5005 is void, so it doesn't count)
        self.assertEqual(sorted(f['id'] for f in found), [5001, 5002])
        self.assertIn('FAIL absent: MMEX already has', r.lines[0])
        before = fx.sha(self.db)
        result = apply(self.db, {'ops': ops + [BILL[1]]}, BackupPolicy(os.path.join(self.tmp, 'bak')))
        self.assertFalse(result.ok)
        self.assertEqual(fx.sha(self.db), before, 'a failing absent check writes nothing')

    def test_type_and_payee_narrow_it(self):
        jan = {'op': 'absent', 'account': BANK, 'from': '2026-01-01', 'to': '2026-01-31'}
        self.assertFalse(preview(self.db, {'ops': [jan]}).checks_ok)
        self.assertTrue(preview(self.db, {'ops': [dict(jan, type='Deposit', **{'from': '2026-01-01', 'to': '2026-01-30'})]}).checks_ok)
        self.assertTrue(preview(self.db, {'ops': [dict(jan, payee='Old Shop')]}).checks_ok)
        self.assertTrue(preview(self.db, {'ops': [dict(jan, payee='Nobody Yet')]}).checks_ok)
        self.assertFalse(preview(self.db, {'ops': [dict(jan, type='Bogus')]}).ok)


class TokenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='mmex_tokens_')
        self.path = os.path.join(self.tmp, 'secrets.json')
        secrets_file.create_or_update(self.path, 'x' * 12)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp)

    def test_token_kept_only_as_hash(self):
        token = secrets_file.set_token(self.path, 'check_bills')
        with open(self.path) as fh:
            self.assertNotIn(token, fh.read())
        self.assertEqual(list(secrets_file.load(self.path)['api_tokens']), ['check_bills'])
        self.assertNotEqual(secrets_file.set_token(self.path, 'check_bills'), token, 'a new token each time')
        secrets_file.set_token(self.path, 'check_bills', revoke=True)
        self.assertEqual(secrets_file.load(self.path)['api_tokens'], {})

    def test_bad_source_names(self):
        for name in ('you', 'Check', '1x', 'a b', '', 'x' * 33):
            with self.assertRaises(secrets_file.SecretsError, msg=name):
                secrets_file.set_token(self.path, name)

    def test_password_change_keeps_tokens(self):
        secrets_file.set_token(self.path, 'reconcile')
        secrets_file.create_or_update(self.path, 'y' * 12)
        self.assertIn('reconcile', secrets_file.load(self.path)['api_tokens'])


class QueueTestsBase(EntryTestsBase):
    def setUp(self):
        super().setUp()
        path = os.path.join(self.data, 'secrets.json')
        self.bills = secrets_file.set_token(path, 'check_bills')
        self.recon = secrets_file.set_token(path, 'reconcile')

    def api(self, method, path, token=None, **kw):
        headers = kw.pop('headers', {})
        headers['Authorization'] = f'Bearer {token or self.bills}'
        return getattr(self.client, method)('/api' + path, headers=headers, **kw)

    def submit(self, key='TNB|202603', ops=None, **extra):
        return self.api('post', '/queue', json=dict({'key': key, 'title': f'Bill {key}', 'ops': ops or BILL}, **extra))

    def page_form(self, item_id):
        html = self.client.get(f'/queue/{item_id}').text
        seen = re.search(r'name="seen" value="([^"]*)"', html)
        changed = re.search(r'name="changed" value="([01])"', html)
        return html, (seen.group(1) if seen else None), (changed.group(1) if changed else None)

    def approve(self, item_id, ack=False, seen=None):
        html, page_seen, changed = self.page_form(item_id)
        data = {'csrf_token': self.token(), 'seen': seen if seen is not None else page_seen, 'changed': changed or '0'}
        if ack:
            data['ack'] = '1'
        return self.client.post(f'/queue/{item_id}/approve', data=data)

    def bill_rows(self):
        return self.rows("NOTES = 'bill 202603'")


class ApiTests(QueueTestsBase):
    def test_errors_are_json(self):
        r = self.client.get('/api/queue')
        self.assertEqual((r.status_code, r.get_json()), (401, {'error': 'missing or unknown API token'}))
        r = self.api('get', '/queue', headers={'X-Forwarded-For': '1.2.3.4'})
        self.assertEqual((r.status_code, r.get_json()), (404, {'error': 'not found'}))
        r = self.api('get', '/balance?account=Nope')
        self.assertEqual(r.status_code, 400)
        self.assertIn('no account named', r.get_json()['error'])

    def test_needs_a_known_token(self):
        self.assertEqual(self.client.get('/api/queue').status_code, 401)
        self.assertEqual(self.api('get', '/queue', token='nope').status_code, 401)
        self.assertEqual(self.api('get', '/queue').status_code, 200)

    def test_refused_through_a_proxy(self):
        r = self.api('get', '/queue', headers={'X-Forwarded-For': '1.2.3.4'})
        self.assertEqual(r.status_code, 404)
        self.app.config['MMEX_SETTINGS'].api_via_proxy = True
        self.assertEqual(self.api('get', '/queue', headers={'X-Forwarded-For': '1.2.3.4'}).status_code, 200)

    def test_no_csrf_and_no_session_needed(self):
        fresh = self.app.test_client()
        r = fresh.post('/api/queue', json={'key': 'k', 'ops': BILL}, headers={'Authorization': f'Bearer {self.bills}'})
        self.assertEqual(r.status_code, 201, r.text)

    def test_submit_previews_and_writes_nothing(self):
        before = fx.sha(self.db)
        r = self.submit()
        self.assertEqual(r.status_code, 201, r.text)
        body = r.get_json()
        self.assertEqual(body['status'], 'pending')
        self.assertTrue(any(line.startswith('+ 2026-03-10 Withdrawal') for line in body['preview']['lines']))
        self.assertEqual(fx.sha(self.db), before)
        item = self.api('get', f'/queue/{body["id"]}').get_json()
        self.assertEqual((item['source'], item['key'], item['status'], item['rows']), ('check_bills', 'TNB|202603', 'pending', []))

    def test_same_key_pending_conflicts_unless_replaced(self):
        first = self.submit().get_json()['id']
        r = self.submit()
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()['id'], first)
        second = self.submit(replace=True).get_json()['id']
        self.assertEqual(self.api('get', f'/queue/{first}').get_json()['status'], 'superseded')
        self.assertEqual(self.api('get', f'/queue/{second}').get_json()['status'], 'pending')
        # The same key from another source is its own batch.
        self.assertEqual(self.submit(token=None).status_code, 409)
        r = self.api('post', '/queue', token=self.recon, json={'key': 'TNB|202603', 'ops': BILL})
        self.assertEqual(r.status_code, 201)

    def test_invalid_or_failing_batches_are_refused(self):
        r = self.submit(ops=[dict(BILL[1], account='No Such')])
        self.assertEqual(r.status_code, 422)
        self.assertIn('no account named', r.get_json()['error'])
        jan = [dict(BILL[0], **{'from': '2026-01-01', 'to': '2026-01-31'}), BILL[1]]
        r = self.submit(ops=jan)
        self.assertEqual(r.status_code, 422)
        found = [e for e in r.get_json()['preview']['events'] if e['kind'] == 'absent'][0]['found']
        self.assertEqual(sorted(f['amount'] for f in found), [8.0, 12.5])
        for bad in ({'key': 'k'}, {'key': '', 'ops': BILL}, {'key': 'k', 'ops': []}):
            self.assertEqual(self.api('post', '/queue', json=bad).status_code, 400, bad)
        self.assertEqual(self.api('post', '/queue', data='not json').status_code, 400)
        self.assertEqual(self.api('get', '/queue').get_json()['items'], [])

    def test_a_source_sees_only_its_own_batches(self):
        item_id = self.submit().get_json()['id']
        self.assertEqual(self.api('get', f'/queue/{item_id}', token=self.recon).status_code, 404)
        self.assertEqual(self.api('get', '/queue', token=self.recon).get_json()['items'], [])
        self.assertEqual(len(self.api('get', '/queue?status=pending').get_json()['items']), 1)
        self.assertEqual(len(self.api('get', '/queue?key=TNB|202603').get_json()['items']), 1)
        self.assertEqual(self.api('get', '/queue?status=bogus').status_code, 400)
        summary = self.api('get', '/queue/summary', token=self.recon).get_json()
        self.assertEqual((summary['pending'], summary['by_source']), (1, {'check_bills': 1}))
        self.assertTrue(summary['oldest_utc'])

    def test_reads(self):
        r = self.api('get', f'/balance?account={BANK}&as_of=2026-01-06').get_json()
        self.assertEqual(r['balance'], 1000 - 12.5 - 8.0)
        self.assertEqual(self.api('get', '/balance?account=Nope').status_code, 400)
        rows = self.api('get', f'/find?account={BANK}&from=2026-01-05&to=2026-01-06').get_json()['rows']
        self.assertEqual([r['id'] for r in rows], [5002, 5001])
        self.assertEqual(self.api('get', '/find?from=bad').status_code, 400)
        accounts = self.api('get', '/lookup/accounts?like=cash').get_json()['rows']
        self.assertEqual([a['name'] for a in accounts], [CASH_EUR, 'Cash (USD)'])
        cats = self.api('get', '/lookup/categories?like=take').get_json()['rows']
        self.assertEqual(cats[0]['name'], 'Food:Takeaway')
        self.assertEqual(self.api('get', '/lookup/bogus').status_code, 404)


class QueuePageTests(QueueTestsBase):
    def test_approve_applies_records_and_reports_back(self):
        item_id = self.submit().get_json()['id']
        page = self.client.get('/')
        self.assertIn('<span class="badge" title="1 waiting">1</span>', page.text)
        html, seen, changed = self.page_form(item_id)
        self.assertIn('Approving does this', html)
        self.assertEqual(changed, '0')
        baks = len(os.listdir(self.bak)) if os.path.isdir(self.bak) else 0
        r = self.approve(item_id)
        self.assertEqual(r.status_code, 302)
        rows = self.bill_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(os.listdir(self.bak)), baks + 1, 'approving always backs up first')
        item = self.api('get', f'/queue/{item_id}').get_json()
        self.assertEqual((item['status'], item['rows'], item['undone']), ('approved', [rows[0][0]], False))
        history = self.client.get('/history').text
        self.assertIn('Bill TNB|202603: Added 2026-03-10 Withdrawal', history)
        self.assertIn('<span class="chip">check_bills</span>', history)
        self.assertNotIn('class="badge"', self.client.get('/').text)
        # Approving twice does nothing more.
        self.client.post(f'/queue/{item_id}/approve', data={'csrf_token': self.token(), 'seen': seen, 'changed': '0'})
        self.assertEqual(len(self.bill_rows()), 1)
        # Undone in History: the API says so.
        write_id = self.app.config['MMEX_STORE'].item(item_id)['write_id']
        self.client.post(f'/history/{write_id}/undo', data={'csrf_token': self.token()})
        self.assertEqual(len(self.bill_rows()), 0)
        self.assertTrue(self.api('get', f'/queue/{item_id}').get_json()['undone'])

    def test_stale_page_is_refused(self):
        item_id = self.submit().get_json()['id']
        _, seen, _ = self.page_form(item_id)
        self.add(amount='9.99', date='2026-02-01')  # moves the balance the preview shows
        r = self.client.post(f'/queue/{item_id}/approve', data={'csrf_token': self.token(), 'seen': seen, 'changed': '0'},
                             follow_redirects=True)
        self.assertIn('has changed since the page was loaded', r.text)
        self.assertEqual(self.bill_rows(), [])

    def test_changed_since_sent_needs_a_tick(self):
        item_id = self.submit().get_json()['id']
        self.add(amount='9.99', date='2026-02-01')
        html, seen, changed = self.page_form(item_id)
        self.assertEqual(changed, '1')
        self.assertIn('has changed since it was sent', html)
        self.approve(item_id)  # no tick: refused
        self.assertEqual(self.bill_rows(), [])
        self.approve(item_id, ack=True)
        self.assertEqual(len(self.bill_rows()), 1)

    def test_bill_entered_meanwhile_cannot_be_approved(self):
        item_id = self.submit().get_json()['id']
        self.add(amount='42.50', date='2026-03-09')  # the same bill, typed in by hand
        html, seen, _ = self.page_form(item_id)
        self.assertIn('can\'t be applied as it is now', html)
        self.assertIsNone(seen, 'no Approve button')
        self.client.post(f'/queue/{item_id}/approve', data={'csrf_token': self.token(), 'seen': 'x', 'changed': '1', 'ack': '1'})
        self.assertEqual(self.bill_rows(), [])

    def test_reject_with_note(self):
        item_id = self.submit().get_json()['id']
        self.client.post(f'/queue/{item_id}/reject', data={'csrf_token': self.token(), 'note': 'wrong month'})
        item = self.api('get', f'/queue/{item_id}').get_json()
        self.assertEqual((item['status'], item['note']), ('rejected', 'wrong month'))
        self.assertEqual(self.bill_rows(), [])
        self.assertEqual(self.submit().status_code, 201, 'a rejected key can be sent again')

    def test_edit_a_single_add(self):
        item_id = self.submit().get_json()['id']
        self.client.post(f'/queue/{item_id}/edit', data={'csrf_token': self.token(), 'amount': '43.10',
                                                          'date': '2026-03-11', 'notes': 'bill 202603'})
        item = self.app.config['MMEX_STORE'].item(item_id)
        self.assertTrue(item['edited'])
        self.assertEqual(item['plan']['ops'][0]['op'], 'absent', 'the check stays')
        self.assertEqual((item['plan']['ops'][1]['amount'], item['plan']['ops'][1]['date']), (43.1, '2026-03-11'))
        self.assertEqual(self.page_form(item_id)[2], '0', 'the edited preview is the new baseline')
        self.approve(item_id)
        self.assertEqual([(r[2], r[8][:10]) for r in self.bill_rows()], [(43.1, '2026-03-11')])

    def test_edit_refuses_bad_input_and_multi_op_batches(self):
        item_id = self.submit().get_json()['id']
        r = self.client.post(f'/queue/{item_id}/edit', data={'csrf_token': self.token(), 'amount': '-1',
                                                              'date': '2026-03-11', 'notes': ''}, follow_redirects=True)
        self.assertIn('enter a positive amount', r.text)
        self.assertFalse(self.app.config['MMEX_STORE'].item(item_id)['edited'])
        two = BILL + [dict(BILL[1], amount=1.0, notes='second')]
        other = self.submit(key='two', ops=two).get_json()['id']
        self.assertNotIn('Edit before approving', self.page_form(other)[0])
        r = self.client.post(f'/queue/{other}/edit', data={'csrf_token': self.token(), 'amount': '1', 'date': '2026-03-11'})
        self.assertEqual(r.status_code, 400)

    def test_tabs_and_read_only(self):
        a = self.submit(key='a').get_json()['id']
        self.submit(key='b', replace=False)
        self.client.post(f'/queue/{a}/reject', data={'csrf_token': self.token()})
        self.assertIn('Bill b', self.client.get('/queue').text)
        self.assertNotIn('Bill a', self.client.get('/queue').text)
        self.assertIn('Bill a', self.client.get('/queue?status=rejected').text)
        self.assertIn('Bill a', self.client.get('/queue?status=all').text)
        self.assertEqual(self.client.get('/queue?status=bogus').status_code, 400)
        self.assertEqual(self.client.get('/queue/999').status_code, 404)


class ReadOnlyQueueTests(QueueTestsBase):
    backups = False

    def test_no_buttons_and_no_approving_without_backups(self):
        item_id = self.submit().get_json()['id']
        html, seen, _ = self.page_form(item_id)
        self.assertIsNone(seen)
        self.assertNotIn('Reject', html)
        r = self.client.post(f'/queue/{item_id}/approve', data={'csrf_token': self.token(), 'seen': 'x', 'changed': '0'})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.bill_rows(), [])


class DigestTests(unittest.TestCase):
    def test_new_row_ids_dont_count(self):
        a = {'ok': True, 'error': None, 'checks_ok': True, 'balances': [], 'lines': ['+ 2026-01-01 x  (id 111)']}
        b = dict(a, lines=['+ 2026-01-01 x  (id 222)'])
        c = dict(a, lines=['~ 2026-01-01 x  (id 111)'])
        self.assertEqual(preview_digest(a), preview_digest(b))
        self.assertNotEqual(preview_digest(a), preview_digest(c))


if __name__ == '__main__':
    unittest.main()
