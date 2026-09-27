"""`clients/transfer/source.py` and the three facts it reports.

The tests are mostly about the *reported* facts rather than about bytes, because
each of them changes what the caller does next and each of them is a decision
that a plausible implementation gets wrong in a way no byte-level assertion
notices: a `200` to a ranged request silently means "start over", a
`Content-Length` on a partial body is not the file's length, and an expired
signature is an ordinary answer rather than a bug.
"""

from __future__ import annotations

import unittest
from urllib import error as urlerror


from wt_media_agent.clients.transfer import (
    SourceStalledError,
    SourceUnavailableError,
    extension_from_url,
    open_source,
)
from wt_media_agent.clients.transfer.source import parse_content_range


class FakeResponse:
    """The `http.client.HTTPResponse` surface `source.py` uses."""

    def __init__(self, body: bytes = b"", status: int = 200, headers=None, fail_after=None):
        self.status = status
        self.headers = dict(headers or {})
        self._body = body
        self._offset = 0
        self._fail_after = fail_after
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        if self._fail_after is not None and self._offset >= self._fail_after:
            raise OSError("connection reset")
        chunk = self._body[self._offset:] if size < 0 else self._body[self._offset:self._offset + size]
        self._offset += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True


class RecordingOpener:
    """Records the headers it was asked with, and answers with one response."""

    def __init__(self, response=None, raises=None):
        self.calls: list[tuple[str, dict, float]] = []
        self._response = response
        self._raises = raises

    def __call__(self, url, headers, timeout):
        self.calls.append((url, dict(headers), timeout))
        if self._raises is not None:
            raise self._raises
        return self._response


class ParseContentRangeTest(unittest.TestCase):
    def test_reads_the_start_and_the_total(self):
        self.assertEqual(parse_content_range("bytes 100-199/1000"), (100, 1000))

    def test_an_unknown_total_is_none_and_not_zero(self):
        """`/*` says the server did not answer how long the whole thing is.

        Zero would claim an empty resource, and a caller that believed it would
        refuse a perfectly good download.
        """
        self.assertEqual(parse_content_range("bytes 100-199/*"), (100, None))

    def test_absent_or_malformed_is_not_a_guess(self):
        for value in (None, "", "items 0-9/10", "bytes abc-def/10", "bytes"):
            self.assertEqual(parse_content_range(value), (None, None), value)


