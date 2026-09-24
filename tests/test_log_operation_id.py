"""CHG-057 T-21: one id per request, where it is set, and how far it reaches.

D-10 said `operation_id` is not delivered across the two ends this CHG; each side
may generate its own internally. T-17 did the Desktop half -- one id per Agent
session, on the `agent.supervisor` records. This is the Agent half: one id per
*HTTP request*, rendered as a field on the records that request writes, so the
lines one request produced can be read back as a group.

Nothing goes on the wire for it: no header, no query, no environment variable.
T-17 pinned the Desktop's outbound request byte-for-byte for the same reason, and
this side must not undo that -- so the responses here are asserted to not carry
the id either.

The boundary is `AgentHandler.handle_one_request`, set before the request line is
parsed and reset in a `finally`. That placement is what makes the id a property of
every request instead of a property of the routes somebody remembered to touch: a
401, a 404 and a malformed request line are all inside it. Only routes that write
a record can show it in a file, so the "rejected before it logged" half is
asserted structurally (see `test_the_id_is_set_before_the_request_line_is_read`).
"""

from __future__ import annotations

import http.client
import inspect
import json
import logging
import re
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from support import LoggingStateTestCase, UnusedBitBrowser

from wt_media_agent.local_api.server import LocalApiServer, make_handler
from wt_media_agent.local_api.state import LocalAgentState
from wt_media_agent.runtime.config import load_config
from wt_media_agent.runtime.logging import (
    AGENT_LOG_NAME,
    ERROR_LOG_NAME,
    OPERATION_ID_FIELD,
    begin_operation,
    configure_from,
    end_operation,
)

#: A route that writes a record and returns 400 without consulting BitBrowser:
#: an empty body means `profile_id` is missing, which is rejected before any
#: dependency is reached. Using it keeps this module's server hermetic -- nothing
#: here can reach BitBrowser, Cloud or the checkout.
REJECTING_ROUTE = "/api/v1/bit-browser/profile-open"

#: The route's record, whose message names no id of its own.
REJECT_RECORD = "local_api.profile_open.reject"

#: `secrets.token_hex(8)`, the generator's shape as it reaches the line.
ID_PATTERN = re.compile(rf"\b{OPERATION_ID_FIELD}=([0-9a-f]{{16}})\b")


class OperationIdLineTest(LoggingStateTestCase):
    """The field as a line reads it: present in a request, absent outside one."""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.directory = Path(self._tmp.name)
        configure_from(
            load_config(
                env={"WT_MEDIA_LOG_FILE": str(self.directory / AGENT_LOG_NAME)},
                frozen=False,
                repo_root=Path("/repo"),
                home=Path("/Users/dev"),
            )
        )
        self.logger = logging.getLogger("wt_media_agent.somewhere")

    def line(self, index: int = -1) -> str:
        """The last record written to `agent.log`, as the file holds it."""
        lines = (self.directory / AGENT_LOG_NAME).read_text().splitlines()
        self.assertTrue(lines, "positive control: the file has records in it")
        return lines[index]

    def test_a_record_with_no_request_around_it_is_unchanged(self):
        """The whole point of an empty field: today's lines keep today's shape.

        Not "contains no id" -- the message must still be the first thing after
        the logger name, which is what a field rendered as `""` buys and what a
        field rendered as `operation_id=none` would cost.
        """
        self.logger.info("local_api.status.start")

        line = self.line()
        self.assertRegex(
            line,
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2} \[INFO\] "
            r"wt_media_agent\.somewhere: local_api\.status\.start$",
        )
        self.assertNotIn(OPERATION_ID_FIELD, line)

    def test_the_field_sits_after_the_logger_name_and_before_the_message(self):
        token = begin_operation("0123456789abcdef")
        try:
            self.logger.info("local_api.status.start")
        finally:
            end_operation(token)

        self.assertRegex(
            self.line(),
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2} \[INFO\] wt_media_agent\.somewhere: "
            r"operation_id=0123456789abcdef local_api\.status\.start$",
        )

    def test_an_id_is_scoped_and_does_not_outlive_its_request(self):
        """`end_operation` restores what was there before, it does not clear it.

        A request nested inside another must give the inner lines the inner id and
        hand the outer id back afterwards -- "no id at all" would be a different
        claim, and one this assertion can tell apart from the right one.
        """
        outer = begin_operation("aaaaaaaaaaaaaaaa")
        self.logger.info("outer before")
        inner = begin_operation("bbbbbbbbbbbbbbbb")
        try:
            self.logger.info("inner")
        finally:
            end_operation(inner)
        self.logger.info("outer after")
        end_operation(outer)
        self.logger.info("no request")

        lines = (self.directory / AGENT_LOG_NAME).read_text().splitlines()
        self.assertEqual(
            [ID_PATTERN.search(line).group(1) if ID_PATTERN.search(line) else None for line in lines],
            ["aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb", "aaaaaaaaaaaaaaaa", None],
        )

    def test_error_log_keeps_its_fields_in_order_with_the_id_inserted(self):
        """D-03's five fields stay, and stay in order; the id is inserted, not appended.

        The id goes after the logger name and before `error_code` -- the same
        place it takes in the other two files -- and the message stays last.
        """
        token = begin_operation("fedcba9876543210")
        try:
            logging.getLogger("wt_media_agent.runner.task").error(
                "executor gave up",
                extra={
                    "error_code": "executor_error",
                    "task_id": "task-77",
                    "context": "attempt 2",
                },
            )
        finally:
            end_operation(token)

        line = (self.directory / ERROR_LOG_NAME).read_text().splitlines()[-1]
        self.assertRegex(
            line,
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2} \[ERROR\] "
            r"wt_media_agent\.runner\.task: operation_id=fedcba9876543210 "
            r"error_code=executor_error task_id=task-77 context=attempt 2 "
            r"executor gave up$",
        )

    def test_error_log_without_a_request_keeps_the_none_placeholder(self):
        """The regression guard for `error.log`: no id, no new characters."""
        logging.getLogger("wt_media_agent.runner.task").error(
            "no code on this one", extra={}
        )

        line = (self.directory / ERROR_LOG_NAME).read_text().splitlines()[-1]
        self.assertRegex(
            line,
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2} \[ERROR\] "
            r"wt_media_agent\.runner\.task: error_code=none no code on this one$",
        )


