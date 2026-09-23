"""UTC timestamp formatting.

Timestamps are written into SQLite as `YYYY-MM-DDTHH:MM:SSZ` and compared there
as strings, so the format is a contract rather than a formatting preference.
It is defined once, here, so that changing it is a one-line change instead of a
search for copies that have drifted.

`storage/migration.py` is deliberately not a caller. Its `applied_at` column
default is SQLite's own `strftime('%Y-%m-%dT%H:%M:%fZ', 'now')` -- a different
function, evaluated by the database, with a different format (it carries
fractional seconds). That column is schema, not application code, and the
migration module's symbols are frozen by CHG-056.
"""

from __future__ import annotations

import time

#: The wire/storage timestamp format. Zero-padded and fixed-width on purpose:
#: SQLite stores these as TEXT and orders them with `ORDER BY`, so text order
#: has to match time order.
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def utc_now_iso() -> str:
    """Return the current UTC time formatted as `TIMESTAMP_FORMAT`."""
    return time.strftime(TIMESTAMP_FORMAT, time.gmtime())
