"""Tests for the process assembly and the mode surfaces (CHG-056 T-04).

This replaces the `test_app.py` scaffold assertion. It asserts what the old
scaffold could not: that the assembled Agent is wired together -- one BitBrowser
client shared by every executor, every declared task type accounted for, and no
mode starting network work or demanding a token without being asked to.
"""

from __future__ import annotations

from dataclasses import replace
import importlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock
from urllib import error as urlerror
from urllib import request as urlrequest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from http.server import ThreadingHTTPServer

from support import LoggingStateTestCase, UnusedBitBrowser

from wt_media_agent import runtime
from wt_media_agent.bootstrap import cloud as cloud_mode
from wt_media_agent.bootstrap.app import build_components, database_path
from wt_media_agent.local_api.server import LocalApiServer, make_handler
from wt_media_agent.runtime.config import load_config
from wt_media_agent.runtime.constants import TASK_TYPE_MATERIAL_DOWNLOAD

#: An empty environment, so the developer's shell cannot decide a result.
SILENT_ENV: dict[str, str] = {}


def declared_task_types() -> set[str]:
    return {
        value
        for name, value in vars(runtime.constants).items()
        if name.startswith("TASK_TYPE_") and isinstance(value, str)
    }


class ComponentTestCase(LoggingStateTestCase):
    """Builds components against a throwaway repo root and an empty environment.

    Restores logging state on the way out, because `build_components` is the
    single Logger entry point (the user's ruling 二/五): assembling the Agent
    here installs three file handlers aimed at this test's throwaway directory,
    and a test that left them installed would hand every later test a handler
    whose file no longer exists. That is measured, not hypothetical -- leaving
    it installed made six later tests print `--- Logging error ---
    FileNotFoundError` (CHG-057 T-06).
    """

    def build(self, **env):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        config = load_config(
            config_dir=root / "config",
            env=env,
            frozen=False,
            repo_root=root,
            home=root / "home",
        )
        return build_components(config)


