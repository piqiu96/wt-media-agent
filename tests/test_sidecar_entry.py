from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class SidecarEntryTests(unittest.TestCase):
    def test_sidecar_entry_starts_local_api_on_loopback(self) -> None:
        from wt_media_agent import sidecar_main

        with mock.patch.object(sidecar_main.local_api_server, "main", return_value=0) as main:
            self.assertEqual(sidecar_main.main(), 0)

        main.assert_called_once_with(["--host", "127.0.0.1", "--port", "8765"])


if __name__ == "__main__":
    unittest.main()
