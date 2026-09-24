"""Retention and rotation of the three Agent log files (CHG-057 T-07).

The ruling (六) states four bounds, and each one gets its own mutation here:

* a file rolls when the UTC date turns over;
* a file rolls when it reaches the single-file cap (20 MB as shipped);
* one oversized record is *truncated* -- marked `truncate=true original_size=<n>`
  -- and does not start a new file;
* rolled files outside the retention window are deleted;
* the oldest rolled files are deleted when the total budget is exceeded;
* the file currently being written is never one of the deleted ones.

The clock is injected, so "a day passed" and "the window closed" are statements
this test makes rather than things it waits for. The caps are passed in small:
the shipped 20 MB is asserted once, on the wiring, and exercised at 200 bytes,
because a test that writes 20 MB to prove a bound is a test nobody runs.
"""

from __future__ import annotations

import logging
import sys
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from support import LoggingStateTestCase

from wt_media_agent.runtime.logging import (
    AGENT_LOG_NAME,
    ERROR_LOG_NAME,
    TASK_LOG_NAME,
    BoundedFileHandler,
    LogBudget,
    configure_logging,
)

DAY = 24 * 60 * 60
#: A fixed instant, so a rolled file's name is asserted against a literal.
BASE = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc).timestamp()


def stamp(offset_days: int) -> str:
    """The `YYYYMMDD` a rolled file carries `offset_days` from the base date."""
    moment = datetime.fromtimestamp(BASE, tz=timezone.utc) + timedelta(days=offset_days)
    return moment.strftime("%Y%m%d")