class RequestOperationIdTest(LoggingStateTestCase):
    """The same field, driven by two real requests over a real loopback socket."""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.directory = Path(self._tmp.name)
        configure_from(
            load_config(
                env={"WT_MEDIA_LOG_FILE": str(self.directory / AGENT_LOG_NAME)},
                frozen=False,
                repo_root=Path("/repo"),
                home=Path("/Users/dev"),
            )
        )
        api = LocalApiServer(LocalAgentState(), bitbrowser=UnusedBitBrowser())
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(api))
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.addCleanup(self._stop)

    def _stop(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def post(self, path: str) -> tuple[int, dict[str, str], str]:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        try:
            connection.request("POST", path, body=b"{}", headers={"content-type": "application/json"})
            response = connection.getresponse()
            return (
                response.status,
                {name.lower(): value for name, value in response.getheaders()},
                response.read().decode("utf-8"),
            )
        finally:
            connection.close()

    def records(self) -> list[str]:
        text = (self.directory / AGENT_LOG_NAME).read_text()
        return [line for line in text.splitlines() if REJECT_RECORD in line]

    def test_each_request_gets_its_own_id_and_the_line_says_so(self):
        first = self.post(REJECTING_ROUTE)
        second = self.post(REJECTING_ROUTE)

        self.assertEqual(first[0], 400, "positive control: the route answered")
        self.assertEqual(second[0], 400, "positive control: the route answered again")

        lines = self.records()
        self.assertEqual(len(lines), 2, f"one record per request, got {len(lines)}")
        ids = [ID_PATTERN.search(line) for line in lines]
        self.assertTrue(all(ids), f"every request's record carries an id: {lines}")
        self.assertNotEqual(
            ids[0].group(1),
            ids[1].group(1),
            "two requests must not share one id",
        )
        for line, match in zip(lines, ids):
            self.assertRegex(
                line,
                rf"^\d{{4}}-\d{{2}}-\d{{2}}T\d{{2}}:\d{{2}}:\d{{2}} \[WARNING\] "
                rf"wt_media_agent\.local_api\.server: {OPERATION_ID_FIELD}="
                rf"{match.group(1)} {REJECT_RECORD} reason=profile_id_required$",
            )

        # The id is a field on this process's records and nothing else: it is not
        # a header and not a body, on either response.
        for status, headers, body in (first, second):
            for match in ids:
                needle = match.group(1)
                self.assertNotIn(needle, body)
                self.assertNotIn(needle, json.dumps(headers), "the id must not leave in a header")

    def test_a_record_written_outside_a_request_has_no_id(self):
        """The control for the assertion above: this thread is not a request thread."""
        self.post(REJECTING_ROUTE)
        logging.getLogger("wt_media_agent.somewhere").info("main thread, no request")
        self.assertEqual(len(self.records()), 1, "positive control: the request did log")

        lines = (self.directory / AGENT_LOG_NAME).read_text().splitlines()
        outside = [line for line in lines if "main thread, no request" in line]
        self.assertEqual(len(outside), 1)
        self.assertNotIn(OPERATION_ID_FIELD, outside[0])

    def test_the_id_is_set_before_the_request_line_is_read(self):
        """The rejected-before-it-logs half, asserted structurally.

        A 401, a 404 and a malformed request line all stop inside
        `handle_one_request` before writing any record, so their ids are not
        observable in a file. What *is* observable is the shape of the wrapper:
        the id is set first, the whole request is handled second, and the reset
        cannot run before the handling does. Asserting this is weaker than
        asserting a behaviour, and it is labelled as what it is -- the behaviour
        it stands for has no file to show up in.
        """
        source = inspect.getsource(make_handler(LocalApiServer(LocalAgentState(), bitbrowser=UnusedBitBrowser())))

        self.assertIn("def handle_one_request", source, "the wrapper is where it must be")
        body = source.split("def handle_one_request", 1)[1]
        body = body.split("def ", 1)[0]

        set_at = body.find("begin_operation(")
        handle_at = body.find("super().handle_one_request()")
        finally_at = body.find("finally:")
        reset_at = body.find("end_operation(")
        self.assertGreaterEqual(set_at, 0, f"the id is set in the wrapper: {body!r}")
        self.assertGreaterEqual(handle_at, 0, f"the request is handled in the wrapper: {body!r}")
        self.assertGreaterEqual(finally_at, 0, f"the reset is in a finally: {body!r}")
        self.assertGreaterEqual(reset_at, 0, f"the id is reset in the wrapper: {body!r}")
        self.assertLess(set_at, handle_at, "the id exists before the request is served")
        self.assertLess(handle_at, finally_at, "the reset cannot run before the handling")
        self.assertLess(finally_at, reset_at, "the reset is the finally body")


if __name__ == "__main__":
    unittest.main()
