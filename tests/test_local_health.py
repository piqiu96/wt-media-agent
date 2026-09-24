"""Tests for `GET /api/v1/health`, the aggregate health check (CHG-057 T-08).

Authority: architecture baseline §5.6 and ADR-0016 §10. `/healthz` is a frozen
contract path (`http/local_agent.rs` consumes it and must keep working), and
`/api/v1/health` aggregates Agent version, Agent status, Cloud, BitBrowser and
Storage. It must not trigger any outbound *write* request (heartbeat included)
and it must not raise because a dependency is unavailable -- the dependency's
status degrades to `abnormal` or `unknown` and the endpoint still answers 200.

Two arms per dependency on purpose. A *known* dependency failure
(`ConnectionRefusedError`, `BitBrowserResponseError`, a missing database) has to
land on the specific value that names it; a failure nobody anticipated has to
land on `unknown` **and be logged**, because an endpoint that folds a
`ZeroDivisionError` into a plausible-looking `abnormal` hides the bug the next
reader needs.

The "no outbound write" half cannot be proved by a double: a double cannot say
what the real socket did. It is proved in two other places -- the module's
import shape below, and the real-process arm in
`evidence/task-08-health.md`, where a scratch TCP listener standing in for Cloud
accepts the connection and reports **zero bytes received**.
"""

from __future__ import annotations

import ast
import json
import socket
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
import sys
from urllib import request as urlrequest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from support import isolated_paths

from wt_media_agent.clients.bitbrowser import BitBrowserResponseError
from wt_media_agent.local_api.health import (
    ABNORMAL,
    NORMAL,
    UNKNOWN,
    UNREACHABLE,
    CloudReachability,
    health_report,
)
from wt_media_agent.local_api.server import LocalApiServer, make_handler
from wt_media_agent.local_api.state import LocalAgentState
from wt_media_agent.runtime.version import __version__
from wt_media_agent.storage.checkpoint_store import CheckpointStore
from wt_media_agent.storage.migration import apply_migrations


CLOUD_URL = "http://127.0.0.1:18080"


class ScratchCloud:
    """A listening loopback socket standing in for Cloud.

    Nothing here speaks HTTP: `normal` is the answer to "can a socket be
    opened", so a listener that accepts and reads nothing is the whole of Cloud
    as far as this endpoint is concerned -- which is the point.
    """

    def __enter__(self) -> "ScratchCloud":
        self._listener = socket.socket()
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(4)
        self._listener.settimeout(5)
        self.url = f"http://127.0.0.1:{self._listener.getsockname()[1]}"
        return self

    def __exit__(self, *exception) -> bool:
        self._listener.close()
        return False

    def accepted_bytes(self) -> bytes:
        """What the probe sent, read after it closed (so this cannot block)."""
        connection, _ = self._listener.accept()
        with connection:
            return connection.recv(1024)


class HealthyBrowser:
    def group_list(self):
        return []


class UnreachableBrowser:
    """A dead BitBrowser port: `_post_json` wraps the URLError, not the socket."""

    def group_list(self):
        raise BitBrowserResponseError(
            "BitBrowser Local API request failed: [Errno 61] Connection refused"
        )


class BrokenBrowser:
    """A bug, not a dependency failure."""

    def group_list(self):
        raise ZeroDivisionError("broke")


class ClosedSocket:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class RecordingConnect:
    """Records the connect and refuses to be anything else.

    `__getattr__` raises, so a probe that tried to *use* the connection (send,
    recv, makefile) cannot pass this double by accident: the only calls the
    Cloud probe is allowed to make are `connect` and the returned `close`.
    """

    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.calls: list[tuple[tuple[str, int], float]] = []
        self.sockets: list[ClosedSocket] = []

    def __call__(self, target, timeout):
        self.calls.append((target, timeout))
        if self.error is not None:
            raise self.error
        sock = ClosedSocket()
        self.sockets.append(sock)
        return sock


def healthy_report(**overrides) -> dict[str, object]:
    """The aggregate with every dependency answering, then one thing changed.

    `store=None` is the one hole: it reads `unknown` by design, so a test that
    wants a fully `ok` roll-up has to pass a real migrated store.
    """
    arguments = {
        "state": LocalAgentState(agent_id="local-agent-dev", status="idle"),
        "bitbrowser": HealthyBrowser(),
        "store": None,
        "cloud": CloudReachability(base_url=CLOUD_URL, connect=RecordingConnect()),
    }
    arguments.update(overrides)
    return health_report(**arguments).to_dict()


