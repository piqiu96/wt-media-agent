"""Retention and rotation of the three Agent log files (CHG-057 T-07, T-02).

The ruling (六, narrowed by 三) leaves two bounds, and each gets its own mutation
here:

* a file rolls when the hour turns over;
* one oversized record is *truncated* -- marked `truncate=true original_size=<n>`
  -- and neither starts a new file nor is allowed to exceed the cap;
* rolled files outside the retention window are deleted, and the file currently
  being written never is.

Volume is deliberately untested, because it is deliberately unbounded: the
single-file cap and the three files' shared total are gone (the ruling 三), and
what remains is `retention_days`. A test asserting a size bound here would be
asserting something the ruling removed.

**Two clocks, and only one of them is injectable.** The retention window takes
its clock as an argument (`LogRetention(clock=...)`), so "the window closed" is a
statement this test makes. The roll *trigger* is the stdlib's and reads
`time.time()` inside `shouldRollover`, with no seam to inject -- so the trigger
is driven through `rolloverAt`, the field the stdlib itself decides with, and the
rename is driven through `doRollover`, which is public. Handing the handler a
stale `rolloverAt` and emitting is then exactly what the first write after an hour
boundary does in a real run, which is the property being pinned.
"""

from __future__ import annotations

import contextlib
import io
import logging
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from support import LoggingStateTestCase

from wt_media_agent.runtime.constants import (
    DEFAULT_LOG_MAX_RECORD_BYTES,
    DEFAULT_LOG_RETENTION_DAYS,
)
from wt_media_agent.runtime.logging import (
    AGENT_LOG_NAME,
    ARCHIVE_FORMAT,
    ERROR_LOG_NAME,
    TASK_LOG_NAME,
    TRUNCATION_MARKER,
    HourlyFileHandler,
    LogRetention,
    configure_logging,
)

DAY = 24 * 60 * 60


def stamp(offset_days: int) -> str:
    """The archive stamp `offset_days` from now, in the shape names carry."""
    return (datetime.now() - timedelta(days=offset_days)).strftime(ARCHIVE_FORMAT)


