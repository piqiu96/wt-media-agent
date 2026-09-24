import logging
import tempfile
import unittest
from pathlib import Path

import wt_media_agent
from wt_media_agent.runtime.config import load_config
from wt_media_agent.runtime.logging import (
    DEFAULT_COMPONENT,
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
        kinds = [type(handler).__name__ for handler in logging.getLogger().handlers]
        self.assertEqual(kinds, ["StreamHandler", "RotatingFileHandler"])


if __name__ == "__main__":
    unittest.main()