class OpenSourceTest(unittest.TestCase):
    def test_a_fresh_download_sends_no_range_header(self):
        opener = RecordingOpener(FakeResponse(b"abc", headers={"Content-Length": "3"}))
        stream = open_source("http://cdn.test/f", opener=opener)

        self.assertEqual(opener.calls[0][1], {})
        self.assertTrue(stream.range_honoured)

    def test_a_resume_asks_from_its_offset(self):
        opener = RecordingOpener(
            FakeResponse(b"def", status=206, headers={"Content-Range": "bytes 3-5/6"})
        )
        stream = open_source("http://cdn.test/f", offset=3, opener=opener)

        self.assertEqual(opener.calls[0][1], {"Range": "bytes=3-"})
        self.assertTrue(stream.range_honoured)
        self.assertEqual(stream.total_bytes, 6)
        self.assertEqual(stream.start_offset, 3)

    def test_a_server_that_ignores_the_range_says_so(self):
        """The one fact that keeps a resumed download from splicing two bodies.

        A `200` with the whole file, appended to a part, gives a file of the
        right length and the wrong bytes. The flag is the only warning, so it
        has to be false here even though the response itself is a success.
        """
        opener = RecordingOpener(FakeResponse(b"abcdef", headers={"Content-Length": "6"}))
        stream = open_source("http://cdn.test/f", offset=3, opener=opener)

        self.assertFalse(stream.range_honoured)

    def test_a_whole_body_sent_to_a_ranged_request_reports_the_whole_length(self):
        """`Content-Length` on a `200` is the file's length, offset or not.

        This response is the "server ignored Range" case: the body is everything
        from byte zero, so its length is the total. Adding the offset to it -- the
        arithmetically tempting thing, since the caller asked to start at 3 --
        would overstate the file by three bytes and make the executor's
        size check fail on a file that is exactly right.
        """
        opener = RecordingOpener(FakeResponse(b"abcdef", headers={"Content-Length": "6"}))
        stream = open_source("http://cdn.test/f", offset=3, opener=opener)

        self.assertEqual(stream.total_bytes, 6)

    def test_a_partial_body_does_not_claim_a_total_it_cannot_know(self):
        """An honoured range with no `Content-Range` leaves the total unknown.

        `Content-Length` is the part's length here; a caller that treated it as
        the file's would compare a finished download against the length of one
        of its chunks.
        """
        opener = RecordingOpener(FakeResponse(b"def", status=206, headers={"Content-Length": "3"}))
        stream = open_source("http://cdn.test/f", offset=3, opener=opener)

        self.assertEqual(stream.total_bytes, -1)

    def test_a_resume_landing_somewhere_else_is_refused(self):
        """Splicing a different region onto the part would corrupt it silently."""
        opener = RecordingOpener(
            FakeResponse(b"xxx", status=206, headers={"Content-Range": "bytes 900-902/1000"})
        )
        with self.assertRaises(SourceUnavailableError):
            open_source("http://cdn.test/f", offset=3, opener=opener)

    def test_an_expired_signature_is_an_ordinary_answer(self):
        opener = RecordingOpener(raises=urlerror.HTTPError("http://cdn.test/f", 403, "Forbidden", {}, None))
        with self.assertRaises(SourceUnavailableError):
            open_source("http://cdn.test/f", opener=opener)

    def test_a_timeout_is_told_apart_from_a_refusal(self):
        """They name different operator actions, so they are different types."""
        opener = RecordingOpener(raises=TimeoutError("timed out"))
        with self.assertRaises(SourceStalledError):
            open_source("http://cdn.test/f", opener=opener)

    def test_a_url_error_whose_reason_is_a_timeout_is_a_stall(self):
        opener = RecordingOpener(raises=urlerror.URLError(TimeoutError("timed out")))
        with self.assertRaises(SourceStalledError):
            open_source("http://cdn.test/f", opener=opener)

    def test_no_error_message_carries_the_address(self):
        """The address is a short-lived credential.

        `secret_policy.forbidden_fields` names `source_address`, so this is a
        contract assertion and not a style preference -- and the URL is exactly
        what a naive `raise ... from exc` or an f-string would leak.
        """
        address = "http://cdn.test/f?signature=SUPERSECRET"
        for raises in (
            urlerror.HTTPError(address, 403, "Forbidden", {}, None),
            urlerror.URLError(OSError("connection refused")),
            OSError("connection refused"),
        ):
            with self.assertRaises(SourceUnavailableError) as caught:
                open_source(address, opener=RecordingOpener(raises=raises))
            message = str(caught.exception)
            self.assertNotIn("SUPERSECRET", message)
            self.assertNotIn("cdn.test", message)
            self.assertNotIn(address, message)

    def test_no_error_message_carries_the_upstreams_own_words(self):
        """The other half of the same policy.

        `raw_upstream_messages_exposed: false` is separate from the field list,
        and it is the half a message like `f"...: {exc}"` breaks: `HTTPError`'s
        own `str()` is `"HTTP Error 403: Forbidden"`, so quoting the exception
        forwards the upstream's reason phrase while leaking no address at all.
        Checking only for the address passes that.
        """
        address = "http://cdn.test/f?signature=SUPERSECRET"
        raises = urlerror.HTTPError(address, 403, "Forbidden by the edge node", {}, None)
        with self.assertRaises(SourceUnavailableError) as caught:
            open_source(address, opener=RecordingOpener(raises=raises))

        self.assertNotIn("Forbidden by the edge node", str(caught.exception))
        self.assertNotIn("Forbidden", str(caught.exception))

    def test_a_malformed_address_does_not_come_back_quoted(self):
        """`urlopen` refuses a bad address itself, and says which one.

        That `ValueError` is raised from inside the call this module wraps, so
        without a handler of its own it goes straight out of `open_source`
        carrying the address -- which is what makes it worth a test rather than
        a note.
        """
        address = "not a url at all: SUPERSECRET"
        with self.assertRaises(SourceUnavailableError) as caught:
            open_source(address, opener=RecordingOpener(raises=ValueError(f"unknown url type: {address}")))
        message = str(caught.exception)
        self.assertNotIn("SUPERSECRET", message)
        self.assertNotIn(address, message)


