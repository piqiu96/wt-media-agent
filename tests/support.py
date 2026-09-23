"""Shared test doubles. Not a test module -- `test*.py` is the discover pattern.

Kept to things that are only useful *because* they are shared: a double defined
next to a single test tends to inherit that test's assumptions.
"""

from __future__ import annotations


class UnusedBitBrowser:
    """A BitBrowser stand-in that fails if anything reaches for it.

    `LocalApiServer` needs a client to be constructed (CHG-056 T-04), but most
    routes never touch one. Passing this makes "this route did not consult
    BitBrowser" an assertion rather than an assumption -- a real client would
    have answered, and a `MagicMock` would have swallowed the question.
    """

    def __getattr__(self, name: str):
        raise AssertionError(f"this test must not touch BitBrowser.{name}")