class Clock:
    """A clock the test moves."""

    def __init__(self, stamp: float = BASE) -> None:
        self.now = stamp

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

    def budget(self, *, total_bytes: int = 10_000_000, retention_days: int = 14) -> LogBudget:
        return LogBudget(
            self.directory,
            total_bytes=total_bytes,
            retention_days=retention_days,
            clock=self.clock,
        )

    def handler(
        self,
        budget: LogBudget,
        *,
        max_bytes: int = 10_000,
        name: str = AGENT_LOG_NAME,
    ) -> BoundedFileHandler:
        handler = BoundedFileHandler(
            self.directory / name, max_bytes=max_bytes, budget=budget
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        self.addCleanup(handler.close)
        return handler

    def emit(self, handler: BoundedFileHandler, message: str) -> None:
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

    def rolled(self, name: str, body: str) -> Path:
        """A rolled file placed by name, for the rules that read the directory."""
        path = self.directory / name
        path.write_text(body)
        return path

    def names(self) -> list[str]:
        return sorted(path.name for path in self.directory.iterdir())

    def read(self, name: str) -> str:
        return (self.directory / name).read_text()


class DateRollTest(RolloverTestCase):
    def test_a_day_change_rolls_the_file(self):
        budget = self.budget()
        handler = self.handler(budget)

        self.emit(handler, "first")
        self.clock.advance(DAY)
        self.emit(handler, "second")

        # The rolled name is asserted exactly: the day it covered, then the
        # index. Renaming it after the *new* day would file yesterday's records
        # under today.
        self.assertEqual(self.names(), sorted([AGENT_LOG_NAME, f"agent-{stamp(0)}-1.log"]))
        self.assertEqual(self.read(AGENT_LOG_NAME).strip(), "second")
        self.assertEqual(self.read(f"agent-{stamp(0)}-1.log").strip(), "first")

    def test_two_rolls_on_one_day_get_different_names(self):
        budget = self.budget()
        handler = self.handler(budget, max_bytes=60)

        self.emit(handler, "a" * 30)
        self.emit(handler, "b" * 30)
        self.emit(handler, "c" * 30)

        self.assertEqual(
            self.names(),
            sorted(
                [
                    AGENT_LOG_NAME,
                    f"agent-{stamp(0)}-1.log",
                    f"agent-{stamp(0)}-2.log",
                ]
            ),
        )
        self.assertEqual(self.read(f"agent-{stamp(0)}-1.log").strip(), "a" * 30)
        self.assertEqual(self.read(f"agent-{stamp(0)}-2.log").strip(), "b" * 30)


class SizeRollTest(RolloverTestCase):
    def test_a_file_that_reaches_the_single_file_cap_rolls(self):
        budget = self.budget()
        handler = self.handler(budget, max_bytes=85)

        self.emit(handler, "x" * 80)  # 81 bytes with the newline
        self.emit(handler, "second")

        self.assertEqual(self.names(), sorted([AGENT_LOG_NAME, f"agent-{stamp(0)}-1.log"]))
        self.assertEqual(self.read(AGENT_LOG_NAME).strip(), "second")
        self.assertEqual(self.read(f"agent-{stamp(0)}-1.log").strip(), "x" * 80)

    def test_a_file_is_not_rolled_before_it_reaches_the_cap(self):
        """Paired control: without it, a handler that rolled on every record passes."""
        budget = self.budget()
        handler = self.handler(budget, max_bytes=85)

        self.emit(handler, "x" * 40)
        self.emit(handler, "y" * 40)

        self.assertEqual(self.names(), [AGENT_LOG_NAME])
        self.assertEqual(self.read(AGENT_LOG_NAME).split(), ["x" * 40, "y" * 40])


class OversizedRecordTest(RolloverTestCase):
    def test_one_oversized_record_is_truncated_and_marked(self):
        budget = self.budget()
        handler = self.handler(budget, max_bytes=200)

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

    def test_the_marker_records_bytes_not_characters(self):
        """`original_size` is a byte count, which is what the cap is about."""
        budget = self.budget()
        handler = self.handler(budget, max_bytes=60)

        self.emit(handler, "é" * 100)  # two bytes each

        text = self.read(AGENT_LOG_NAME)
        self.assertLessEqual(len(text.encode()), 60)
        self.assertIn(" truncate=true original_size=200", text)
        self.assertNotIn("�", text, "a half-written character is dropped, not mangled")

    def test_a_record_that_fits_is_not_marked(self):
        """Paired control, and the reason the marker is not a fixed suffix."""
        budget = self.budget()
        handler = self.handler(budget, max_bytes=200)

        self.emit(handler, "y" * 100)

        self.assertNotIn("truncate=true", self.read(AGENT_LOG_NAME))


class AgePruningTest(RolloverTestCase):
    def test_rolled_files_outside_the_retention_window_are_deleted(self):
        budget = self.budget(retention_days=14)
        live = self.handler(budget)
        self.emit(live, "live")
        # Both sides of the boundary, so "delete everything" cannot pass: a file
        # 13 days old is inside a 14-day window, one 14 days old is not.
        inside = self.rolled(f"agent-{stamp(-13)}-1.log", "kept\n")
        outside = self.rolled(f"agent-{stamp(-14)}-1.log", "gone\n")

        budget.prune()

        self.assertEqual(self.names(), sorted([AGENT_LOG_NAME, inside.name]))

    def test_a_roll_ages_out_the_history_without_being_asked(self):
        """Pruning has to happen on the write path too, not only on demand."""
        budget = self.budget(retention_days=2)
        handler = self.handler(budget, max_bytes=60)
        stale = self.rolled(f"agent-{stamp(-9)}-1.log", "old\n")

        self.emit(handler, "a" * 30)
        self.emit(handler, "b" * 30)  # rolls, and the roll is what prunes

        self.assertNotIn(stale.name, self.names())
        self.assertIn(f"agent-{stamp(0)}-1.log", self.names())


class TotalBudgetTest(RolloverTestCase):
    def test_the_oldest_rolled_files_are_deleted_when_the_budget_is_exceeded(self):
        budget = self.budget(total_bytes=160)
        live = self.handler(budget)
        self.emit(live, "L" * 50)  # 51 bytes
        oldest = self.rolled(f"agent-{stamp(-3)}-1.log", "a" * 100)
        second = self.rolled(f"agent-{stamp(-2)}-2.log", "b" * 100)
        kept = self.rolled(f"agent-{stamp(-1)}-1.log", "c" * 100)

        budget.prune()

        # Deleted: the two oldest, dated first and then by index. Kept: the
        # newest rolled file and the live one -- "deleted what it should" and
        # "did not delete what it should not", in one assertion each.
        self.assertNotIn(oldest.name, self.names())
        self.assertNotIn(second.name, self.names())
        self.assertIn(kept.name, self.names())
        self.assertIn(AGENT_LOG_NAME, self.names())
        self.assertEqual(self.total(), 151)

    def test_the_budget_is_shared_by_the_three_files(self):
        """400 MB is the Agent's budget, not 400 MB per file."""
        budget = self.budget(total_bytes=300)
        live = {
            name: self.handler(budget, name=name)
            for name in (AGENT_LOG_NAME, TASK_LOG_NAME, ERROR_LOG_NAME)
        }
        for name, handler in live.items():
            self.emit(handler, name)
        task_rolled = self.rolled(f"task-{stamp(-2)}-1.log", "t" * 150)
        error_rolled = self.rolled(f"error-{stamp(-1)}-1.log", "e" * 150)

        budget.prune()

        self.assertLessEqual(self.total(), 300)
        self.assertNotIn(task_rolled.name, self.names(), "the oldest is whichever family it is")
        self.assertIn(error_rolled.name, self.names())
        for name, handler in live.items():
            self.assertIn(name, self.names())
            self.assertIn(name, self.read(name), "the open file keeps what it wrote")

    def test_a_foreign_file_in_the_directory_is_left_alone(self):
        """Only this Agent's own log files are the budget's business."""
        budget = self.budget(total_bytes=10)
        live = self.handler(budget)
        self.emit(live, "L" * 50)
        foreign = self.rolled("someone-elses-notes.txt", "x" * 50)

        budget.prune()

        self.assertIn(foreign.name, self.names())

    def test_the_total_does_not_grow_with_the_volume_written(self):
        """The bound is a bound, not a delay: 200 KB in, one file's worth out.

        Pruning runs at a roll, so between two prunes the open files may still
        grow to their own caps -- that is the difference between
        `total_bytes + 3 * max_bytes` and `total_bytes`, and it is what this
        pins. What it rules out is the thing the ruling is actually about: a
        directory whose size follows how much has been logged.
        """
        budget = self.budget(total_bytes=500)
        handler = self.handler(budget, max_bytes=200)

        for index in range(2_000):
            self.emit(handler, f"{index:04d}" + "x" * 96)  # 200 KB of records

        self.assertLessEqual(self.total(), 500 + 3 * 200)

    def total(self) -> int:
        return sum(path.stat().st_size for path in self.directory.iterdir())


class BudgetRobustnessTest(RolloverTestCase):
    def test_a_file_that_vanishes_between_listing_and_sizing_is_not_fatal(self):
        """The budget is shared by three handlers running in three threads.

        A roll in one thread can rename a file away after another thread's prune
        has listed it. Letting that raise would abort the prune *and* lose the
        record whose emit triggered it -- the failure this guard exists to stop,
        and the one the unguarded version shows: `FileNotFoundError` out of
        `prune`.
        """
        # Sized to leave room: the point of this test is that the *sizing* step
        # survives the file being gone, so nothing else may delete anything
        # (a file over budget would be removed whether or not its size was read).
        budget = self.budget(total_bytes=200)
        live = self.handler(budget)
        self.emit(live, "L" * 50)
        vanished = self.rolled(f"agent-{stamp(-1)}-1.log", "x" * 50)
        real_stat = Path.stat

        def flaky(path: Path, *args, **kwargs):
            if path.name == vanished.name:
                raise FileNotFoundError(path)
            return real_stat(path, *args, **kwargs)

        with mock.patch.object(Path, "stat", flaky):
            deleted = budget.prune()

        self.assertEqual(deleted, [])
        self.assertIn(vanished.name, self.names(), "a real file is still there")
        self.assertIn(AGENT_LOG_NAME, self.names())
        self.assertIn("L" * 50, self.read(AGENT_LOG_NAME))


class OpenFileIsNeverDeletedTest(RolloverTestCase):
    def test_the_file_being_written_survives_both_pruning_rules(self):
        budget = self.budget(total_bytes=10, retention_days=0)
        handler = self.handler(budget)
        self.emit(handler, "L" * 50)
        ancient = self.rolled(f"agent-{stamp(-90)}-1.log", "y" * 50)

        budget.prune()

        # Every rolled file has to go here -- past the window and over the
        # budget -- and the open one still may not.
        self.assertEqual(self.names(), [AGENT_LOG_NAME])
        self.assertNotIn(ancient.name, self.names())
        self.assertIn("L" * 50, self.read(AGENT_LOG_NAME))

    def test_an_open_file_survives_a_clock_jump_past_its_own_window(self):
        budget = self.budget(retention_days=14)
        handler = self.handler(budget)
        self.emit(handler, "still here")

        self.clock.advance(90 * DAY)
        budget.prune()

        self.assertEqual(self.names(), [AGENT_LOG_NAME])
        self.assertIn("still here", self.read(AGENT_LOG_NAME))

    def test_an_open_file_whose_name_looks_like_history_is_still_protected(self):
        """The rule is about being open, not about being named `agent.log`.

        A live file whose name *does* match a rolled one is the only case where
        the protection and the naming rule disagree -- and it is the case the
        `register` guard exists for (a second handler, an operator who named the
        file with a date in it). Asserted both ways round, so this cannot pass by
        the name simply not matching: the same file, unregistered, is deleted.
        """
        budget = self.budget(retention_days=0)
        budget.register(self.directory / AGENT_LOG_NAME)
        open_now = self.directory / f"agent-{stamp(0)}-1.log"
        open_now.write_text("open right now\n")
        budget.register(open_now)

        budget.prune()

        self.assertIn(open_now.name, self.names())
        self.assertIn("open right now", self.read(open_now.name))

        # Control: history by the same name, not registered as open.
        history = self.directory / f"agent-{stamp(0)}-2.log"
        history.write_text("yesterday\n")
        budget.prune()

        self.assertNotIn(history.name, self.names())
        self.assertIn(open_now.name, self.names())


class WiringTest(LoggingStateTestCase):
    """The configured caps reach the installed handlers, and they are one budget.

    Restores logging state through the shared base class, because this is the
    one test in this file that installs handlers on the root logger.
    """

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)
        self.log_file = self.directory / AGENT_LOG_NAME

    def installed(self) -> list[BoundedFileHandler]:
        return [
            handler
            for handler in logging.getLogger().handlers
            if isinstance(handler, BoundedFileHandler)
        ]

    def test_the_installed_handlers_carry_the_configured_caps(self):
        configure_logging(
            log_file=str(self.log_file),
            max_bytes=1_111,
            retention_days=3,
            total_bytes=9_999,
        )

        files = self.installed()
        self.assertEqual(len(files), 3, "one per file, all three bounded")
        for handler in files:
            self.assertEqual(handler.max_bytes, 1_111)
            self.assertEqual(handler.budget.total_bytes, 9_999)
            self.assertEqual(handler.budget.retention_days, 3)
        # One budget object, not three: the cap is the Agent's, and a per-file
        # copy would multiply it by three without anything saying so.
        self.assertIs(files[0].budget, files[1].budget)
        self.assertIs(files[1].budget, files[2].budget)
        self.assertEqual(self.names(), sorted([AGENT_LOG_NAME, TASK_LOG_NAME, ERROR_LOG_NAME]))

    def test_the_shipped_defaults_are_the_rulings_numbers(self):
        configure_logging(log_file=str(self.log_file))

        for handler in self.installed():
            self.assertEqual(handler.max_bytes, 20 * 1024 * 1024)
            self.assertEqual(handler.budget.retention_days, 14)
            self.assertEqual(handler.budget.total_bytes, 400 * 1024 * 1024)

    def names(self) -> list[str]:
        return sorted(path.name for path in self.directory.iterdir())


class StartupCleanupTest(LoggingStateTestCase):
    """History that aged out while the Agent was stopped goes at startup.

    This is the case a unit test on the writer cannot reach: pruning needs to
    know which names are this Agent's files, and that knowledge reaches the
    budget only when a handler is built. Pruning *before* the handlers exist --
    which is where it has to happen, before the first write -- therefore deleted
    nothing at all, while still looking right in a run where something rolled
    later. Measured on a real process (CHG-057 T-07): with the shipped caps the
    1999-dated files survived a whole run.
    """

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)
        today = datetime.now(tz=timezone.utc).date()
        self.day = lambda offset: (today + timedelta(days=offset)).strftime("%Y%m%d")

    def test_configure_logging_deletes_stale_history_before_the_first_write(self):
        stale = self.directory / f"agent-{self.day(-30)}-1.log"
        stale.write_text("history from a stop\n")
        recent = self.directory / f"agent-{self.day(-2)}-1.log"
        recent.write_text("still inside the window\n")

        configure_logging(
            log_file=str(self.directory / AGENT_LOG_NAME), retention_days=14
        )

        self.assertFalse(stale.exists(), "history outside the window must not survive startup")
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
