"""Bounded discovery reads over accumulated public ranking history."""
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from spoondev import db, profiledb, worker


class DiscoveryQueryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name)/'observations.sqlite3'
        db.initialize(self.database)
        profiledb.initialize(self.database)
        self.now = datetime(2026, 10, 10, 12, tzinfo=timezone.utc).timestamp()

    def ranking(self, dj, listeners, *, seconds=0, month='2026-10', complete=True):
        return profiledb.save_dj_ranking(self.database, {'id':dj, 'name':'DJ'+dj},
            [{'user':{'id':uid, 'name':'User'+uid}, 'temperature':temperature}
             for uid, temperature in listeners], month, complete,
            worker._iso(self.now+seconds))

    def live(self, dj, uid, seconds=0):
        return db.save_snapshot(self.database, {
            'room_id':dj, 'broadcaster':{'id':dj, 'name':'DJ'+dj},
            'listeners':[{'id':uid, 'name':'User'+uid}], 'complete':True,
            'observed_at':worker._iso(self.now+seconds)})

    def appearances(self, users, now=None):
        with worker._readonly(self.database) as conn:
            before = conn.total_changes
            result = worker._appearances(conn, users, self.now if now is None else now)
            self.assertEqual(conn.total_changes, before)
            return result

    def test_latest_monthly_replacement_handles_empty_partial_and_equal_time(self):
        self.ranking('10', [('77', 80)], seconds=-20)
        self.ranking('10', [], seconds=-10)
        self.ranking('20', [('77', 80)], seconds=-20)
        self.ranking('20', [('88', None)], seconds=-10, complete=False)
        self.ranking('30', [('77', 40)], seconds=-10)
        self.ranking('30', [('88', 0)], seconds=-10)
        self.ranking('40', [('77', None)], seconds=-10, complete=False)
        self.assertEqual(self.appearances(['77']), {'40'})
        self.assertEqual(self.appearances(['88']), {'20','30'})
        self.assertEqual(self.appearances(['77','88']), {'20','30','40'})

    def test_future_snapshot_does_not_replace_asof_ranking_or_add_destination(self):
        self.ranking('10', [('77', 80)], seconds=-1)
        self.ranking('10', [], seconds=0.000001)
        self.ranking('20', [('77', 80)], seconds=0.000001)
        self.live('30', '77', seconds=-30*86400)
        self.live('40', '77', seconds=-30*86400-0.000001)
        self.live('50', '77', seconds=0.000001)
        self.live('60', '77')
        self.assertEqual(self.appearances(['77']), {'10','30','60'})

    def test_current_month_is_jst_even_before_utc_month_boundary(self):
        self.now = datetime(2026, 9, 30, 15, tzinfo=timezone.utc).timestamp()
        self.ranking('10', [('77', 80)], seconds=-1, month='2026-09')
        self.ranking('20', [('77', 80)], month='2026-10')
        self.assertEqual(self.appearances(['77']), {'20'})

    def test_batched_cohort_unions_live_and_latest_monthly_destinations(self):
        users = [str(uid) for uid in range(1001, 1810)]
        self.ranking('10', [(users[0], 80)])
        self.ranking('20', [(users[400], 80)])
        self.ranking('30', [(users[-1], 80)])
        self.live('40', users[0])
        self.live('50', users[400])
        self.live('60', users[-1])
        self.assertEqual(self.appearances(users), {'10','20','30','40','50','60'})
        self.assertEqual(self.appearances([]), set())

    def test_legacy_observation_database_without_monthly_tables(self):
        path = Path(self.temp.name)/'legacy.sqlite3'
        db.initialize(path)
        db.save_snapshot(path, {
            'room_id':'10', 'broadcaster':{'id':'10', 'name':'DJ10'},
            'listeners':[{'id':'77', 'name':'User77'}], 'complete':True,
            'observed_at':worker._iso(self.now)})
        with worker._readonly(path) as conn:
            self.assertEqual(worker._appearances(conn, ['77'], self.now), {'10'})

    def test_archived_rankings_do_not_require_per_listener_history_lookups(self):
        # This VM-work budget covers 115,200 archived ranking entries. It avoids
        # clock-sensitive timing assertions while detecting a return to looking
        # up the latest DJ snapshot for every matching historical listener row.
        with sqlite3.connect(self.database) as conn:
            conn.executemany('INSERT INTO profile_users VALUES(?,?,?,?)',
                [(str(uid),str(uid),None,worker._iso(self.now))
                 for uid in list(range(1,13))+list(range(101,221))])
            for dj in range(1,13):
                for revision in range(80):
                    sid = conn.execute('''INSERT INTO monthly_dj_snapshots
                        (dj_id,month,observed_at,complete) VALUES(?,?,?,1)''',
                        (str(dj),'2026-10',worker._iso(self.now-80*3600+revision*3600))).lastrowid
                    conn.executemany('INSERT INTO monthly_dj_listeners VALUES(?,?,?)',
                        [(sid,str(uid),20) for uid in range(101,221)])
        instructions = 0
        def track():
            nonlocal instructions
            instructions += 1000
            return 0
        with worker._readonly(self.database) as conn:
            conn.set_progress_handler(track,1000)
            result = worker._appearances(conn, [str(uid) for uid in range(101,181)], self.now)
        self.assertEqual(result, {str(dj) for dj in range(1,13)})
        self.assertLess(instructions,100000)


if __name__ == '__main__':
    unittest.main()