class Clock:
    """A clock the test moves. Only `LogRetention` takes one."""

    def __init__(self, now: float | None = None) -> None:
        self.now = time.time() if now is None else now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RolloverTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)
        self.clock = Clock()

    def retention(self, *, retention_days: int = 14) -> LogRetention:
        return LogRetention(
            self.directory, retention_days=retention_days, clock=self.clock
        )

    def handler(
        self,
        retention: LogRetention,
        *,
        max_record_bytes: int = 10_000,
        name: str = AGENT_LOG_NAME,
    ) -> HourlyFileHandler:
        handler = HourlyFileHandler(
            self.directory / name,
            max_record_bytes=max_record_bytes,
            retention=retention,
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        self.addCleanup(handler.close)
        return handler

    def emit(self, handler: HourlyFileHandler, message: str) -> None:
        handler.emit(
            logging.LogRecord(
                name="t",
                level=logging.INFO,
                pathname=__file__,
                lineno=1,
                msg=message,
                args=(),
                exc_info=None,
            )
        )

    def hour_has_turned(self, handler: HourlyFileHandler) -> None:
        """Put the rollover a minute behind us, so the next record rolls.

        This is the state a handler is in on the first write after an hour
        boundary; the value is the stdlib's own field, and `shouldRollover`
        compares `time.time()` against it.
        """
        handler.rolloverAt = int(time.time()) - 60

    def rolled(self, name: str, body: str) -> Path:
        """A rolled file placed by name, for the rules that read the directory."""
        path = self.directory / name
        path.write_text(body)
        return path

    def archive(self, *, days_ago: int = 0) -> str:
        """An archive name for the hour `days_ago` days back, as the handler writes it."""
        return f"{AGENT_LOG_NAME}.{stamp(days_ago)}"

    @staticmethod
    def _shift(stamp_text: str, hours: int) -> str:
        """The archive stamp `hours` away from `stamp_text`."""
        moment = datetime.strptime(stamp_text, ARCHIVE_FORMAT) + timedelta(hours=hours)
        return moment.strftime(ARCHIVE_FORMAT)

    def names(self) -> list[str]:
        return sorted(path.name for path in self.directory.iterdir())

    def read(self, name: str) -> str:
        return (self.directory / name).read_text()

    def total(self) -> int:
        return sum(path.stat().st_size for path in self.directory.iterdir())


class HourRollTest(RolloverTestCase):
    def test_an_hour_boundary_rolls_the_file(self):
        retention = self.retention()
        handler = self.handler(retention)

        self.emit(handler, "first")
        self.hour_has_turned(handler)
        self.emit(handler, "second")

        # One archive, named for the hour that ended, and a live file holding the
        # record that arrived after it. The name is asserted as a shape and not
        # against a literal hour: the suffix is rendered from `rolloverAt`, and a
        # literal could only be right for one minute of the day.
        archives = [name for name in self.names() if name != AGENT_LOG_NAME]
        self.assertEqual(len(archives), 1, self.names())
        self.assertTrue(archives[0].startswith(f"{AGENT_LOG_NAME}."))
        self.assertEqual(len(archives[0]), len(AGENT_LOG_NAME) + 1 + len(stamp(0)))
        self.assertEqual(self.read(AGENT_LOG_NAME).strip(), "second")
        self.assertEqual(self.read(archives[0]).strip(), "first")

    def test_records_within_one_hour_stay_in_one_file(self):
        """Paired control: without it, a handler that always rolls passes."""
        retention = self.retention()
        handler = self.handler(retention)

        self.emit(handler, "one")
        self.emit(handler, "two")

        self.assertEqual(self.names(), [AGENT_LOG_NAME])
        self.assertEqual(self.read(AGENT_LOG_NAME).split(), ["one", "two"])

    def test_the_roll_lands_on_the_hour_boundary_not_an_hour_after_startup(self):
        """Aligned on purpose, because the archive's *name* comes from the trigger.

        `computeRollover` has an alignment branch only for `MIDNIGHT` and the
        weekly forms; for `when="H"` the stdlib returns `currentTime + interval`,
        so a handler built at 20:58 rolls at 21:58 -- and `doRollover` names the
        archive from `rolloverAt - interval`, i.e. 20:58, giving `...-20` for a
        file whose window runs to 21:58. The name and the contents then disagree by
        up to an hour, and the Desktop, which rolls on the clock hour, would be
        naming a different window with the same name. Found by running a real
        process and reading its `rolloverAt`; the stdlib docs do not mention it.
        """
        handler = self.handler(self.retention())

        moment = time.localtime(handler.rolloverAt)
        self.assertEqual((moment.tm_min, moment.tm_sec), (0, 0), "the trigger is on the hour")
        self.assertGreater(handler.rolloverAt, time.time())

        handler.doRollover()
        moment = time.localtime(handler.rolloverAt)
        self.assertEqual((moment.tm_min, moment.tm_sec), (0, 0), "and stays there")


class OversizedRecordTest(RolloverTestCase):
    def test_one_oversized_record_is_truncated_and_marked(self):
        retention = self.retention()
        handler = self.handler(retention, max_record_bytes=200)

        self.emit(handler, "x" * 500)

        self.assertEqual(
            self.names(),
            [AGENT_LOG_NAME],
            "an oversized record must be truncated in place, not rolled",
        )
        text = self.read(AGENT_LOG_NAME)
        self.assertLessEqual(len(text.encode()), 200, "the cap still holds")
        self.assertTrue(
            text.endswith(" truncate=true original_size=500\n"),
            f"the marker must carry the *original* byte count, got {text[-40:]!r}",
        )
        self.assertTrue(text.startswith("x" * 100), "the head of the record survives")
        self.assertNotIn("x" * 300, text)

    def test_the_marker_is_the_one_the_desktop_writes(self):
        """One reader learns one spelling: the marker is byte-for-byte the same.

        Asserted against the literal rather than against `TRUNCATION_MARKER`, so
        changing the constant has to be a deliberate change to this line too.
        """
        self.assertEqual(TRUNCATION_MARKER, " truncate=true original_size=")

    def test_the_marker_records_bytes_not_characters(self):
        """`original_size` is a byte count, which is what the cap is about."""
        retention = self.retention()
        handler = self.handler(retention, max_record_bytes=60)

        self.emit(handler, "é" * 100)  # two bytes each

        text = self.read(AGENT_LOG_NAME)
        self.assertLessEqual(len(text.encode()), 60)
        self.assertIn(" truncate=true original_size=200", text)
        self.assertNotIn("\ufffd", text, "a half-written character is dropped, not mangled")

    def test_a_record_that_fits_is_not_marked(self):
        """Paired control, and the reason the marker is not a fixed suffix."""
        retention = self.retention()
        handler = self.handler(retention, max_record_bytes=200)

        self.emit(handler, "y" * 100)

        self.assertNotIn("truncate=true", self.read(AGENT_LOG_NAME))

    def test_a_record_is_bounded_by_its_own_cap_not_by_the_files_size(self):
        """The cap is about one line, which is all that is left of the bounds.

        Two records that are each inside the cap are both written whole, however
        large the file grows: the file has no cap any more (the ruling 三).
        """
        retention = self.retention()
        handler = self.handler(retention, max_record_bytes=200)

        for index in range(50):
            self.emit(handler, f"{index:04d}" + "x" * 150)

        text = self.read(AGENT_LOG_NAME)
        self.assertEqual(len(text.splitlines()), 50)
        self.assertNotIn("truncate=true", text)
        self.assertGreater(len(text.encode()), 200 * 25, "the file is allowed to grow")


class AgePruningTest(RolloverTestCase):
    def test_rolled_files_outside_the_retention_window_are_deleted(self):
        retention = self.retention(retention_days=14)
        live = self.handler(retention)
        self.emit(live, "live")
        # Both sides of the boundary, so "delete everything" cannot pass. Both
        # names are measured from the window's own edge rather than from the wall
        # clock: the window keeps a stamp from the edge onwards, so "inside" is a
        # day *after* the edge. And "outside" has to be a whole hour past it,
        # because a name N days back lands on the same *hour* as the edge --
        # `stamp(14)` under a 14-day window is the cutoff hour itself, which the
        # strict comparison keeps. That case is the test below; this one is about
        # a clear gap each way.
        edge = retention.cutoff()
        inside = self.rolled(f"{AGENT_LOG_NAME}.{self._shift(edge, +24)}", "kept\n")
        outside = self.rolled(f"{AGENT_LOG_NAME}.{self._shift(edge, -1)}", "gone\n")

        retention.prune()

        self.assertEqual(self.names(), sorted([AGENT_LOG_NAME, inside.name]))

    def test_an_archive_in_the_cutoff_hour_itself_is_kept(self):
        """The boundary is strict, and it is the same boundary the Desktop draws.

        `stamp < cutoff`, not `<=`: `AppendTimestamp::too_old` in file-rotate
        0.8.0 (`suffix.rs`) builds `(Local::now() - age).format(self.format)` and
        compares the *suffix string* against it with `<` -- not the file's mtime
        -- so one hour at the edge is not deleted by one side and kept by the
        other. Asserted through `cutoff()` so the assertion is about the
        comparison rather than about this machine's clock.
        """
        retention = self.retention(retention_days=14)
        live = self.handler(retention)  # registering is what makes pruning match
        self.emit(live, "live")
        edge = retention.cutoff()
        at_the_edge = self.rolled(f"{AGENT_LOG_NAME}.{edge}", "edge\n")
        just_past = self.rolled(f"{AGENT_LOG_NAME}.{self._shift(edge, -1)}", "past\n")

        retention.prune()

        self.assertIn(at_the_edge.name, self.names())
        self.assertNotIn(just_past.name, self.names())

    def test_a_file_that_cannot_be_deleted_does_not_stop_the_others(self):
        """One stubborn file must not take the whole window down with it.

        Measured on the path that really fails: `prune` lists the directory and
        then unlinks, and another writer can remove a file in between -- the same
        list-then-act race the size-capped handler this replaced had to survive.
        The failure is reported to stderr and not raised, because `prune` runs on
        the emit path: letting it propagate would turn one undeletable file into a
        logging handler that raises on every record.
        """
        retention = self.retention(retention_days=14)
        live = self.handler(retention)  # registering is what makes pruning match
        self.emit(live, "live")
        edge = retention.cutoff()
        stubborn = self.rolled(f"{AGENT_LOG_NAME}.{self._shift(edge, -3)}", "stuck\n")
        doomed = self.rolled(f"{AGENT_LOG_NAME}.{self._shift(edge, -2)}", "goes\n")
        real_unlink = Path.unlink

        def fail_on_stubborn(path: Path, *args: object, **kwargs: object) -> None:
            if path.name == stubborn.name:
                raise PermissionError(1, "Operation not permitted")
            real_unlink(path, *args, **kwargs)  # type: ignore[arg-type]

        with mock.patch.object(Path, "unlink", fail_on_stubborn):
            with mock.patch.object(logging, "raiseExceptions", True):
                with contextlib.redirect_stderr(io.StringIO()) as reported:
                    deleted = retention.prune()

        self.assertEqual([path.name for path in deleted], [doomed.name])
        self.assertEqual(self.names(), sorted([AGENT_LOG_NAME, stubborn.name]))
        self.assertIn("cannot delete", reported.getvalue())
        self.assertIn(stubborn.name, reported.getvalue())

    def test_a_window_never_told_its_names_deletes_nothing(self):
        """Deleting is opt-in per family: an unregistered window matches no names.

        Worth pinning because the failure is silent -- `prune` on an object nobody
        registered leaves the directory exactly as `prune` with nothing old to
        delete does, so a lost `register` call and a healthy window look alike.
        That is the shape of the CHG-057 T-07 defect this class replaced, where a
        real run left its stale files in place for its whole lifetime. The control
        registers the one name and the same file does go, so this cannot pass by
        the fixture simply not being old.
        """
        retention = self.retention(retention_days=14)
        stale = self.rolled(
            f"{AGENT_LOG_NAME}.{self._shift(retention.cutoff(), -24)}", "old\n"
        )

        retention.prune()
        self.assertIn(stale.name, self.names(), "no family is registered yet")

        retention.register(self.directory / AGENT_LOG_NAME)
        retention.prune()
        self.assertNotIn(stale.name, self.names())

    def test_a_roll_ages_out_the_history_without_being_asked(self):
        """Pruning has to happen on the write path too, not only on demand."""
        retention = self.retention(retention_days=2)
        handler = self.handler(retention)
        stale = self.rolled(self.archive(days_ago=9), "old\n")

        self.emit(handler, "first")
        self.hour_has_turned(handler)
        self.emit(handler, "second")  # rolls, and the roll is what prunes

        self.assertNotIn(stale.name, self.names())
        self.assertNotIn(stale.name, self.names())

    def test_a_foreign_file_in_the_directory_is_left_alone(self):
        """Only this Agent's own log files are the window's business."""
        retention = self.retention(retention_days=0)
        live = self.handler(retention)
        self.emit(live, "L" * 50)
        foreign = self.rolled("someone-elses-notes.txt", "x" * 50)
        # Dated exactly like an archive, but not one of *our* families: the
        # family is matched from the live name, so another tool's dated file of
        # the same shape by another name is still none of our business.
        other_family = self.rolled(f"notes.log.{stamp(90)}", "x" * 50)

        retention.prune()

        self.assertIn(foreign.name, self.names())
        self.assertIn(other_family.name, self.names())


class OpenFileIsNeverDeletedTest(RolloverTestCase):
    def test_the_file_being_written_survives_pruning(self):
        retention = self.retention(retention_days=0)
        handler = self.handler(retention)
        self.emit(handler, "L" * 50)
        ancient = self.rolled(self.archive(days_ago=90), "y" * 50)

        retention.prune()

        # Every rolled file has to go here -- past the window -- and the open one
        # still may not.
        self.assertEqual(self.names(), [AGENT_LOG_NAME])
        self.assertNotIn(ancient.name, self.names())
        self.assertIn("L" * 50, self.read(AGENT_LOG_NAME))

    def test_an_open_file_survives_a_clock_jump_past_its_own_window(self):
        retention = self.retention(retention_days=14)
        handler = self.handler(retention)
        self.emit(handler, "still here")

        self.clock.advance(90 * DAY)
        retention.prune()

        self.assertEqual(self.names(), [AGENT_LOG_NAME])
        self.assertIn("still here", self.read(AGENT_LOG_NAME))

    def test_an_open_file_whose_name_looks_like_history_is_still_protected(self):
        """The rule is about being open, not about being named `agent.log`.

        The naming rule makes this nearly unreachable -- an archive carries a
        stamp and a live file does not -- but `logging.file` lets an operator name
        the live file anything, including something of that shape. So this is the
        one place the `_live` guard can be observed at all: every other test's open
        file is named `agent.log`, which its own family pattern (`.log.<stamp>`)
        cannot match, so those pass with the guard removed.

        The open file is stamped well outside the window on purpose. An earlier
        version of this test used the window's edge hour, and it passed with the
        guard mutated out -- age alone kept the file, so the protection was never
        exercised. Asserted both ways round at the same age: the same shape,
        unregistered, does go.
        """
        retention = self.retention(retention_days=14)
        retention.register(self.directory / AGENT_LOG_NAME)
        edge = retention.cutoff()
        open_now = self.directory / f"{AGENT_LOG_NAME}.{self._shift(edge, -24)}"
        open_now.write_text("open right now\n")
        retention.register(open_now)
        # Control: history of the same shape and the same age, not open.
        history = self.directory / f"{AGENT_LOG_NAME}.{self._shift(edge, -48)}"
        history.write_text("yesterday\n")

        retention.prune()

        self.assertNotIn(history.name, self.names(), "the same age, unregistered, goes")
        self.assertIn(open_now.name, self.names())
        self.assertIn("open right now", self.read(open_now.name))


class WiringTest(LoggingStateTestCase):
    """The configured numbers reach the installed handlers, and they share one window.

    Restores logging state through the shared base class, because this is the one
    test in this file that installs handlers on the root logger.
    """

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)
        self.log_file = self.directory / AGENT_LOG_NAME

    def installed(self) -> list[HourlyFileHandler]:
        return [
            handler
            for handler in logging.getLogger().handlers
            if isinstance(handler, HourlyFileHandler)
        ]

    def test_the_installed_handlers_carry_the_configured_numbers(self):
        configure_logging(
            log_file=str(self.log_file),
            max_record_bytes=1_111,
            retention_days=3,
        )

        files = self.installed()
        self.assertEqual(len(files), 3, "one per file, all three bounded")
        for handler in files:
            self.assertEqual(handler.max_record_bytes, 1_111)
            self.assertEqual(handler.retention.retention_days, 3)
        # One retention object, not three: the window is the Agent's, and three
        # copies could be told three different things about it.
        self.assertIs(files[0].retention, files[1].retention)
        self.assertIs(files[1].retention, files[2].retention)
        self.assertEqual(
            self.names(), sorted([AGENT_LOG_NAME, TASK_LOG_NAME, ERROR_LOG_NAME])
        )
        # The stdlib's own deletion is off, so nothing but `LogRetention` can
        # remove a file -- and its count rule cannot run behind our back.
        for handler in files:
            self.assertEqual(handler.backupCount, 0)
            self.assertEqual(handler.suffix, ARCHIVE_FORMAT, "archives name the hour")
            self.assertFalse(handler.utc, "names are local, like the record stamps")

    def test_the_shipped_defaults_are_the_rulings_numbers(self):
        configure_logging(log_file=str(self.log_file))

        for handler in self.installed():
            self.assertEqual(handler.max_record_bytes, 1 * 1024 * 1024)
            self.assertEqual(handler.retention.retention_days, 14)
            self.assertEqual(
                (handler.max_record_bytes, handler.retention.retention_days),
                (DEFAULT_LOG_MAX_RECORD_BYTES, DEFAULT_LOG_RETENTION_DAYS),
            )

    def names(self) -> list[str]:
        return sorted(path.name for path in self.directory.iterdir())


