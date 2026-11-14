import datetime
import os
import shutil
import tempfile
import unittest

from engine import BackupPolicy
from tests import fixtures as fx


class BackupPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='mmex_backup_test_')
        self.db = fx.build_db(os.path.join(self.tmp, 'money.mmb'))
        self.dir = os.path.join(self.tmp, 'bak')
        self.t0 = datetime.datetime(2026, 11, 11, 22, 0, 0)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_naming_and_copy(self):
        path, taken = BackupPolicy(self.dir).take(self.db, now=self.t0)
        self.assertTrue(taken)
        self.assertEqual(os.path.basename(path), 'money.mmb_web_2026-11-11_220000.bak')
        self.assertEqual(fx.dump(path), fx.dump(self.db))

    def test_interval_skips_recent_unless_forced(self):
        policy = BackupPolicy(self.dir, min_interval_min=30)
        first, _ = policy.take(self.db, now=self.t0)
        same, taken = policy.take(self.db, now=self.t0 + datetime.timedelta(minutes=29))
        self.assertEqual((same, taken), (first, False))
        _, taken = policy.take(self.db, now=self.t0 + datetime.timedelta(minutes=29), force=True)
        self.assertTrue(taken)
        _, taken = policy.take(self.db, now=self.t0 + datetime.timedelta(minutes=61))
        self.assertTrue(taken)
        self.assertEqual(len(policy.existing(self.db)), 3)

    def test_zero_interval_backs_up_every_time(self):
        policy = BackupPolicy(self.dir, min_interval_min=0)
        policy.take(self.db, now=self.t0)
        _, taken = policy.take(self.db, now=self.t0)
        self.assertTrue(taken, 'same second gets a _2 suffix')
        self.assertEqual([os.path.basename(p) for p in policy.existing(self.db)],
                         ['money.mmb_web_2026-11-11_220000.bak', 'money.mmb_web_2026-11-11_220000_2.bak'])

    def test_keep_prunes_oldest_and_leaves_other_files(self):
        os.makedirs(self.dir)
        others = ['money.mmb_update_2026-11-10.bak', 'other.mmb_web_2026-01-01_000000.bak', 'notes.txt']
        for name in others:
            open(os.path.join(self.dir, name), 'w').close()
        policy = BackupPolicy(self.dir, keep=3, min_interval_min=0)
        made = [policy.take(self.db, now=self.t0 + datetime.timedelta(hours=h))[0] for h in range(5)]
        self.assertEqual(policy.existing(self.db), made[-3:])
        for name in others:
            self.assertTrue(os.path.exists(os.path.join(self.dir, name)), f'{name} untouched')

    def test_bad_settings(self):
        for kw in ({'keep': 0}, {'min_interval_min': -1}, {'tag': 'a/b'}):
            with self.assertRaises(ValueError):
                BackupPolicy(self.dir, **kw)


if __name__ == '__main__':
    unittest.main()
