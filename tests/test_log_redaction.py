"""CHG-057 T-06: credentials must not reach a log file (the ruling 十, 十三·7).

Two layers are asserted, and they fail for different reasons:

1. the pure function, table-driven over the *shapes* a credential arrives in --
   `Name: value`, `Name=value`, `"Name": "value"`, a query string, a bare
   bearer/JWT blob, and the local runtime token's own verbatim value;
2. the wiring, through `configure_from`, so a real file on disk is read back.

The table is deliberately built out of inputs that are ordinary in this codebase
today (headers, form bare credentials, a URL) rather than exotic ones: the point
is what a log line already can contain, not what an attacker could construct.

Every case carries a "must still be readable" column. Masking by discarding the
line would satisfy "the secret is gone" while destroying the log's purpose, so
the surrounding text is asserted to survive in every row.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from support import LoggingStateTestCase

from wt_media_agent.runtime.config import SENSITIVE_KEY_NAMES, load_config
from wt_media_agent.runtime.logging import (
    AGENT_LOG_NAME,
    ERROR_LOG_NAME,
    REDACTED,
    RedactingFormatter,
    configure_from,
    redact,
)

#: The value injected as this process's runtime token in the tests below. Long
#: enough to be a real credential, and distinctive enough that a hit is not a
#: coincidence.
RUNTIME_TOKEN = "rt-9f8e7d6c5b4a32100fedcba987654321"

JWT = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVP"

#: (label, line, strings that must be gone, strings that must remain).
CASES: list[tuple[str, str, list[str], list[str]]] = [
    (
        "cookie header",
        "request Cookie: session=abc123def456",
        ["abc123def456"],
        ["request", "Cookie:"],
    ),
    (
        # A cookie header is masked pair by pair: every value goes, every name
        # stays. A cookie's name says nothing about whether its value is worth
        # keeping, so the value is not the place to make that judgement.
        "cookie with several pairs",
        "Cookie: a=1; sessionid=abc123def456; theme=dark",
        ["abc123def456", "theme=dark"],
        ["Cookie:", "sessionid=", "theme="],
    ),
    (
        "set-cookie header",
        "Set-Cookie: token=abc123def456; Path=/",
        ["abc123def456"],
        ["Set-Cookie:", "Path="],
    ),
    (
        "authorization bearer",
        f"Authorization: Bearer {JWT}",
        [JWT],
        ["Authorization:"],
    ),
    (
        "bare bearer with no key",
        f"calling the api with Bearer {JWT} now",
        [JWT],
        ["calling the api with", "now"],
    ),
    (
        "bare jwt with no key and no scheme",
        f"rejected credential {JWT} for the request",
        [JWT],
        ["rejected credential", "for the request"],
    ),
    (
        "password assignment",
        "password=hunter2 could not be used",
        ["hunter2"],
        ["could not be used"],
    ),
    (
        "password with a space separator",
        "password: hunter2 (rejected)",
        ["hunter2"],
        ["(rejected)"],
    ),
    (
        "json-shaped password",
        '{"password": "hunter2", "user": "alice"}',
        ["hunter2"],
        ["alice", "password"],
    ),
    (
        "uppercase key",
        "PASSWORD=TopSecret7",
        ["TopSecret7"],
        ["PASSWORD="],
    ),
    (
        "proxy password",
        "proxy_password=s3cret99 for http://proxy.example.test:8080",
        ["s3cret99"],
        ["http://proxy.example.test:8080"],
    ),
    (
        "refresh token",
        "refresh_token=rt-1234567890ab rejected",
        ["rt-1234567890ab"],
        ["rejected"],
    ),
    (
        "api key",
        "api_key=AKIAIOSFODNN7EXAMPLE sent",
        ["AKIAIOSFODNN7EXAMPLE"],
        ["sent"],
    ),
    (
        "credential in a url",
        "GET https://api.example.test/v1/x?token=abcd1234efgh&page=2 -> 401",
        ["abcd1234efgh"],
        ["page=2", "-> 401"],
    ),
    (
        "credential in a url userinfo",
        "connecting to https://alice:pw123456@cloud.example.test/api",
        ["pw123456"],
        ["alice", "cloud.example.test/api"],
    ),
]


class RedactTableTest(unittest.TestCase):
    """Every row is a shape; both columns of every row are asserted."""

    def test_no_credential_shape_survives_and_the_line_stays_readable(self):
        for label, line, gone, kept in CASES:
            with self.subTest(label=label):
                masked = redact(line)
                for needle in gone:
                    self.assertNotIn(needle, masked)
                for needle in kept:
                    self.assertIn(needle, masked, f"{label}: lost surrounding text")
                self.assertIn(REDACTED, masked, f"{label}: nothing was masked at all")

    def test_ordinary_text_passes_through_unchanged(self):
        """The positive control for the table: not everything is a credential.

        Without this, a `redact` that returned `***` for every input would pass
        every row above.
        """
        ordinary = [
            "recovering 3 incomplete task(s)",
            "task t-1 succeeded",
            "the token could not be refreshed",  # names a credential, holds none
            "no executor for task type cookie_read_task",
            "GET /api/v1/health -> 200",
        ]
        for line in ordinary:
            with self.subTest(line=line):
                self.assertEqual(redact(line), line)

    def test_masking_is_idempotent(self):
        """A record is formatted by every handler; the second pass must be a no-op."""
        for label, line, _gone, _kept in CASES:
            with self.subTest(label=label):
                once = redact(line)
                self.assertEqual(redact(once), once)

    def test_every_configured_sensitive_key_name_is_covered(self):
        """The vocabulary is shared with the config loader, not re-typed.

        `SENSITIVE_KEY_NAMES` is what refuses a credential in a shipped TOML
        file. A name added there must also be masked here, so this walks the
        real set instead of a copy of it -- and reports the denominator.
        """
        names = sorted(SENSITIVE_KEY_NAMES)
        self.assertGreaterEqual(len(names), 10, "positive control: the set is populated")
        for name in names:
            for line in (f"{name}=abcd1234efgh", f"{name}: abcd1234efgh"):
                with self.subTest(name=name, line=line):
                    self.assertNotIn("abcd1234efgh", redact(line))

    def test_the_known_secret_wins_even_when_no_key_names_it(self):
        masked = redact(f"handing over {RUNTIME_TOKEN} to the sidecar", secrets=[RUNTIME_TOKEN])
        self.assertNotIn(RUNTIME_TOKEN, masked)
        self.assertIn("handing over", masked)
        self.assertIn("to the sidecar", masked)

    def test_a_lookalike_value_is_not_masked_by_the_known_secret(self):
        """The needle is the value, not its shape. Positive control for the row above."""
        other = "rt-0000000000000000000000000000000"
        masked = redact(f"handing over {other} to the sidecar", secrets=[RUNTIME_TOKEN])
        self.assertIn(other, masked)

    def test_a_tiny_configured_secret_is_not_used_as_a_needle(self):
        """A short value would redact ordinary prose wherever its letters appear."""
        masked = redact("the abc report for abc", secrets=["abc"])
        self.assertEqual(masked, "the abc report for abc")


class RedactingFormatterTest(unittest.TestCase):
    """The mask has to cover the traceback too, which a message filter cannot."""

    def record(self, message: str, exc_info=None) -> logging.LogRecord:
        return logging.LogRecord(
            "wt_media_agent.somewhere", logging.ERROR, __file__, 1, message, (), exc_info
        )

    def test_a_secret_in_the_message_is_masked(self):
        formatter = RedactingFormatter("%(message)s")
        self.assertNotIn("hunter2", formatter.format(self.record("password=hunter2 failed")))

    def test_a_secret_inside_a_traceback_is_masked(self):
        """`logger.exception` output is formatted by the formatter, not the record.

        A `logging.Filter` that rewrote `record.msg` would leave this leaking:
        the traceback is rendered by `Formatter.formatException` from
        `exc_info`. Masking the rendered line is why the mask lives here.
        """
        try:
            raise ValueError("cloud rejected the request with password=hunter2")
        except ValueError:
            record = self.record("cloud call failed", sys.exc_info())

        text = RedactingFormatter("%(message)s").format(record)
        self.assertIn("ValueError", text, "positive control: the traceback was rendered")
        self.assertNotIn("hunter2", text)


class ConfiguredRedactionTest(LoggingStateTestCase):
    """End to end: the token this process holds must not reach a log file.

    `configure_from` installs the formatters, so this reads a real file back
    rather than asserting on the function that is also under test.
    """

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.directory = Path(self._tmp.name)

    def configure(self, **env: str):
        configure_from(
            load_config(
                env={"WT_MEDIA_LOG_FILE": str(self.directory / AGENT_LOG_NAME), **env},
                frozen=False,
                repo_root=Path("/repo"),
                home=Path("/Users/dev"),
            )
        )

    def test_the_runtime_token_never_reaches_a_file(self):
        self.configure(WT_MEDIA_AGENT_RUNTIME_TOKEN=RUNTIME_TOKEN)
        logging.getLogger("wt_media_agent.somewhere").error(
            "local api rejected the sidecar token %s", RUNTIME_TOKEN
        )
        logging.getLogger("wt_media_agent.somewhere").info("token was presented")

        agent = (self.directory / AGENT_LOG_NAME).read_text()
        error = (self.directory / ERROR_LOG_NAME).read_text()
        self.assertIn("token was presented", agent, "positive control: the file was written")
        for written in (agent, error):
            self.assertNotIn(RUNTIME_TOKEN, written)
        self.assertIn("local api rejected the sidecar token", agent)

    def test_headers_in_a_logged_message_are_masked_on_disk(self):
        self.configure()
        logging.getLogger("wt_media_agent.somewhere").error(
            f"upstream said: Authorization: Bearer {JWT}"
        )
        written = (self.directory / ERROR_LOG_NAME).read_text()
        self.assertIn("upstream said", written)
        self.assertNotIn(JWT, written)


class ConfigObjectReprTest(unittest.TestCase):
    """The ruling 十: a sensitive object must not print itself (Python side)."""

    def config(self, **env: str):
        return load_config(
            env=env,
            frozen=False,
            repo_root=Path("/repo"),
            home=Path("/Users/dev"),
        )

    def test_the_resolved_config_does_not_print_its_token(self):
        cfg = self.config(WT_MEDIA_AGENT_RUNTIME_TOKEN=RUNTIME_TOKEN)
        self.assertEqual(cfg.runtime_token, RUNTIME_TOKEN, "positive control: it holds it")
        for text in (repr(cfg), str(cfg), f"{cfg}"):
            self.assertNotIn(RUNTIME_TOKEN, text)

    def test_the_other_fields_are_still_visible(self):
        """Masking one field must not turn the whole repr into a stub."""
        cfg = self.config(WT_MEDIA_LOG_LEVEL="WARNING")
        self.assertIn("log_level='WARNING'", repr(cfg))


if __name__ == "__main__":
    unittest.main()
