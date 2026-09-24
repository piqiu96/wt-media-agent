import logging
import sys
import tempfile
import unittest
from pathlib import Path

import wt_media_agent
from wt_media_agent.runtime.config import load_config
from wt_media_agent.runtime.logging import (
    AGENT_LOG_NAME,
    DEFAULT_COMPONENT,
    ERROR_LOG_NAME,
    TASK_LOG_NAME,
    NoTracebackFormatter,
    configure_from,
    configure_logging,
)

#: The name modules actually log under: `getLogger(__name__)` inside the package
#: yields `<package>.<module>`. Taken from the package object rather than written
#: out, so it cannot drift -- but *not* from `DEFAULT_COMPONENT`, which is the
#: value under test.
PACKAGE_LOGGER = wt_media_agent.__name__

#: Every logger name `dictConfig` may touch, so `tearDown` can restore all of
#: them even when a test mutates which one is configured.
LOGGER_NAMES = (PACKAGE_LOGGER, DEFAULT_COMPONENT, "wt-media-agent")

SILENT_ENV: dict[str, str] = {}


def config(**env: str):
    return load_config(
        env=env,
        frozen=False,
        repo_root=Path("/repo"),
        home=Path("/Users/dev"),
    )


class LoggingStateTestCase(unittest.TestCase):
    """`dictConfig` rewrites global logging state; every test must put it back."""

    def setUp(self):
        root = logging.getLogger()
        self._root_level = root.level
        self._root_handlers = list(root.handlers)
        self._saved = {
            name: (
                list(logging.getLogger(name).handlers),
                logging.getLogger(name).level,
                logging.getLogger(name).propagate,
            )
            for name in dict.fromkeys(LOGGER_NAMES)
        }

    def tearDown(self):
        root = logging.getLogger()
        for handler in root.handlers:
            if handler not in self._root_handlers:
                handler.close()
        root.handlers[:] = self._root_handlers
        root.setLevel(self._root_level)

        for name, (handlers, level, propagate) in self._saved.items():
            logger = logging.getLogger(name)
            for handler in logger.handlers:
                if handler not in handlers:
                    handler.close()
            logger.handlers[:] = handlers
            logger.setLevel(level)
            logger.propagate = propagate