class StartupCleanupTest(LoggingStateTestCase):
    """History that aged out while the Agent was stopped goes at startup.

    This is the case a unit test on the writer cannot reach: pruning needs to
    know which names are this Agent's files, and that knowledge reaches the
    retention object only when a name is registered. `configure_logging` therefore
    declares all three *before* the first prune.
    """

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)

    def test_configure_logging_deletes_stale_history_before_the_first_write(self):
        stale = self.directory / f"{AGENT_LOG_NAME}.{stamp(30)}"
        stale.write_text("history from a stop\n")
        recent = self.directory / f"{AGENT_LOG_NAME}.{stamp(2)}"
        recent.write_text("still inside the window\n")

        configure_logging(
            log_file=str(self.directory / AGENT_LOG_NAME), retention_days=14
        )

        self.assertFalse(
            stale.exists(), "history outside the window must not survive startup"
        )
        self.assertTrue(recent.exists(), "and history inside it must")

    def test_the_three_files_are_protected_from_the_startup_cleanup(self):
        """The live files exist from the previous run; the cleanup may not take them."""
        live = self.directory / AGENT_LOG_NAME
        live.write_text("what the last run said\n")

        configure_logging(log_file=str(live), retention_days=0)

        self.assertTrue(live.exists())
        self.assertEqual(live.read_text(), "what the last run said\n")


if __name__ == "__main__":
    unittest.main()