class AssemblyTests(ComponentTestCase):
    def setUp(self):
        # Before `self.build()`: it is what installs the handlers this test's
        # tearDown has to put back, and the state has to be captured first.
        super().setUp()
        self.components = self.build()
        self.config = self.components.config

    def test_every_browser_driven_executor_shares_the_assembled_client(self):
        """The property that self-construction made unassertable.

        `runner._executors` is private, but this is the only handle on what the
        runner was actually handed, and "what it was handed" is the whole point
        of T-04.
        """
        client = self.components.bitbrowser
        factories = self.components.runner._executors

        self.assertEqual(set(factories), declared_task_types())
        for task_type, factory in sorted(factories.items()):
            instance = factory(self.components.cloud, self.config.agent_id)
            own = getattr(instance, "bitbrowser", None)
            if own is not None:
                with self.subTest(task_type=task_type):
                    self.assertIs(own, client)

    def test_the_download_executor_reads_what_the_two_routes_write(self):
        """One object at each end, which is the whole of the assembly's job here.

        The bind route writes the node credential and the save-directory route
        writes the folder; the download executor reads both. Ends wired to
        *different* objects look exactly like this from either side -- an Agent
        that answers `has_node_credential: true` and then claims nothing, for the
        rest of its life, with nothing in any log to say why -- and
        `bootstrap/app.py` is the only place the two ends are joined.
        """
        credential, directory = "credential-the-bind-route-wrote", "/tmp/save-dir-from-the-route"
        self.components.node_credential.set(credential)
        self.components.save_directories.set(directory)

        executor = self.components.transfers._executors[TASK_TYPE_MATERIAL_DOWNLOAD](
            self.components.cloud, self.config.agent_id
        )

        self.assertEqual(executor._credential(), credential)
        self.assertEqual(executor._save_directory(), directory)
        # The loop's own callable is the second reader of the same object, and it
        # is the one that decides whether there is anything to claim at all.
        self.assertEqual(self.components.transfers._credential(), credential)

    def test_both_loops_dispatch_from_one_inventory(self):
        """The registry *is* the sharing: one mapping, read by both loops.

        Two registries built from the same factories would agree today and drift
        the first time either loop is handed something the other is not -- and
        the loop that is missing an entry does not fail, it refuses in a debug
        log line while Cloud waits out the lease.
        """
        runner_factories = self.components.runner._executors
        transfer_factories = self.components.transfers._executors

        self.assertEqual(set(transfer_factories), {TASK_TYPE_MATERIAL_DOWNLOAD})
        for task_type, factory in transfer_factories.items():
            self.assertIs(factory, runner_factories[task_type])

    def test_the_state_reports_the_configured_agent_id(self):
        """A configured id, not the dataclass default -- which happens to agree."""
        configured = "agent-7f3c"
        components = self.build(WT_MEDIA_AGENT_ID=configured)

        self.assertEqual(components.config.agent_id, configured)
        self.assertEqual(components.state.agent_id, configured)
        self.assertEqual(components.runner.config.agent_id, configured)

    def test_the_database_lives_under_the_configured_data_directory(self):
        path = database_path(self.config)

        self.assertEqual(path.parent, self.config.paths.data_dir)
        self.assertTrue(path.exists(), "assembly must have applied the schema")

    def test_the_token_stays_out_of_the_environment_facts(self):
        """The facts are meant to be pasted into a report, so: no credentials."""
        token = "runtime-token-must-not-travel"
        components = self.build(WT_MEDIA_AGENT_RUNTIME_TOKEN=token)

        self.assertEqual(components.config.runtime_token, token)
        self.assertNotIn(token, json.dumps(cloud_mode.environment_facts(components)))

    def test_a_credential_inside_a_configured_url_is_masked_in_the_environment_facts(self):
        """The other half of "no credentials": one can hide *inside* a value.

        `cloud.base_url` takes a `https://user:password@host` shape when a proxy
        or a private Cloud needs one, and its key looks harmless -- so the value
        is masked instead of trusted. Measured before the mask existed: the
        password reached the facts verbatim (CHG-057 T-06).
        """
        password, host = "pw123456", "cloud.example.test"
        components = self.build(
            WT_MEDIA_CLOUD_BASE_URL=f"https://alice:{password}@{host}/api"
        )

        facts = cloud_mode.environment_facts(components)
        # Positive controls first: the field is there and still readable, so the
        # negative below cannot pass by the value having been emptied.
        self.assertIn(host, str(facts["cloud_base_url"]))
        self.assertIn("alice", str(facts["cloud_base_url"]))
        self.assertNotIn(password, json.dumps(facts))


class CloudModeTests(ComponentTestCase):
    def test_cloud_mode_reports_and_returns_without_starting_the_runner(self):
        started = mock.MagicMock()
        components = replace(self.build(), runner=mock.MagicMock(start=started))

        with mock.patch.object(cloud_mode, "build_components", return_value=components):
            exit_code = cloud_mode.run()

        self.assertEqual(exit_code, 0)
        started.assert_not_called()

    def test_cloud_mode_starts_the_runner_only_when_the_switch_is_on(self):
        """Paired control for the test above: the switch really does gate it."""
        started = mock.MagicMock()
        components = replace(
            self.build(WT_MEDIA_AGENT_RUN_RUNNER="true"),
            runner=mock.MagicMock(start=started),
            # Both loops are stubbed, and both because run mode starts both: a
            # real `TransferRunner` here would poll Cloud on a daemon thread for
            # the rest of the run, against a base URL no test serves, and log
            # into a throwaway directory this test is about to delete.
            transfers=mock.MagicMock(),
        )
        self.assertTrue(components.config.run_runner)

        with mock.patch.object(cloud_mode, "build_components", return_value=components):
            cloud_mode.run()

        started.assert_called_once_with()


