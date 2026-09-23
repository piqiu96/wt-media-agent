import calendar
import re
import time
import unittest

from wt_media_agent.utils.time import TIMESTAMP_FORMAT, utc_now_iso

ISO_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class UtcNowIsoTest(unittest.TestCase):
    def test_shape_is_seconds_precision_zulu(self):
        self.assertRegex(utc_now_iso(), ISO_UTC_RE)

    def test_value_parses_back_and_is_within_a_minute_of_now(self):
        parsed = time.strptime(utc_now_iso(), "%Y-%m-%dT%H:%M:%SZ")
        delta = abs(calendar.timegm(parsed) - time.time())
        self.assertLess(delta, 60)

    def test_value_is_utc_not_local(self):
        """Catches a switch to `time.localtime`, which would keep the shape.

        Reading the same fields two ways separates the two: `calendar.timegm`
        treats them as UTC, `time.mktime` as local. Exactly one of the two can
        land on the current instant.

        Note the limit of this check: it only discriminates when the machine's
        local timezone is not UTC. On a UTC host it is vacuous, so it skips.
        """
        if time.timezone == 0 and time.altzone == 0:
            self.skipTest("local timezone is UTC; local-vs-UTC is indistinguishable")
        fields = time.strptime(utc_now_iso(), "%Y-%m-%dT%H:%M:%SZ")
        now = time.time()
        self.assertLess(abs(calendar.timegm(fields) - now), 60, "string does not track UTC")
        self.assertGreater(abs(time.mktime(fields) - now), 60, "string tracks local time, not UTC")

    def test_format_is_fixed_width_so_text_order_matches_time_order(self):
        """The reason the format is frozen: SQLite orders these as TEXT.

        The 09 -> 10 pair is the case zero-padding exists for; a non-padded
        format would sort "…:9Z" after "…:10Z".
        """
        cases = [
            (0, "1970-01-01T00:00:00Z"),
            (1, "1970-01-01T00:00:01Z"),
            (9, "1970-01-01T00:00:09Z"),
            (10, "1970-01-01T00:00:10Z"),
            (86_400, "1970-01-02T00:00:00Z"),
            (1_767_225_600, "2026-01-01T00:00:00Z"),
        ]
        rendered = [time.strftime(TIMESTAMP_FORMAT, time.gmtime(e)) for e, _ in cases]
        self.assertEqual(rendered, [s for _, s in cases])
        self.assertEqual(sorted(rendered), rendered)


if __name__ == "__main__":
    unittest.main()
