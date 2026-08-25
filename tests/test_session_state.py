#!/usr/bin/env python3
"""Unit tests for session-scoped approval state."""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import session_state


class SessionStateTests(unittest.TestCase):
    def setUp(self):
        # Redirect state to a throwaway directory so tests never touch the
        # grants of a real running session.
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_dir = session_state.SESSIONS_DIR
        session_state.SESSIONS_DIR = self._tmp.name

    def tearDown(self):
        session_state.SESSIONS_DIR = self._orig_dir
        self._tmp.cleanup()

    def test_grant_then_allows_medium(self):
        session_state.grant("sess-a", max_risk="medium")
        self.assertTrue(session_state.allows("sess-a", "medium"))
        self.assertTrue(session_state.allows("sess-a", "low"))

    def test_medium_grant_does_not_cover_high(self):
        session_state.grant("sess-a", max_risk="medium")
        self.assertFalse(session_state.allows("sess-a", "high"))

    def test_grant_is_scoped_to_one_session(self):
        session_state.grant("sess-a", max_risk="medium")
        self.assertFalse(session_state.allows("sess-b", "medium"))

    def test_no_grant_allows_nothing(self):
        self.assertFalse(session_state.allows("never-granted", "medium"))

    def test_empty_session_id_is_rejected(self):
        self.assertFalse(session_state.grant("", max_risk="medium"))
        self.assertFalse(session_state.allows("", "medium"))

    def test_expired_grant_stops_allowing(self):
        session_state.grant("sess-a", max_risk="medium", ttl_seconds=1)
        # Backdate the grant rather than sleeping.
        path = session_state._path("sess-a")
        import json
        with open(path) as f:
            data = json.load(f)
        data["granted_at"] = time.time() - 10
        with open(path, "w") as f:
            json.dump(data, f)

        self.assertFalse(session_state.allows("sess-a", "medium"))
        # ...and the stale file is cleaned up so it can't come back.
        self.assertFalse(os.path.exists(path))

    def test_revoke(self):
        session_state.grant("sess-a", max_risk="medium")
        self.assertTrue(session_state.revoke("sess-a"))
        self.assertFalse(session_state.allows("sess-a", "medium"))

    def test_revoke_all(self):
        session_state.grant("sess-a")
        session_state.grant("sess-b")
        self.assertEqual(session_state.revoke_all(), 2)
        self.assertEqual(session_state.list_active(), [])

    def test_list_active_is_newest_first(self):
        session_state.grant("older")
        time.sleep(0.01)
        session_state.grant("newer")
        ids = [g["session_id"] for g in session_state.list_active()]
        self.assertEqual(ids, ["newer", "older"])

    def test_session_id_with_path_separators_is_sanitised(self):
        # A malformed session id must not let a grant escape SESSIONS_DIR.
        session_state.grant("../../etc/passwd", max_risk="medium")
        written = os.listdir(session_state.SESSIONS_DIR)
        self.assertEqual(len(written), 1)
        self.assertNotIn("/", written[0])

    def test_high_ceiling_covers_everything(self):
        session_state.grant("sess-a", max_risk="high")
        self.assertTrue(session_state.allows("sess-a", "high"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