class SourceStreamTest(unittest.TestCase):
    def _stream(self, **kwargs):
        opener = RecordingOpener(FakeResponse(headers={"Content-Length": "0"}, **kwargs))
        return open_source("http://cdn.test/f", opener=opener)

    def test_reads_reach_the_end_and_then_report_it(self):
        stream = self._stream(body=b"abcdef")
        self.assertEqual(stream.read(4), b"abcd")
        self.assertEqual(stream.read(4), b"ef")
        self.assertEqual(stream.read(4), b"")

    def test_a_reset_part_way_through_is_a_stall_not_a_silent_short_read(self):
        """A truncated body must not look like a finished one.

        This is the fault the whole module is arranged around: a caller that
        could not tell the difference would hash a short file, find the digest
        wrong, and blame the source's contents rather than the connection.
        """
        stream = self._stream(body=b"abc", fail_after=1)
        self.assertEqual(stream.read(1), b"a")
        with self.assertRaises(SourceStalledError):
            stream.read(10)

    def test_it_closes_with_the_context_manager(self):
        response = FakeResponse(headers={"Content-Length": "0"})
        with open_source("http://cdn.test/f", opener=RecordingOpener(response)):
            pass
        self.assertTrue(response.closed)

    def test_a_header_lookup_does_not_depend_on_the_spelling(self):
        """`http.client` is case-insensitive; a test double's dict is not."""
        opener = RecordingOpener(FakeResponse(headers={"content-range": "bytes 0-2/3"}))
        stream = open_source("http://cdn.test/f", opener=opener)
        self.assertEqual(stream.total_bytes, 3)


class ExtensionFromUrlTest(unittest.TestCase):
    """The one part of a file name the agent cannot choose for itself.

    `LocalLease` carries a title, a size and a digest, and no extension -- by
    design, since the name is this side's to pick and `Completion.file_name` is
    where the choice goes back. The address is asked because a presigned `GET`
    *is* the object key: the suffix in it is the one the uploader chose.
    """

    def test_the_suffix_of_the_signed_key_is_the_extension(self):
        self.assertEqual(
            extension_from_url("https://cdn.test/bucket/materials/42/abc.mp4?X-Amz-Signature=x"),
            "mp4",
        )

    def test_the_query_string_is_not_part_of_the_name(self):
        """A signature can contain dots; a key's suffix lives in the path."""
        self.assertEqual(extension_from_url("https://cdn.test/f?a=b.mp4"), "bin")

    def test_an_address_with_no_key_suffix_says_bin_rather_than_guessing(self):
        """`bin` is a fact -- "this side cannot tell" -- and `mp4` would not be.

        The material is known to be a video, so an implementation could assume a
        container and be right most of the time. Most of the time is what puts a
        file in a player that refuses it, with a name that said it would play.
        """
        for url in (
            "https://cdn.test/download?sig=x",
            "https://cdn.test/materials/42/abc",
            "https://cdn.test/a/.hidden",
            "https://cdn.test/a/abc.",
            "https://cdn.test/a/trailing/",
            "",
            "not a url at all",
        ):
            with self.subTest(url=url):
                self.assertEqual(extension_from_url(url), "bin")

    def test_a_dot_in_the_key_that_is_not_an_extension_is_not_taken_as_one(self):
        for url in ("https://cdn.test/a/clip.mp4-backup", "https://cdn.test/a/x.verylongsuffix"):
            with self.subTest(url=url):
                self.assertEqual(extension_from_url(url), "bin")

    def test_the_reader_can_see_an_extension_that_is_there(self):
        """The positive control for the arms above."""
        self.assertEqual(extension_from_url("https://cdn.test/a/b.WEBM"), "webm")


if __name__ == "__main__":
    unittest.main()
