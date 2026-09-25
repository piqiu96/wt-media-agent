from __future__ import annotations

import os
from pathlib import Path
import importlib.util
import inspect
import socket
import sys
import tempfile
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from support import LoggingStateTestCase, UnusedBitBrowser, isolated_paths


class SidecarEntryTests(LoggingStateTestCase):
    """Restores logging state: one test below runs the real sidecar entry.

    `bootstrap.sidecar.run` assembles the Agent, which initializes the Logger
    (the user's ruling 二/五) and installs three file handlers on this test's
    throwaway directory. Leaving them installed is what CHG-057 T-06 measured
    as the source of the suite's stray `Logging error` tracebacks.
    """
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


class ReadinessLineTests(LoggingStateTestCase):
    """The line Desktop waits for before it calls anything (CHG-059 T-01).

    `wt-media-desktop`'s `sidecar/readiness.rs` parses this line as the first of
    its two readiness signals, and compares the port on it against the port it is
    about to call. Nothing checks the two repositories against each other at
    build time, so the format is pinned here — a reworded print fails in this
    repository rather than in every launch as a start that times out.

    Being exact about what the test below can say: the *format* is pinned by
    running the real `serve`, and the **ordering** Desktop depends on — "by the
    time this line exists, the socket accepts" — is **measured**, by connecting
    to the announced address at the instant the line is printed. What is not
    covered here is a frozen sidecar launched by the real Desktop: that is the
    real-machine arm in this change's evidence.

    One thing the line does **not** claim, registered rather than implied: it
    names the port `serve` was *asked* for, not the one the socket ended up on.
    In every real launch those are the same — Desktop passes a concrete
    `agent.port` — so the difference is unexercised, and a `port=0` launch would
    announce `0`. Left as it is deliberately: it is the requested port that
    Desktop compares against, and the two agree wherever it matters.
    """

    def test_the_readiness_line_is_printed_only_after_the_socket_accepts(self) -> None:
        from wt_media_agent.local_api.server import serve
        from wt_media_agent.local_api.state import LocalAgentState

        with isolated_paths():
            # A concrete port, not 0: the line names the requested port, so a
            # scratch port the kernel chose would not match what is printed.
            probe = socket.socket()
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
            probe.close()

            host = "127.0.0.1"
            expected = f"wt-media-agent local API listening on {host}:{port}"

            printed: list[str] = []
            accepting: list[bool] = []
            reached_the_loop: list[bool] = []

            def spy(*args, **kwargs):
                """Record the line instead of writing it.

                Swallowed rather than forwarded: the suite should not print the
                Agent's startup line for every run, and the text is what the
                assertions are about -- `printed` is the reading, stdout is not.
                """
                text = " ".join(str(arg) for arg in args)
                if text == expected:
                    # Exactly what Desktop does the instant it sees the line.
                    try:
                        with socket.create_connection((host, port), timeout=5):
                            accepting.append(True)
                    except OSError:
                        accepting.append(False)
                printed.append(text)

            def stop_here(self):
                """Let `serve` return so the test can end.

                Only the loop is replaced; the socket underneath is real, and
                `serve`'s `finally` still closes it. The control for this being
                a fair substitution is `reached_the_loop` below.
                """
                reached_the_loop.append(True)

            with mock.patch("builtins.print", spy), mock.patch.object(
                ThreadingHTTPServer, "serve_forever", stop_here
            ):
                serve(
                    host,
                    port,
                    bitbrowser=UnusedBitBrowser(),
                    state=LocalAgentState(agent_id="local-agent-dev", status="idle"),
                )

            self.assertIn(expected, printed, "the line Desktop parses, verbatim")
            self.assertEqual(reached_the_loop, [True], "the control: serve got this far")
            self.assertEqual(
                accepting,
                [True],
                "the line must not appear before the socket accepts connections",
            )


if __name__ == "__main__":
    unittest.main()