class AggregateShapeTest(unittest.TestCase):
    def test_the_aggregate_reports_version_agent_status_and_three_dependencies(self) -> None:
        with isolated_paths() as paths:
            store = CheckpointStore(paths.data / "agent.db")
            apply_migrations(paths.data / "agent.db")

            body = healthy_report(store=store)

        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["service"], "wt-media-agent")
        self.assertEqual(body["agent_version"], __version__)
        self.assertEqual(body["agent_status"], "idle")
        self.assertEqual(
            body["dependencies"],
            {
                "cloud": {"status": NORMAL},
                "bitbrowser": {"status": NORMAL},
                "storage": {"status": NORMAL},
            },
        )

    def test_the_aggregate_is_not_a_renamed_status_route(self) -> None:
        """`/api/v1/status` answers a much wider question; this one must not."""
        body = healthy_report()

        for absent in ("bit_profile_ids", "main_user_id", "disk", "python_version",
                       "operating_system", "ffmpeg", "pending_result_count"):
            self.assertNotIn(absent, body)

    def test_the_agent_status_is_the_observable_state_not_the_dependency_roll_up(self) -> None:
        with isolated_paths() as paths:
            apply_migrations(paths.data / "agent.db")

            body = healthy_report(
                state=LocalAgentState(status="running"),
                store=CheckpointStore(paths.data / "agent.db"),
            )

        self.assertEqual(body["agent_status"], "running")
        self.assertEqual(body["status"], "ok")


class DegradedDependencyTest(unittest.TestCase):
    """The ruling's arm: each dependency unavailable, none of them fatal."""

    def test_cloud_refused_degrades_to_abnormal_without_raising(self) -> None:
        body = healthy_report(
            cloud=CloudReachability(
                base_url=CLOUD_URL, connect=RecordingConnect(ConnectionRefusedError("refused"))
            )
        )

        self.assertEqual(body["dependencies"]["cloud"], {"status": ABNORMAL})
        self.assertEqual(body["status"], "abnormal")

    def test_cloud_timeout_degrades_to_abnormal(self) -> None:
        body = healthy_report(
            cloud=CloudReachability(
                base_url=CLOUD_URL, connect=RecordingConnect(socket.timeout("timed out"))
            )
        )

        self.assertEqual(body["dependencies"]["cloud"], {"status": ABNORMAL})

    def test_bitbrowser_unreachable_degrades_to_unreachable(self) -> None:
        """Named the same way `/api/v1/status` names it, so the two can be compared."""
        body = healthy_report(bitbrowser=UnreachableBrowser())

        self.assertEqual(body["dependencies"]["bitbrowser"], {"status": UNREACHABLE})
        self.assertEqual(body["status"], "abnormal")

    def test_storage_that_cannot_be_read_degrades_to_abnormal(self) -> None:
        with isolated_paths() as paths:
            store = CheckpointStore(paths.data / "absent.db")

            body = healthy_report(store=store)

        self.assertEqual(body["dependencies"]["storage"], {"status": ABNORMAL})
        self.assertEqual(body["status"], "abnormal")

    def test_one_dead_dependency_does_not_hide_the_others(self) -> None:
        with isolated_paths() as paths:
            apply_migrations(paths.data / "agent.db")

            body = healthy_report(
                store=CheckpointStore(paths.data / "agent.db"),
                bitbrowser=UnreachableBrowser(),
            )

        self.assertEqual(body["status"], "abnormal")
        self.assertEqual(body["dependencies"]["bitbrowser"]["status"], UNREACHABLE)
        self.assertEqual(body["dependencies"]["storage"]["status"], NORMAL)
        self.assertEqual(body["dependencies"]["cloud"]["status"], NORMAL)
        self.assertEqual(body["agent_status"], "idle")

    def test_an_unexpected_failure_degrades_to_unknown_and_is_logged(self) -> None:
        """Not `abnormal`: nobody knows what that was, and the log has to say so."""
        with self.assertLogs("wt_media_agent.local_api.health", level="WARNING") as captured:
            body = healthy_report(bitbrowser=BrokenBrowser())

        self.assertEqual(body["dependencies"]["bitbrowser"], {"status": UNKNOWN})
        self.assertEqual(body["status"], "abnormal")
        self.assertTrue(
            any("local_api.health.bitbrowser.degraded" in line for line in captured.output),
            captured.output,
        )

    def test_no_cloud_endpoint_and_no_store_are_unknown_not_abnormal(self) -> None:
        """"This process has no Cloud configured" is not a Cloud outage."""
        body = healthy_report(cloud=CloudReachability(base_url=""), store=None)

        self.assertEqual(body["dependencies"]["cloud"], {"status": UNKNOWN})
        self.assertEqual(body["dependencies"]["storage"], {"status": UNKNOWN})
        self.assertEqual(body["status"], "abnormal")


