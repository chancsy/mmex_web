"""The Queue page's review of a batch: twins merged, splits and new payees
badged, balance checks as dividers, the sender's summary and reasons.

    python -m unittest tests.test_review -v
"""

import unittest

from tests.fixtures import BANK
from tests.test_queue import QueueTestsBase
from web.review import build, twin_matcher

PATTERN = '{payee}, {expense_account} -> {to_account}'


def row(type_, account, amount, date='2099-03-05', **kw):
    out = {'id': kw.pop('id', 1), 'date': date, 'type': type_, 'account': account, 'amount': amount,
           'category': kw.pop('category', 'Transfer' if type_ == 'Transfer' else 'Food'), 'notes': kw.pop('notes', ''),
           'status': kw.pop('status', ''), 'number': '', 'tags': kw.pop('tags', [])}
    out.update(kw)
    return out


def add(r, op, why=None):
    e = {'kind': 'add', 'row': r, 'op': op}
    if why:
        e['why'] = why
    return e


def check(account, as_of, mmex, statement=None, op=0):
    statement = mmex if statement is None else statement
    return {'kind': 'check', 'account': account, 'as_of': as_of, 'mmex': mmex, 'statement': statement,
            'ok': abs(mmex - statement) < 0.005, 'op': op}


class MatcherTests(unittest.TestCase):
    def test_pattern(self):
        m = twin_matcher(PATTERN)
        self.assertEqual(m('Pizza Place, Household -> Cash'), {'payee': 'Pizza Place', 'expense': 'Household', 'to': 'Cash'})
        self.assertIsNone(m('Shell'))
        self.assertEqual(twin_matcher('{payee} (via {to_account})')('Coffee (via Cash)'), {'payee': 'Coffee', 'to': 'Cash'})
        self.assertIsNone(twin_matcher('no placeholders')('no placeholders'))
        self.assertIsNone(twin_matcher('{payee} {payee}')('a a'), 'a repeated placeholder matches nothing')


