from __future__ import annotations

import os
from pathlib import Path
import importlib.util
import inspect
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class SidecarEntryTests(unittest.TestCase):
    def test_macos_build_uses_adhoc_signing_for_embedded_python(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "build_desktop_sidecar", ROOT / "scripts" / "build_desktop_sidecar.py"
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)

        self.assertEqual(module.pyinstaller_signing_args("darwin"), ["--codesign-identity", "-"])
        self.assertEqual(
            module.python_library_resign_args("darwin"),
            ["codesign", "--force", "--sign", "-"],
        )
        self.assertEqual(module.python_library_resign_args("windows"), [])

    def test_the_sidecar_entry_delegates_to_the_bootstrap_surface(self) -> None:
        """The entry is a delegate; assembly lives in `bootstrap` (ADR-0016 §2).

        `--host`/`--port` used to be spelled out here. They are now configuration
        so that Desktop can set them per launch, which the test below covers.
        """
        from wt_media_agent import sidecar_main

        with mock.patch.object(sidecar_main.sidecar, "run", return_value=0) as run:
            self.assertEqual(sidecar_main.main(), 0)

        run.assert_called_once_with()

    def test_the_four_controlled_variables_reach_the_api_surface(self) -> None:
        """Desktop's whole contract with a sidecar is these four variables.

        Read through the real path -- environment, `runtime/config.py`,
        `bootstrap` -- and observed at the one place that binds the socket, so
        a variable that stopped being honoured would show up as a wrong bind
        rather than as an untested assumption.
        """
        from wt_media_agent.bootstrap import sidecar as sidecar_surface

        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "WT_MEDIA_LOCAL_API_HOST": "127.0.0.1",
                "WT_MEDIA_LOCAL_API_PORT": "38999",
                "WT_MEDIA_AGENT_RUNTIME_TOKEN": "launch-token",
                "WT_MEDIA_AGENT_DATA_DIR": str(Path(tmp) / "data"),
            }
            with mock.patch.dict(os.environ, env, clear=False):
                with mock.patch.object(sidecar_surface, "serve") as serve:
                    self.assertEqual(sidecar_surface.run(), 0)

            args, kwargs = serve.call_args
            self.assertEqual(args[:2], ("127.0.0.1", 38999))
            self.assertEqual(kwargs["auth_token"], "launch-token")
            # Observable rather than internal: the data directory the variable
            # named is where assembly put the database.
            self.assertTrue((Path(tmp) / "data" / "local-agent.sqlite3").is_file())

    def test_no_sidecar_entry_accepts_a_command_line_argument(self) -> None:
        """Why the token cannot leak into `ps`: nothing here takes argv.

        The sidecar entry used to build `["--host", "127.0.0.1", "--port",
        "8765"]` and hand it to `local_api.server.main`. Both halves are gone:
        `main` here takes no parameters, and the surface it delegates to takes
        none either, so there is no path from a command line into this process.
        """
        from wt_media_agent import sidecar_main
        from wt_media_agent.bootstrap import sidecar as sidecar_surface

        self.assertEqual(list(inspect.signature(sidecar_main.main).parameters), [])
        self.assertEqual(list(inspect.signature(sidecar_surface.run).parameters), [])


if __name__ == "__main__":
    unittest.main()
