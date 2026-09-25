from __future__ import annotations

import os
from pathlib import Path
import importlib.util
import inspect
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
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


class SigtermTests(LoggingStateTestCase):
    """SIGTERM is answered, and its arrival does not cut in-flight requests off.

    Desktop's exit path (`wt-media-desktop/src-tauri/src/sidecar/mod.rs`, CHG-059
    T-03) sends SIGTERM and waits a grace window before it kills. For that to be
    anything other than a slower kill, two things have to be true here:

    1. SIGTERM is **answered** — the process leaves by its ordinary exit path,
       which is the only one that runs `server_close()`;
    2. that path **waits** for a request already being served.

    Both are measured on a real child process, because both are claims about a
    signal and about process exit, and neither exists in-process: SIGTERM's
    default disposition is what the red reading is about, and `signal.signal`
    only works on the main thread, so a test that called `serve` on a thread
    could not install the handler it is testing.

    The driver is a real `serve` with stubbed components (`UnusedBitBrowser`),
    the same substitution `ReadinessLineTests` makes: the socket, the request
    handling and the exit path are production's.
    """

    HOST = "127.0.0.1"
    #: How long the exit is allowed to take when nothing is in flight. The
    #: server polls its shutdown flag every 0.5 s (`serve_forever`'s default),
    #: so this is that plus a wide margin -- and it is asserted as a **bounded**
    #: time, not as "eventually", because the point of the second test is the
    #: difference between this and a request that is still being served.
    FREE_EXIT_SECONDS = 1.5

    def _driver(self, directory: Path, port: int) -> Path:
        script = directory / "serve_driver.py"
        script.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(ROOT / 'src')!r})\n"
            f"sys.path.insert(0, {str(ROOT / 'tests')!r})\n"
            "from wt_media_agent.local_api.server import serve\n"
            "from wt_media_agent.local_api.state import LocalAgentState\n"
            "from support import UnusedBitBrowser\n"
            f"serve({self.HOST!r}, {port}, bitbrowser=UnusedBitBrowser(),\n"
            "      state=LocalAgentState(agent_id='local-agent-dev', status='idle'))\n",
            encoding="utf-8",
        )
        return script

    def _scratch_port(self) -> int:
        probe = socket.socket()
        probe.bind((self.HOST, 0))
        port = probe.getsockname()[1]
        probe.close()
        return port

    def _start(self, paths, directory: Path):
        """A real `serve` in its own process, plus its stdout as it arrives."""
        port = self._scratch_port()
        environment = dict(os.environ)
        environment["WT_MEDIA_AGENT_DATA_DIR"] = str(paths.data)
        process = subprocess.Popen(
            [sys.executable, "-u", str(self._driver(directory, port))],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=environment,
        )
        # Registered in this order because cleanups run last-in-first-out: the
        # process is killed before its stdout is closed, not after.
        self.addCleanup(process.stdout.close)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        lines: list[str] = []
        threading.Thread(
            target=lambda: [lines.append(line) for line in process.stdout],
            daemon=True,
        ).start()

        announcement = f"wt-media-agent local API listening on {self.HOST}:{port}"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if announcement in "".join(lines):
                return process, lines, port
            if process.poll() is not None:
                break
            time.sleep(0.02)
        self.fail(f"the driver never announced readiness: {''.join(lines)!r}")

    def _sigterm_a_serving_agent(self, *, hold_a_request: bool):
        """Start the driver, optionally park a request mid-flight, send SIGTERM.

        The held connection sends a request line and nothing more, so the handler
        thread exists and is blocked reading the headers: that is a request
        **already being served**, which is the state `server_close()` joins on.
        The caller closes it to release the thread — the read returns empty and
        the handler finishes.
        """
        paths = self.enterContext(isolated_paths())
        directory = self.enterContext(tempfile.TemporaryDirectory())
        process, lines, port = self._start(paths, Path(directory))

        held = None
        if hold_a_request:
            held = socket.create_connection((self.HOST, port), timeout=5)
            self.addCleanup(held.close)
            held.sendall(b"GET /healthz HTTP/1.1\r\n")

        process.send_signal(signal.SIGTERM)
        return process, lines, held

    def test_an_in_process_serve_restores_the_disposition_it_displaced(self) -> None:
        """A signal disposition is process-wide, so `serve` must put the old one back.

        The subprocess tests above cannot see this: their process dies with the
        handler still installed and nobody is left to notice. The caller that can
        notice is an in-process one -- `ReadinessLineTests` above is exactly that,
        and so is any embedding of this server -- where a leftover
        `wt-media-sigterm` handler would keep answering SIGTERM for the rest of
        the host process's life.
        """
        from wt_media_agent.local_api.server import serve
        from wt_media_agent.local_api.state import LocalAgentState

        before = signal.getsignal(signal.SIGTERM)
        with isolated_paths(), mock.patch.object(
            ThreadingHTTPServer, "serve_forever", lambda self: None
        ):
            serve(
                self.HOST,
                0,
                bitbrowser=UnusedBitBrowser(),
                state=LocalAgentState(agent_id="local-agent-dev", status="idle"),
            )

        self.assertEqual(
            signal.getsignal(signal.SIGTERM),
            before,
            "`serve` installs a SIGTERM handler, so it owes the caller the "
            "disposition it displaced; without that, whoever called `serve` "
            "answers SIGTERM as this server for good",
        )

    def test_a_sigterm_is_answered_with_the_ordinary_exit_path(self) -> None:
        process, lines, _ = self._sigterm_a_serving_agent(hold_a_request=False)

        self.assertEqual(
            process.wait(timeout=10),
            0,
            "SIGTERM must reach the ordinary exit path; the reading this rules out "
            f"is the default disposition, which reports -{signal.SIGTERM} and never "
            f"reaches `server_close()`; stdout was {''.join(lines)!r}",
        )
        self.assertIn(
            "wt-media-agent local API stopped",
            "".join(lines),
            "the line is printed after `server_close()`, so its presence is what "
            f"separates 'closed' from 'died'; stdout was {''.join(lines)!r}",
        )

    def test_a_request_in_flight_when_the_signal_arrives_is_waited_for(self) -> None:
        process, lines, held = self._sigterm_a_serving_agent(hold_a_request=True)

        time.sleep(self.FREE_EXIT_SECONDS + 0.5)
        self.assertIsNone(
            process.poll(),
            "a request still being served must hold the exit open; the process was "
            f"gone after {self.FREE_EXIT_SECONDS + 0.5:.1f}s while the connection "
            f"was open; stdout was {''.join(lines)!r}",
        )
        # The ordering Desktop's reader depends on. It reads the tail of the
        # sidecar's output to tell "asked and closed" from "had to be killed", so
        # a line printed *before* the join would answer the wrong question.
        self.assertNotIn(
            "wt-media-agent local API stopped",
            "".join(lines),
            "that line is printed after the join, so it must not exist while a "
            f"request is still being served; stdout was {''.join(lines)!r}",
        )

        held.close()
        self.assertEqual(
            process.wait(timeout=10),
            0,
            "the held request must be the only thing delaying the exit, so releasing "
            f"it must be followed by the ordinary one; stdout was {''.join(lines)!r}",
        )
        self.assertIn("wt-media-agent local API stopped", "".join(lines))

    def test_with_nothing_in_flight_the_exit_is_prompt(self) -> None:
        """The same setup with nothing in flight exits *well* inside the window.

        Without this, "still alive after 2 s with a request in flight" has a
        second explanation — an exit path that is simply slow — and the reading
        would be about timing rather than about the join.
        """
        process, _, _ = self._sigterm_a_serving_agent(hold_a_request=False)

        started = time.monotonic()
        process.wait(timeout=10)
        self.assertLess(
            time.monotonic() - started,
            self.FREE_EXIT_SECONDS,
            "so the in-flight test's window is longer than this exit takes",
        )


if __name__ == "__main__":
    unittest.main()
