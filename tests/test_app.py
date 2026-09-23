from __future__ import annotations

import unittest
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wt_media_agent.app import create_app


class AgentScaffoldTests(unittest.TestCase):
    def test_create_app_runs_local_mode(self) -> None:
        app = create_app("local")

        self.assertEqual(app.mode, "local")
        self.assertEqual(app.run(), 0)


if __name__ == "__main__":
    unittest.main()
