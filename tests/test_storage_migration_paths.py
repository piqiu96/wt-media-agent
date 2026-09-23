import os
import tempfile
import unittest
from pathlib import Path

from wt_media_agent.runtime.config import reset_config_cache
from wt_media_agent.storage.migration import (
    DEFAULT_DB_NAME,
    default_data_dir,
    default_db_path,
    main,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
OLD_DEV_DIR = Path.home() / ".wt-media-agent"
#: The environment this function genuinely reads. Removed for each test so the
#: developer's shell cannot decide the outcome.
CONTROLLED_KEYS = ("WT_MEDIA_AGENT_DATA_DIR", "WT_MEDIA_ENV")


class DataDirTestCase(unittest.TestCase):
    def setUp(self):
        self._saved = {key: os.environ.pop(key, None) for key in CONTROLLED_KEYS}
        reset_config_cache()

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        reset_config_cache()


class DefaultDataDirTest(DataDirTestCase):
    def test_the_explicit_data_dir_still_wins(self):
        os.environ["WT_MEDIA_AGENT_DATA_DIR"] = "/tmp/wt-pinned"
        reset_config_cache()
        self.assertEqual(default_data_dir(), Path("/tmp/wt-pinned"))

    def test_a_checkout_writes_to_the_repository_local_directory(self):
        self.assertEqual(default_data_dir(), REPO_ROOT / ".local" / "data")

    def test_production_uses_the_installed_location(self):
        os.environ["WT_MEDIA_ENV"] = "production"
        reset_config_cache()
        self.assertEqual(
            default_data_dir(), Path.home() / "Library/Application Support/WTMedia/Agent"
        )

    def test_the_old_unconditional_home_directory_is_gone(self):
        """The recorded behaviour change (CHG-056 T-03).

        This assertion is the point of the test: with nothing configured the old
        code returned `~/.wt-media-agent` for every deployment shape. If that
        ever comes back, a development database silently shadows the real one.
        """
        for environment in ({}, {"WT_MEDIA_ENV": "production"}):
            with self.subTest(environment=environment):
                for key, value in environment.items():
                    os.environ[key] = value
                reset_config_cache()
                self.assertNotEqual(default_data_dir(), OLD_DEV_DIR)
                for key in environment:
                    os.environ.pop(key, None)
                reset_config_cache()


class DefaultDbPathTest(DataDirTestCase):
    def test_the_database_name_is_appended(self):
        self.assertEqual(
            default_db_path(Path("/tmp/explicit")), Path("/tmp/explicit") / DEFAULT_DB_NAME
        )

    def test_it_falls_back_to_the_resolved_data_dir(self):
        os.environ["WT_MEDIA_AGENT_DATA_DIR"] = "/tmp/wt-pinned"
        reset_config_cache()
        self.assertEqual(default_db_path(), Path("/tmp/wt-pinned") / DEFAULT_DB_NAME)


class MainTest(DataDirTestCase):
    def test_an_explicit_data_dir_still_creates_the_database_there(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(main(["--data-dir", tmp]), 0)
            self.assertTrue((Path(tmp) / DEFAULT_DB_NAME).is_file())

    def test_the_default_now_follows_the_configured_data_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["WT_MEDIA_AGENT_DATA_DIR"] = tmp
            reset_config_cache()
            self.assertEqual(main([]), 0)
            self.assertTrue((Path(tmp) / DEFAULT_DB_NAME).is_file())


if __name__ == "__main__":
    unittest.main()