class ModeSurfaceWiringTests(ComponentTestCase):
    """The two API modes hand the server the objects its routes answer from.

    `serve` defaults every optional store to `None`, and the routes *answer* an
    absent store rather than ignoring it -- that is what the save-directory
    pair's 503 is for. The consequence is the defect this class exists for: a
    mode that forgot the argument served a machine that had chosen nothing, and
    said so in a way that is indistinguishable from the operator never having
    chosen. Measured in `bootstrap/sidecar.py` and `bootstrap/local.py` until
    CHG-061's assembly step; the console script additionally never started the
    task loop at all, which its own docstring claimed it did.
    """

    def _mode(self, name: str):
        return importlib.import_module(f"wt_media_agent.bootstrap.{name}")

    def _drive(self, mode, **env):
        """Run one mode surface with both of its side effects replaced.

        `start_task_loops` is replaced rather than allowed to run: it starts two
        threads that poll Cloud until `stop()`, and what this class is about is
        what the mode hands over, not what the loops then do with it.
        """
        components = self.build(**env)
        with mock.patch.object(mode, "build_components", return_value=components):
            with mock.patch.object(mode, "start_task_loops") as loops:
                with mock.patch.object(mode, "serve") as serve:
                    mode.run()
        return components, serve, loops

    def test_both_api_modes_hand_over_the_stores_assembly_built(self):
        for name in ("sidecar", "local"):
            with self.subTest(mode=name):
                components, serve, _ = self._drive(self._mode(name))

                # `.get` and not `[...]`: a keyword that was forgotten is the
                # defect this arm is about, and a `KeyError` would report it as
                # a broken test rather than as the answer it is.
                kwargs = serve.call_args.kwargs
                self.assertIs(kwargs.get("state"), components.state)
                self.assertIs(kwargs.get("checkpoint_store"), components.store)
                self.assertIs(kwargs.get("save_directory_store"), components.save_directories)
                # The credential the bind route writes and the download loop
                # reads is one object; a mode that passed a fresh holder would
                # leave both ends working and nothing ever claimed.
                self.assertIs(kwargs.get("node_credential"), components.node_credential)

    def test_both_api_modes_start_both_loops_when_the_switch_is_on(self):
        for name in ("sidecar", "local"):
            with self.subTest(mode=name):
                components, _, loops = self._drive(
                    self._mode(name), WT_MEDIA_AGENT_RUN_RUNNER="true"
                )

                loops.assert_called_once_with(components)

    def test_neither_api_mode_starts_a_loop_unless_it_was_asked_to(self):
        """Paired control: the switch is what decides, so the arms above mean something."""
        for name in ("sidecar", "local"):
            with self.subTest(mode=name):
                _, _, loops = self._drive(self._mode(name))

                loops.assert_not_called()


class HealthzAuthenticationTests(ComponentTestCase):
    """`/healthz` is open while no runtime token is configured, closed after.

    ADR-0016 §10 freezes the route's *body*; requiring the runtime token when
    one is configured is the accepted reading, and the two tests below are a
    pair so that "200" cannot mean "the handler ignores auth entirely".
    """

    def _get_healthz(self, token: str, header: str) -> tuple[int, dict[str, object]]:
        config = load_config(
            config_dir=Path(self._tmp_root) / "config",
            env={"WT_MEDIA_AGENT_RUNTIME_TOKEN": token},
            frozen=False,
            repo_root=Path(self._tmp_root),
            home=Path(self._tmp_root) / "home",
        )
        components = build_components(config)
        api = LocalApiServer(
            components.state,
            bitbrowser=UnusedBitBrowser(),
            checkpoint_store=components.store,
            auth_token=config.runtime_token,
        )
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(api))
        thread = threading.Thread(target=httpd.serve_forever)
        thread.start()
        try:
            request = urlrequest.Request(f"http://127.0.0.1:{httpd.server_port}/healthz")
            if header:
                request.add_header("authorization", header)
            try:
                # network-ok: the loopback HTTP server started above
                with urlrequest.urlopen(request) as response:
                    return response.status, json.loads(response.read())
            except urlerror.HTTPError as error:
                return error.code, json.loads(error.read())
        finally:
            httpd.shutdown()
            thread.join()
            httpd.server_close()

    def setUp(self):
        super().setUp()
        self.components = None
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self._tmp_root = tmp.name

    def test_healthz_is_open_when_no_token_is_configured(self):
        status, body = self._get_healthz("", "")

        self.assertEqual(status, 200)
        self.assertEqual(
            body, {"status": "ok", "service": "wt-media-agent", "mode": "m1"}
        )

    def test_healthz_rejects_a_request_without_the_configured_token(self):
        status, body = self._get_healthz("launch-token", "")

        self.assertEqual(status, 401)
        self.assertEqual(body, {"error": "unauthorized"})

    def test_healthz_accepts_the_configured_token(self):
        status, _ = self._get_healthz("launch-token", "Bearer launch-token")

        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