class CloudReachabilityTest(unittest.TestCase):
    def test_the_configured_host_and_port_are_what_is_contacted(self) -> None:
        connect = RecordingConnect()

        status = CloudReachability(base_url=CLOUD_URL, timeout=1.5, connect=connect).status()

        self.assertEqual(status, NORMAL)
        self.assertEqual(connect.calls, [(("127.0.0.1", 18080), 1.5)])
        self.assertEqual([sock.closed for sock in connect.sockets], [True])

    def test_the_scheme_decides_the_default_port(self) -> None:
        connect = RecordingConnect()

        CloudReachability(base_url="https://cloud.example", connect=connect).status()

        self.assertEqual(connect.calls, [(("cloud.example", 443), 2.0)])

    def test_nothing_that_is_not_an_http_endpoint_is_probed(self) -> None:
        for endpoint in ("", "   ", "not a url", "http://", "ftp://host:21", "localhost:18080"):
            with self.subTest(endpoint=endpoint):
                connect = RecordingConnect()

                status = CloudReachability(base_url=endpoint, connect=connect).status()

                self.assertEqual(status, UNKNOWN)
                self.assertEqual(connect.calls, [])

    def test_a_port_that_is_not_a_number_is_unknown_rather_than_a_crash(self) -> None:
        connect = RecordingConnect()

        status = CloudReachability(base_url="http://127.0.0.1:abc", connect=connect).status()

        self.assertEqual(status, UNKNOWN)
        self.assertEqual(connect.calls, [])

    def test_the_default_probe_is_a_real_connection(self) -> None:
        """Injected doubles prove the branches; this proves the shipped default.

        An `OSError` out of `status()` is the contract -- `health_report` is
        where it becomes `abnormal`, so that the log keeps the refusal's own
        message rather than a flattened one.
        """
        with ScratchCloud() as cloud:
            self.assertEqual(CloudReachability(base_url=cloud.url).status(), NORMAL)

        with self.assertRaises(OSError):
            CloudReachability(base_url=cloud.url).status()


class StorageProbeTest(unittest.TestCase):
    """`CheckpointStore.probe` reads one row and writes nothing."""

    def test_a_migrated_database_probes_clean(self) -> None:
        with isolated_paths() as paths:
            apply_migrations(paths.data / "agent.db")
            store = CheckpointStore(paths.data / "agent.db")

            store.probe()

    def test_a_missing_database_fails_and_is_not_created(self) -> None:
        with isolated_paths() as paths:
            db_path = paths.data / "absent.db"
            store = CheckpointStore(db_path)

            with self.assertRaises(FileNotFoundError):
                store.probe()

            self.assertFalse(db_path.exists(), "a probe must not create a database")

    def test_a_database_with_no_schema_fails(self) -> None:
        with isolated_paths() as paths:
            db_path = paths.data / "empty.db"
            db_path.touch()
            store = CheckpointStore(db_path)

            with self.assertRaises(Exception):
                store.probe()

    def test_a_probe_leaves_no_row_behind(self) -> None:
        with isolated_paths() as paths:
            apply_migrations(paths.data / "agent.db")
            store = CheckpointStore(paths.data / "agent.db")

            store.probe()

            self.assertEqual(store.get_incomplete_checkpoints(), [])
            self.assertEqual(store.get_undelivered_results(), [])


