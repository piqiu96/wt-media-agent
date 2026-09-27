"""Where downloads land, and the two routes that decide it.

The operator's choice is the one piece of Agent state the Desktop writes and the
download executor reads, so most of what follows is about the *shape* of that
fact rather than about SQLite: it survives a restart, it is answered from what
was stored rather than from what was asked, and the facts beside it (`writable`,
`free_bytes`) are read when they are asked for rather than frozen at write time.

Every test that needs a directory uses one under `tmp` (via `tempfile`). That is
not tidiness: `scripts/test.sh` fails the whole suite if a test creates a new
path under the checkout's `.local/`, and a save-directory test is precisely the
kind that would.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib import request as urlrequest
from http.server import ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from support import UnusedBitBrowser

from wt_media_agent.local_api.server import LocalApiServer, make_handler
from wt_media_agent.local_api.state import LocalAgentState
from wt_media_agent.storage.migration import apply_migrations
from wt_media_agent.storage.save_directory import SAVE_DIRECTORY_KEY, SaveDirectoryStore


class StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "agent.db"
        apply_migrations(self.db_path)
        self.store = SaveDirectoryStore(self.db_path)

    def test_nothing_chosen_yet_reads_as_none_and_not_as_empty(self) -> None:
        """`None` is a state the operator can be shown; `""` is not.

        The Desktop draws "no folder chosen yet" from this, so a store that
        answered `""` would be inviting a caller to hand `Path("")` to a download.
        """
        self.assertIsNone(self.store.get())

    def test_the_choice_survives_a_new_store_over_the_same_file(self) -> None:
        """The whole reason it is stored: the Agent is restarted at will."""
        self.store.set("/tmp/wt-media-save")

        self.assertEqual(SaveDirectoryStore(self.db_path).get(), "/tmp/wt-media-save")

    def test_the_same_choice_pushed_twice_is_not_a_failure(self) -> None:
        """The Desktop pushes on every start, so a repeat is the normal case.

        An insert would raise on the second push and surface as "could not save
        your folder" for a folder that is already saved.
        """
        self.store.set("/tmp/wt-media-save")
        self.store.set("/tmp/wt-media-save")

        self.assertEqual(self.store.get(), "/tmp/wt-media-save")

    def test_the_latest_choice_replaces_the_earlier_one(self) -> None:
        self.store.set("/tmp/first")
        self.store.set("/tmp/second")

        self.assertEqual(self.store.get(), "/tmp/second")

    def test_a_row_holding_nothing_reads_as_no_choice(self) -> None:
        """`value` is `NOT NULL`, so "" is reachable and means "not chosen".

        Handled here rather than left to the caller because the two readings
        differ in what the Desktop draws, and a store that passed the empty
        string through would have every caller decide it separately.
        """
        with self.store._connect() as db:
            db.execute(
                "INSERT INTO agent_metadata (key, value, updated_at) VALUES (?, '', '')",
                (SAVE_DIRECTORY_KEY,),
            )

        self.assertIsNone(self.store.get())


class RouteTest(unittest.TestCase):
    """The two routes as a caller sees them, over a real loopback socket."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "agent.db"
        apply_migrations(self.db_path)
        self.store = SaveDirectoryStore(self.db_path)
        self.save_dir = Path(self._tmp.name) / "downloads"
        self.save_dir.mkdir()

    def _api(self, *, store: object = ...) -> LocalApiServer:
        if store is ...:
            store = self.store
        return LocalApiServer(
            LocalAgentState(),
            bitbrowser=UnusedBitBrowser(),
            save_directories=store,  # type: ignore[arg-type]
        )

    def _call(
        self, api: LocalApiServer, method: str, path: str, body: object = None
    ) -> tuple[int, dict[str, object]]:
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(api))
        thread = threading.Thread(target=httpd.serve_forever)
        thread.start()
        try:
            data = None if body is None else json.dumps(body).encode("utf-8")
            request = urlrequest.Request(
                f"http://127.0.0.1:{httpd.server_port}{path}", data=data, method=method
            )
            if data is not None:
                request.add_header("content-type", "application/json")
            try:
                # network-ok: the loopback HTTP server this call drives, started above
                with urlrequest.urlopen(request) as response:
                    return response.status, json.loads(response.read())
            except urlrequest.HTTPError as exc:
                return exc.code, json.loads(exc.read())
        finally:
            httpd.shutdown()
            thread.join()
            httpd.server_close()

    # ---- the facts ----

    def test_an_unchosen_directory_is_null_and_not_a_guess(self) -> None:
        """The user's ruling: `null` plus an executor that refuses to download.

        Answering some default directory here would look like a choice the
        operator never made, and the download would land where they did not ask.
        """
        status, body = self._call(self._api(), "GET", "/api/v1/save-directory")

        self.assertEqual(status, 200)
        self.assertEqual(
            body, {"data": {"save_dir": None, "writable": False, "free_bytes": 0}}
        )

    def test_a_chosen_directory_is_answered_with_its_facts(self) -> None:
        self.store.set(str(self.save_dir))

        status, body = self._call(self._api(), "GET", "/api/v1/save-directory")

        self.assertEqual(status, 200)
        data = body["data"]
        assert isinstance(data, dict)
        self.assertEqual(data["save_dir"], str(self.save_dir))
        self.assertIs(data["writable"], True)
        self.assertGreater(data["free_bytes"], 0)

    def test_the_write_answers_from_what_was_stored_not_from_the_request(self) -> None:
        """The response is the fact, not an echo.

        An echo would agree with a store that dropped the write, and the Desktop
        -- which has nothing else to check against -- would report success.
        """
        status, body = self._call(
            self._api(), "POST", "/api/v1/save-directory", {"save_dir": str(self.save_dir)}
        )

        self.assertEqual(status, 200)
        self.assertEqual(self.store.get(), str(self.save_dir))
        self.assertEqual(body["data"]["save_dir"], str(self.save_dir))

    def test_the_two_routes_agree_after_a_write(self) -> None:
        """The write's answer is produced the same way the read's is.

        `free_bytes` is left out of the comparison because it is a reading of a
        moving quantity -- anything else on the machine can write between the
        two calls, and an equality on it fails for a reason that has nothing to
        do with these routes (measured: a run where the two readings differed by
        16 KiB). The two fields compared are the ones the store decides.
        """
        api = self._api()
        written = self._call(
            api, "POST", "/api/v1/save-directory", {"save_dir": str(self.save_dir)}
        )[1]
        read = self._call(api, "GET", "/api/v1/save-directory")[1]

        def decided(body: dict[str, object]) -> dict[str, object]:
            data = dict(body["data"])  # type: ignore[arg-type]
            data.pop("free_bytes")
            return data

        self.assertEqual(decided(written), decided(read))

    def test_a_directory_that_became_unusable_after_the_write_is_reported_now(self) -> None:
        """`writable` and `free_bytes` are read, never stored.

        A directory is not a promise: it can be unmounted, filled or replaced by
        a file long after it was chosen. Storing "writable" at write time would
        have the Agent keep saying yes to a directory that is gone.
        """
        self.store.set(str(self.save_dir))
        self.save_dir.rmdir()

        status, body = self._call(self._api(), "GET", "/api/v1/save-directory")

        self.assertEqual(status, 200)
        self.assertEqual(
            body["data"],
            {"save_dir": str(self.save_dir), "writable": False, "free_bytes": 0},
        )

    @unittest.skipIf(os.geteuid() == 0, "root ignores the mode bits")
    def test_an_unwritable_directory_is_accepted_and_reported_as_unwritable(self) -> None:
        """Deliberate: the contract's 400 is for "not a directory", not for "full".

        Refusing the write would leave the operator unable to record a choice
        they can then fix (mount it, free space); accepting it and reporting
        `writable: false` lets the executor be the one that refuses, with a
        reason about the download rather than about the setting.
        """
        self.save_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(self.save_dir.chmod, stat.S_IRWXU)

        status, body = self._call(
            self._api(), "POST", "/api/v1/save-directory", {"save_dir": str(self.save_dir)}
        )

        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["writable"], False)
        self.assertEqual(self.store.get(), str(self.save_dir))

    # ---- refusals ----

    def test_a_relative_path_is_refused(self) -> None:
        """A relative path means a different directory for every process.

        The candidate is a real directory spelled relatively, which is the only
        form that tests the absolute-path requirement: `"downloads"` would be
        refused by the `is_dir()` check alone from any working directory that
        has no such subdirectory, and this arm would then be green with the
        requirement removed. Asserted rather than assumed, since it is the whole
        discriminating power of the case.
        """
        relative = os.path.relpath(self.save_dir)
        self.assertFalse(os.path.isabs(relative))
        self.assertTrue(os.path.isdir(relative), "the candidate must be a real directory")

        status, body = self._call(
            self._api(), "POST", "/api/v1/save-directory", {"save_dir": relative}
        )

        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "save_directory_invalid")
        self.assertIsNone(self.store.get())

    def test_an_absolute_path_that_is_not_a_directory_is_refused(self) -> None:
        missing = str(Path(self._tmp.name) / "nope")

        for candidate in (missing, str(self.save_dir / "file.mp4")):
            with self.subTest(save_dir=candidate):
                status, _ = self._call(
                    self._api(), "POST", "/api/v1/save-directory", {"save_dir": candidate}
                )
                self.assertEqual(status, 400)

    def test_a_missing_or_non_string_value_is_refused(self) -> None:
        for body in ({}, {"save_dir": None}, {"save_dir": 7}, {"save_dir": ["/tmp"]}):
            with self.subTest(body=body):
                status, _ = self._call(self._api(), "POST", "/api/v1/save-directory", body)
                self.assertEqual(status, 400)

    def test_no_refusal_quotes_the_path_back(self) -> None:
        """A refusal is read by an operator and logged; a path is neither.

        CHG-061 §8 keeps local absolute paths out of evidence and out of
        anything shipped elsewhere, and a message is what would carry one.
        """
        secretive = str(Path(self._tmp.name) / "operator-secret-folder")
        for body in ({"save_dir": secretive}, {"save_dir": "relative-secret-folder"}):
            with self.subTest(body=body):
                status, payload = self._call(
                    self._api(), "POST", "/api/v1/save-directory", body
                )
                self.assertEqual(status, 400)
                self.assertNotIn("secret", json.dumps(payload))

    def test_surrounding_whitespace_is_not_part_of_the_directory(self) -> None:
        """A pasted path arrives with a space often enough to matter.

        Trimmed before the absolute-path check, so " /tmp/x " is accepted as
        "/tmp/x" rather than refused as relative.
        """
        self._call(
            self._api(),
            "POST",
            "/api/v1/save-directory",
            {"save_dir": f"  {self.save_dir}  "},
        )

        self.assertEqual(self.store.get(), str(self.save_dir))

    # ---- a server with no store ----

    def test_a_server_without_a_store_says_so_rather_than_guessing(self) -> None:
        """503, not `null`: "nothing configured here" is not "nothing chosen".

        The 45-odd `LocalApiServer` call sites in the suite mostly pass no store;
        answering `save_dir: null` for them would be this surface claiming the
        operator had not chosen, which it has no way to know.
        """
        api = self._api(store=None)

        for method in ("GET", "POST"):
            with self.subTest(method=method):
                status, body = self._call(
                    api, method, "/api/v1/save-directory", {"save_dir": str(self.save_dir)}
                )
                self.assertEqual(status, 503)
                self.assertEqual(body["error"]["code"], "save_directory_unavailable")

    def test_an_unknown_path_is_still_a_404(self) -> None:
        status, _ = self._call(self._api(), "GET", "/api/v1/save-directory/")

        self.assertEqual(status, 404)

    def test_the_routes_are_behind_the_runtime_token(self) -> None:
        """`_check_auth` runs before the route table, so this is inherited.

        Asserted rather than assumed because the save directory is the one piece
        of state here that says where this machine writes files, and the route
        would be a fine way to read it if the check ever moved after the branch.
        """
        api = LocalApiServer(
            LocalAgentState(),
            bitbrowser=UnusedBitBrowser(),
            save_directories=self.store,
            auth_token="runtime-token",
        )

        for method in ("GET", "POST"):
            with self.subTest(method=method):
                status, _ = self._call(
                    api, method, "/api/v1/save-directory", {"save_dir": str(self.save_dir)}
                )
                self.assertEqual(status, 401)
        self.assertIsNone(self.store.get())


if __name__ == "__main__":
    unittest.main()
