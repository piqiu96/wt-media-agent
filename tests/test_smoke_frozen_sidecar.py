"""The release smoke probe must exercise a running binary, not file presence."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify" / "smoke_frozen_sidecar.py"


@unittest.skipIf(os.name == "nt", "fixture uses a POSIX shebang; Windows runner tests the real executable")
class SmokeFrozenSidecarTest(unittest.TestCase):
    def test_probe_requires_authenticated_running_health_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary) / "fake-sidecar"
            binary.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os\n"
                "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
                "class H(BaseHTTPRequestHandler):\n"
                " def do_GET(self):\n"
                "  if self.headers.get('Authorization') != 'Bearer '+os.environ['WT_MEDIA_AGENT_RUNTIME_TOKEN']:\n"
                "   self.send_response(401); self.end_headers(); return\n"
                "  data=json.dumps({'status':'ok','service':'wt-media-agent','mode':'m1'}).encode()\n"
                "  self.send_response(200); self.end_headers(); self.wfile.write(data)\n"
                "HTTPServer(('127.0.0.1',int(os.environ['WT_MEDIA_LOCAL_API_PORT'])),H).serve_forever()\n",
                encoding="utf-8",
            )
            binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
            result = subprocess.run([
                sys.executable, str(SCRIPT), "--binary", str(binary), "--timeout", "5",
            ], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("healthz ok", result.stdout)


if __name__ == "__main__":
    unittest.main()
