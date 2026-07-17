import os
import tempfile
import unittest
from pathlib import Path

import publicData


class CollectorLockTests(unittest.TestCase):
    def setUp(self):
        self.original_lock_path = publicData.LOCK_PATH
        self.tmpdir = tempfile.TemporaryDirectory()
        publicData.LOCK_PATH = Path(self.tmpdir.name) / "gex_collector.lock"

    def tearDown(self):
        publicData.LOCK_PATH = self.original_lock_path
        self.tmpdir.cleanup()

    def test_collector_lock_removes_dead_pid_lock(self):
        publicData.LOCK_PATH.write_text("999999999", encoding="utf-8")

        with publicData.collector_lock() as acquired:
            self.assertTrue(acquired)
            self.assertEqual(publicData.LOCK_PATH.read_text(encoding="utf-8"), str(os.getpid()))

        self.assertFalse(publicData.LOCK_PATH.exists())

    def test_collector_lock_keeps_live_pid_lock(self):
        publicData.LOCK_PATH.write_text(str(os.getpid()), encoding="utf-8")

        with publicData.collector_lock() as acquired:
            self.assertFalse(acquired)

        self.assertTrue(publicData.LOCK_PATH.exists())


if __name__ == "__main__":
    unittest.main()