class ConfigureLoggingTest(LoggingStateTestCase):
    def test_stderr_only_installs_no_file_handler(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "agent.log"
            configure_logging(level="INFO", log_file="")
        self.assertEqual(len(logging.getLogger().handlers), 1)
        self.assertIsInstance(logging.getLogger().handlers[0], logging.StreamHandler)
        self.assertFalse(target.exists())

    def test_level_is_applied_and_records_below_it_are_dropped(self):
        configure_logging(level="WARNING")
        self.assertEqual(logging.getLogger().level, logging.WARNING)
        self.assertEqual(logging.getLogger(DEFAULT_COMPONENT).level, logging.WARNING)

    def test_the_configured_logger_is_the_package_modules_log_under(self):
        """Probe the real package name, not `DEFAULT_COMPONENT`.

        The pre-T-03 default was the hyphenated `wt-media-agent`, which matches
        no logger in this codebase: `dictConfig` would then configure a logger
        nothing ever writes to, and this test would still pass if it asked for
        the constant instead of the package.
        """
        configure_logging()
        package = logging.getLogger(PACKAGE_LOGGER)
        self.assertTrue(package.handlers, f"{PACKAGE_LOGGER} must be configured")
        self.assertEqual(package.handlers, list(logging.getLogger().handlers))
        self.assertFalse(package.propagate)

    def test_the_two_names_agree(self):
        self.assertEqual(DEFAULT_COMPONENT, PACKAGE_LOGGER)


class ConfigureFromTest(LoggingStateTestCase):
    def test_log_file_is_written_with_the_documented_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "agent.log"
            configure_from(config(WT_MEDIA_LOG_FILE=str(target), WT_MEDIA_LOG_LEVEL="INFO"))
            logging.getLogger("wt_media_agent.somewhere").info("hello from the agent")
            written = target.read_text()
        self.assertIn("hello from the agent", written)
        self.assertIn("[INFO]", written)
        self.assertIn("wt_media_agent.somewhere", written)

    def test_the_log_directory_is_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "nested" / "deeper" / "agent.log"
            configure_from(config(WT_MEDIA_LOG_FILE=str(target)))
            logging.getLogger("wt_media_agent.somewhere").info("written through a new dir")
            self.assertTrue(target.is_file())

    def test_a_useable_level_lets_debug_records_through(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "agent.log"
            configure_from(config(WT_MEDIA_LOG_FILE=str(target), WT_MEDIA_LOG_LEVEL="DEBUG"))
            logging.getLogger("wt_media_agent.somewhere").debug("verbose detail")
            written = target.read_text()
        self.assertIn("verbose detail", written)

    def test_an_uncreatable_log_directory_degrades_to_stderr(self):
        """A read-only filesystem must not stop the Agent from starting."""
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "blocker"
            blocker.write_text("this is a file, not a directory")
            target = blocker / "logs" / "agent.log"
            with self.assertLogs("wt_media_agent.runtime.logging", level="WARNING") as captured:
                configure_from(config(WT_MEDIA_LOG_FILE=str(target)))
        self.assertIn("stderr only", "\n".join(captured.output))
        handlers = logging.getLogger().handlers
        self.assertEqual(len(handlers), 1)
        self.assertIsInstance(handlers[0], logging.StreamHandler)

    def test_a_development_run_writes_a_file_as_well_as_stderr(self):
        """CHG-057 T-03 (the user's ruling 十一): a dev run's logs are not empty.

        This replaces `test_no_configured_file_means_stderr_only`, which
        asserted the opposite (dev -> stderr only, no file). Both halves are
        still asserted, just with the file present: the record reaches the file,
        and stderr is still installed rather than being traded away for it.

        The repo root is *injected* and temporary, so this cannot write into a
        real checkout -- the property CHG-057 T-02 put a rule behind.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            cfg = load_config(
                env={}, frozen=False, repo_root=repo, home=Path("/home/nobody")
            )

            # The layout is asserted through the resolved paths, not by spelling
            # `.local/logs` out again: a hand-written expectation could drift
            # from `RuntimePaths` and still pass. (Writing the literal shape here
            # is also what T-02's rule flags -- correctly, since `<root> /
            # ".local"` is exactly the pattern that must be justified.)
            self.assertEqual(cfg.paths.origin, "dev")
            self.assertTrue(
                str(cfg.paths.logs_dir).startswith(str(repo)),
                "a dev run must stay inside its own checkout",
            )
            self.assertEqual(cfg.log_file, str(cfg.paths.logs_dir / "agent.log"))

            configure_from(cfg)
            logging.getLogger("wt_media_agent.somewhere").info("a development run writes this")
            written = Path(cfg.log_file).read_text()

        self.assertIn("a development run writes this", written)
        # Since T-04 there are three files, not one. The terminal is still a
        # target -- asserted as a count, so "stderr was kept" cannot quietly
        # become "stderr was replaced by the files".
        kinds = [type(handler).__name__ for handler in logging.getLogger().handlers]
        self.assertEqual(kinds[0], "StreamHandler", "the terminal must still be a target")
        self.assertEqual(kinds.count("RotatingFileHandler"), 3)


class ThreeFileLayoutTest(LoggingStateTestCase):
    """`agent.log` / `task.log` / `error.log` -- and what must *not* reach them.

    The negative half is the point. A routing rule that wrote every record to
    every file would satisfy every positive assertion in here, and the ruling's
    requirement is separation, not duplication.
    """

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self._tmp.name)
        configure_from(config(WT_MEDIA_LOG_FILE=str(self.directory / AGENT_LOG_NAME)))

    def tearDown(self):
        self._tmp.cleanup()
        super().tearDown()

    def read(self, name: str) -> str:
        path = self.directory / name
        return path.read_text() if path.is_file() else ""

    def test_all_three_files_are_created(self):
        for name in (AGENT_LOG_NAME, TASK_LOG_NAME, ERROR_LOG_NAME):
            with self.subTest(name=name):
                self.assertTrue((self.directory / name).is_file(), f"{name} missing")

    def test_the_task_narrative_goes_to_the_task_log_as_well_as_the_agent_log(self):
        logging.getLogger("wt_media_agent.runner.runner").info("executing task abc")
        self.assertIn("executing task abc", self.read(TASK_LOG_NAME))
        self.assertIn("executing task abc", self.read(AGENT_LOG_NAME))

    def test_a_non_runner_record_stays_out_of_the_task_log(self):
        logging.getLogger("wt_media_agent.local_api.server").info("local_api.status.start")
        self.assertIn("local_api.status.start", self.read(AGENT_LOG_NAME))
        self.assertNotIn("local_api.status.start", self.read(TASK_LOG_NAME))

    def test_a_similar_name_is_not_the_runner(self):
        """The prefix matches on a dotted boundary, not a string prefix.

        `wt_media_agent.runner_pool` is a different component; sweeping it into
        the task narrative would be the kind of drift nobody notices.
        """
        logging.getLogger("wt_media_agent.runner_pool").info("not a task node")
        self.assertIn("not a task node", self.read(AGENT_LOG_NAME))
        self.assertNotIn("not a task node", self.read(TASK_LOG_NAME))

    def test_errors_reach_the_error_log_as_well_as_the_agent_log(self):
        logging.getLogger("wt_media_agent.somewhere").error("could not write the file")
        self.assertIn("could not write the file", self.read(ERROR_LOG_NAME))
        self.assertIn("could not write the file", self.read(AGENT_LOG_NAME))

    def test_an_info_record_stays_out_of_the_error_log(self):
        logging.getLogger("wt_media_agent.somewhere").info("ordinary progress")
        self.assertIn("ordinary progress", self.read(AGENT_LOG_NAME))
        self.assertNotIn("ordinary progress", self.read(ERROR_LOG_NAME))

    def test_the_traceback_is_in_the_agent_log_and_not_the_error_log(self):
        """The ruling's split: `error.log` is a record, not a stack dump.

        `local_api/server.py:108` already emits a real one of these through
        `logger.exception`, so this is not a hypothetical shape.
        """
        try:
            raise ValueError("the reason it failed")
        except ValueError:
            logging.getLogger("wt_media_agent.somewhere").exception("failed to write the file")

        agent, error = self.read(AGENT_LOG_NAME), self.read(ERROR_LOG_NAME)
        self.assertIn("failed to write the file", error)
        self.assertIn("Traceback (most recent call last)", agent)
        self.assertIn("ValueError: the reason it failed", agent)
        self.assertNotIn("Traceback", error)
        self.assertNotIn("the reason it failed", error)

    def test_the_no_traceback_formatter_ignores_a_traceback_cached_by_another_handler(self):
        """Reproduces the trap directly, independently of handler order.

        A record is one object handed to every handler, and the base formatter
        *caches* the rendered traceback on `record.exc_text`. So a formatter
        that merely returned "" from `formatException` would still print the
        traceback into `error.log` whenever `agent.log`'s handler had run first
        -- which is the order `configure_logging` happens to install. This
        asserts the second render, not the first, so ordering cannot hide it.
        """
        try:
            raise ValueError("boom")
        except ValueError:
            record = logging.LogRecord(
                "wt_media_agent.somewhere",
                logging.ERROR,
                __file__,
                1,
                "the message",
                (),
                sys.exc_info(),
            )

        standard = logging.Formatter("%(message)s")
        self.assertIn("ValueError: boom", standard.format(record))
        self.assertNotIn("ValueError", NoTracebackFormatter("%(message)s").format(record))


if __name__ == "__main__":
    unittest.main()