class RouteTest(unittest.TestCase):
    """The route as a caller sees it, over a real loopback socket."""

    def _get(self, api, path):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(api))
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            with urlrequest.urlopen(f"http://127.0.0.1:{server.server_port}{path}") as response:
                return response.status, response.read()
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

    def test_the_aggregate_route_answers_200(self) -> None:
        with isolated_paths() as paths, ScratchCloud() as cloud:
            apply_migrations(paths.data / "agent.db")
            api = LocalApiServer(
                LocalAgentState(agent_id="local-agent-dev", status="idle"),
                bitbrowser=HealthyBrowser(),
                checkpoint_store=CheckpointStore(paths.data / "agent.db"),
                cloud_base_url=cloud.url,
            )

            status, body = self._get(api, "/api/v1/health")

            self.assertEqual(cloud.accepted_bytes(), b"", "the probe sent a request")

        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["agent_version"], __version__)
        self.assertEqual(payload["dependencies"]["cloud"], {"status": NORMAL})

    def test_the_route_answers_200_when_every_dependency_is_down(self) -> None:
        """A real store, pointed at a database that is not there.

        `store=None` would also read `unknown`, but for the other reason -- it is
        the "this process has no storage configured" path, and it would let this
        arm pass with the storage probe's failure handling removed.
        """
        with isolated_paths() as paths:
            api = LocalApiServer(
                bitbrowser=UnreachableBrowser(),
                checkpoint_store=CheckpointStore(paths.data / "absent.db"),
                cloud_base_url="http://127.0.0.1:1",
            )

            status, body = self._get(api, "/api/v1/health")

        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["status"], "abnormal")
        self.assertEqual(payload["dependencies"]["bitbrowser"], {"status": UNREACHABLE})
        self.assertEqual(payload["dependencies"]["storage"], {"status": ABNORMAL})
        self.assertEqual(payload["dependencies"]["cloud"], {"status": ABNORMAL})

    def test_healthz_is_byte_for_byte_unchanged(self) -> None:
        """`http/local_agent.rs` parses this body; the aggregate must not move it."""
        api = LocalApiServer(
            bitbrowser=HealthyBrowser(),
            cloud_base_url="http://127.0.0.1:1",
        )

        status, body = self._get(api, "/healthz")

        self.assertEqual(status, 200)
        self.assertEqual(body, b'{"status":"ok","service":"wt-media-agent","mode":"m1"}')

    def test_the_two_paths_are_separate(self) -> None:
        """/healthz stays the frozen three keys even where the aggregate is rich."""
        api = LocalApiServer(bitbrowser=HealthyBrowser())

        _, frozen = self._get(api, "/healthz")
        _, aggregate = self._get(api, "/api/v1/health")

        self.assertEqual(sorted(json.loads(frozen)), ["mode", "service", "status"])
        self.assertIn("dependencies", json.loads(aggregate))


class WiringTest(unittest.TestCase):
    """The failure mode of T-07: a probe that is never handed its dependency."""

    def test_the_production_entry_points_forward_the_cloud_endpoint(self) -> None:
        """All three `serve(...)` call sites, because a missed one reads
        `unknown` forever and looks exactly like an unconfigured Cloud."""
        source = (ROOT / "src" / "wt_media_agent" / "local_api" / "server.py").read_text()
        module = ast.parse(source)

        main_calls = [
            node
            for node in ast.walk(module)
            if isinstance(node, ast.Call)
            and getattr(node.func, "id", "") == "serve"
        ]
        self.assertEqual(len(main_calls), 1)
        self.assertIn("cloud_base_url", {kw.arg for kw in main_calls[0].keywords})

        for entry in ("local.py", "sidecar.py"):
            path = ROOT / "src" / "wt_media_agent" / "bootstrap" / entry
            tree = ast.parse(path.read_text())
            calls = [
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "serve"
            ]
            with self.subTest(entry=entry):
                self.assertEqual(len(calls), 1)
                self.assertIn("cloud_base_url", {kw.arg for kw in calls[0].keywords})

    def test_the_health_module_imports_no_http_client(self) -> None:
        """The probe reaches Cloud with a socket and nothing else.

        A `urllib.request` import here would make "the aggregate never sends a
        write request" a promise instead of something the source can be checked
        for; the only outbound call this module is allowed to make is
        `socket.create_connection`.
        """
        source = (ROOT / "src" / "wt_media_agent" / "local_api" / "health.py").read_text()
        tree = ast.parse(source)

        # Both spellings of the same import: `import urllib.request` and
        # `from urllib import request`. The first version of this check collected
        # only `node.module` for the `from` form, so it saw "urllib" and missed
        # "urllib.request" -- which the mutation probe caught by adding exactly
        # that import and watching this test stay green.
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                imported.append(module)
                imported.extend(f"{module}.{alias.name}" for alias in node.names)

        forbidden = [
            name
            for name in imported
            if name.startswith(
                ("urllib.request", "urllib.error", "http.client", "requests", "aiohttp", "httpx")
            )
        ]
        self.assertEqual(forbidden, [], f"health.py must not import {forbidden}")


if __name__ == "__main__":
    unittest.main()
