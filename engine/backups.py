"""Count-based DB backups, kept the way MMEX keeps its own: a set number of
copies, oldest deleted first.

Files are named <db file name>_<tag>_YYYY-MM-DD_HHMMSS.bak, e.g.
money.mmb_web_2026-11-11_223000.bak, so they sort by time and sit beside
MMEX's own money.mmb_update_<date>.bak without ever matching them. Pruning
only ever touches files with this policy's own <name>_<tag>_ prefix.
"""

import datetime
import os
import re

from .dbfile import copy_db

STAMP = '%Y-%m-%d_%H%M%S'


class BackupPolicy:
    def __init__(self, folder, keep=20, min_interval_min=30, tag='web'):
        if keep < 1:
            raise ValueError('keep must be at least 1')
        if min_interval_min < 0:
            raise ValueError('min_interval_min cannot be negative')
        if not re.fullmatch(r'[A-Za-z0-9-]+', tag):
            raise ValueError('tag must be letters, digits or -')
        self.folder = folder
        self.keep = keep
        self.min_interval_min = min_interval_min
        self.tag = tag

    def _prefix(self, db_path) -> str:
        return f'{os.path.basename(db_path)}_{self.tag}_'

    def existing(self, db_path) -> list:
        """This policy's backups of db_path, oldest first."""
        if not os.path.isdir(self.folder):
            return []
        prefix = self._prefix(db_path)
        pattern = re.compile(re.escape(prefix) + r'\d{4}-\d\d-\d\d_\d{6}(?:_\d+)?\.bak$')
        return sorted(os.path.join(self.folder, f) for f in os.listdir(self.folder) if pattern.match(f))

    @staticmethod
    def _taken_at(path) -> datetime.datetime:
        m = re.search(r'_(\d{4}-\d\d-\d\d_\d{6})(?:_\d+)?\.bak$', path)
        return datetime.datetime.strptime(m.group(1), STAMP)

    def latest(self, db_path):
        files = self.existing(db_path)
        return files[-1] if files else None

    def take(self, db_path, force=False, now=None) -> tuple:
        """Back db_path up unless the newest backup is younger than the
        interval (force skips that check). Returns (path, taken): the new
        file, or the recent one that already covers this write."""
        now = now or datetime.datetime.now()
        latest = self.latest(db_path)
        if latest and not force:
            age_min = (now - self._taken_at(latest)).total_seconds() / 60
            if 0 <= age_min < self.min_interval_min:
                return latest, False
        os.makedirs(self.folder, exist_ok=True)
        base = os.path.join(self.folder, self._prefix(db_path) + now.strftime(STAMP))
        path, n = base + '.bak', 1
        while os.path.exists(path):
            n += 1
            path = f'{base}_{n}.bak'
        copy_db(db_path, path)
        self.prune(db_path)
        return path, True

    def prune(self, db_path) -> list:
        """Delete this policy's oldest backups beyond `keep`. Returns them."""
        files = self.existing(db_path)
        doomed = files[:-self.keep] if len(files) > self.keep else []
        for path in doomed:
            os.remove(path)
        return doomed