class BuildTests(unittest.TestCase):
    def preview(self, events, balances=()):
        return {'events': events, 'balances': list(balances), 'lines': [], 'ok': True, 'checks_ok': True}

    def test_twin_split_checks_and_counts(self):
        events = [
            check('Card', '2099-03-01', -10.0, op=1),
            check('Card', '2099-03-04', -12.0, op=2),
            {'kind': 'new_payee', 'name': 'Bakery', 'op': 3},
            add(row('Withdrawal', 'Household', 98.0, payee='Grocer', category='Household:Groceries'), 3),
            add(row('Transfer', 'Card', 98.0, to_account='Cash', notes='Grocer, Household -> Cash'), 4, why='statement line 5'),
            add(row('Withdrawal', 'Card', 50.35, payee='Bakery', category='Gifts', notes='Cake'), 5),
            add(row('Transfer', 'Card', 50.35, to_account='Partner', notes='Bakery', status='F'), 6),
            add(row('Deposit', 'Card', 32.01, payee='Bank', category='Income'), 7),
            check('Card', '2099-03-31', -100.0, op=8),
            check('Cash', '2099-03-31', 50.0, 49.0, op=9),
            {'kind': 'update', 'op': 10, 'changed': ['notes'],
             'before': row('Transfer', 'Card', 4.85, id=7, to_account='Cash', notes='Shop, Household -> Cash'),
             'after': row('Transfer', 'Card', 4.85, id=7, to_account='Cash', notes='Grocer, Household -> Cash')},
        ]
        r = build(self.preview(events, [{'account': 'Card', 'before': -1.0, 'after': -2.5}]), PATTERN, summary='All lines confirmed')
        kinds = [e['kind'] for e in r['entries']]
        self.assertEqual(kinds, ['checks', 'twin', 'row', 'row', 'row', 'checks', 'change'])
        self.assertEqual(len(r['entries'][0]['items']), 2, 'consecutive checks form one divider')
        twin = r['entries'][1]
        self.assertEqual((twin['expense']['payee'], twin['transfer']['to_account'], twin['amount']), ('Grocer', 'Cash', -98.0))
        self.assertEqual(twin['why'], 'statement line 5')
        bakery, partner = r['entries'][2], r['entries'][3]
        self.assertIn(('split', 'Split'), bakery['badges'])
        self.assertIn(('new', 'New payee'), bakery['badges'])
        self.assertIn(('split', 'Split'), partner['badges'])
        self.assertEqual((partner['filter'], partner['amount']), ('transfer', -50.35))
        self.assertEqual(r['entries'][4]['amount'], 32.01)
        self.assertFalse(r['entries'][5]['ok'], 'a failing check marks its divider')
        change = r['entries'][6]
        self.assertEqual(change['fields'], [{'name': 'notes', 'before': 'Shop, Household -> Cash', 'after': 'Grocer, Household -> Cash'}])
        self.assertIn(('twin', 'Twin'), change['badges'])
        self.assertEqual((change['title'], change['route']), ('Grocer', 'Card → Cash'))
        self.assertEqual((partner['title'], partner['route']), ('Bakery', 'Card → Partner'))
        self.assertEqual((bakery['title'], bakery['route']), ('Bakery', 'Card'))
        c = r['counts']
        self.assertEqual((c['new'], c['twin'], c['transfer'], c['own'], c['change']), (5, 1, 1, 2, 1))
        self.assertEqual((c['checks'], c['checks_ok'], c['new_payees']), (4, 3, 1))
        self.assertEqual(r['balances'][0]['change'], -1.5)
        self.assertEqual(r['summary'], 'All lines confirmed')

    def test_twin_needs_same_day_amount_and_payee(self):
        transfer = row('Transfer', 'Card', 9.0, to_account='Cash', notes='Shop, Household -> Cash')
        for other in (row('Withdrawal', 'Household', 9.0, date='2099-03-06', payee='Shop'),
                      row('Withdrawal', 'Household', 9.5, payee='Shop'),
                      row('Withdrawal', 'Household', 9.0, payee='Other'),
                      row('Withdrawal', 'Elsewhere', 9.0, payee='Shop')):
            r = build(self.preview([add(transfer, 1), add(other, 2)]), PATTERN)
            self.assertEqual([e['kind'] for e in r['entries']], ['row', 'row'], other)

    def test_no_events(self):
        r = build({'lines': ['x'], 'balances': []}, PATTERN)
        self.assertFalse(r['has_events'])
        self.assertEqual(r['entries'], [])


class ReviewPageTests(QueueTestsBase):
    def test_page_shows_summary_why_and_checks(self):
        ops = [{'op': 'add', 'date': '2099-03-05', 'type': 'Withdrawal', 'account': BANK, 'payee': 'Diner',
                'category': 'Food:Takeaway', 'amount': 4.5, 'notes': 'lunch', 'why': 'statement: DINER 4.50'}]
        body = self.submit(key='review', ops=ops, summary='One line from the statement').get_json()
        html = self.client.get(f'/queue/{body["id"]}').text
        self.assertIn('One line from the statement', html)
        self.assertIn('Why: statement: DINER 4.50', html)
        self.assertIn('1 new row', html)
        self.assertIn('class="approve-bar"', html)
        self.assertIn('<summary>Plain text</summary>', html)
        self.assertIn('data-filter="own"', html)
        self.assertEqual(self.app.config['MMEX_STORE'].item(body['id'])['plan']['summary'], 'One line from the statement')
        # Approving still works from the new bar.
        self.assertEqual(self.approve(body['id']).status_code, 302)
        self.assertIn('One line from the statement', self.client.get(f'/queue/{body["id"]}').text)

    def test_summary_must_be_text(self):
        r = self.submit(key='review2', summary=['not', 'text'])
        self.assertEqual(r.status_code, 400)


if __name__ == '__main__':
    unittest.main()
