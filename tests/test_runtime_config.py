import os
import tempfile
import tomllib
import unittest
from dataclasses import fields as dataclass_fields
from pathlib import Path

from wt_media_agent.runtime.config import (
    _SPEC,
    AgentConfig,
    ConfigError,
    default_config_dir,
    get_config,
    load_config,
    reset_config_cache,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_REPO = Path("/repo")
FAKE_HOME = Path("/Users/dev")

#: Every test that calls `load()` gets an empty environment unless it says
#: otherwise, so a variable in the developer's shell cannot decide the result.
SILENT_ENV: dict[str, str] = {}


def load(**kwargs) -> AgentConfig:
    kwargs.setdefault("env", SILENT_ENV)
    kwargs.setdefault("frozen", False)
    kwargs.setdefault("repo_root", FAKE_REPO)
    kwargs.setdefault("home", FAKE_HOME)
    return load_config(**kwargs)


def write_config(directory: Path, text: str) -> Path:
    path = directory / "agent.toml"
    path.write_text(text)
    return path


class DefaultsTest(unittest.TestCase):
    def test_defaults_when_nothing_is_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load(config_dir=Path(tmp))
        self.assertEqual(cfg.environment, "development")
        self.assertEqual(cfg.cloud_base_url, "http://127.0.0.1:18080")
        self.assertEqual(cfg.cloud_timeout_seconds, 10.0)
        self.assertEqual(cfg.local_api_host, "127.0.0.1")
        self.assertEqual(cfg.local_api_port, 8765)
        self.assertEqual(cfg.local_api_address, "127.0.0.1:8765")
        self.assertEqual(cfg.bitbrowser_api_url, "http://127.0.0.1:54345")
        self.assertEqual(cfg.bitbrowser_timeout_seconds, 5.0)
        self.assertIsNone(cfg.bitbrowser_create_timeout_seconds)
        self.assertIsNone(cfg.bitbrowser_mutation_timeout_seconds)
        self.assertEqual(cfg.data_dir, "")
        self.assertEqual(cfg.log_level, "INFO")
        self.assertEqual(cfg.log_file, "")
        self.assertEqual(cfg.runtime_token, "")
        self.assertEqual(cfg.paths.origin, "dev")
        self.assertFalse(cfg.is_production)

    def test_spec_and_dataclass_do_not_drift(self):
        """`_SPEC` drives resolution, the dataclass shapes the result.

        If they diverge, a key silently stops being configurable -- the loaded
        document would carry a value nothing ever reads.
        """
        spec_names = [field.name for field in _SPEC]
        declared = {f.name for f in dataclass_fields(AgentConfig)} - {"paths"}
        self.assertEqual(set(spec_names), declared)
        self.assertEqual(len(spec_names), len(set(spec_names)), "duplicate name in _SPEC")


class FileLayerTest(unittest.TestCase):
    def test_file_values_beat_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_config(
                Path(tmp),
                """
environment = "staging"
[cloud]
base_url = "http://cloud.example:9443"
timeout_seconds = 2.5
[local_api]
port = 9999
[logging]
level = "DEBUG"
""",
            )
            cfg = load(config_dir=Path(tmp))
        self.assertEqual(cfg.environment, "staging")
        self.assertEqual(cfg.cloud_base_url, "http://cloud.example:9443")
        self.assertEqual(cfg.cloud_timeout_seconds, 2.5)
        self.assertEqual(cfg.local_api_port, 9999)
        self.assertEqual(cfg.log_level, "DEBUG")
        # Untouched keys still fall back.
        self.assertEqual(cfg.local_api_host, "127.0.0.1")

    def test_missing_file_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load(config_dir=Path(tmp) / "does-not-exist")
        self.assertEqual(cfg.local_api_port, 8765)

    def test_malformed_file_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_config(Path(tmp), "this is not = = toml")
            with self.assertRaises(ConfigError) as caught:
                load(config_dir=Path(tmp))
        self.assertIn("not valid TOML", str(caught.exception))

    def test_the_shipped_development_config_parses(self):
        """The file that actually ships in the repo, not a fixture."""
        cfg = load(config_dir=default_config_dir())
        self.assertEqual(cfg.environment, "development")
        self.assertEqual(cfg.local_api_port, 8765)
        self.assertEqual(cfg.bitbrowser_api_url, "http://127.0.0.1:54345")
        self.assertEqual(cfg.paths.origin, "dev")


class EnvLayerTest(unittest.TestCase):
    def test_env_beats_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_config(Path(tmp), "[local_api]\nport = 1111\n")
            cfg = load(config_dir=Path(tmp), env={"WT_MEDIA_LOCAL_API_PORT": "2222"})
        self.assertEqual(cfg.local_api_port, 2222)

    def test_canonical_log_names_work(self):
        cfg = load(env={"WT_MEDIA_LOG_LEVEL": "WARNING", "WT_MEDIA_LOG_FILE": "/tmp/a.log"})
        self.assertEqual(cfg.log_level, "WARNING")
        self.assertEqual(cfg.log_file, "/tmp/a.log")

    def test_compatibility_aliases_still_work(self):
        """scripts/start-health.sh sets the WT_MEDIA_AGENT_LOG_* names."""
        cfg = load(
            env={
                "WT_MEDIA_AGENT_LOG_LEVEL": "ERROR",
                "WT_MEDIA_AGENT_LOG_FILE": "/tmp/agent.log",
            }
        )
        self.assertEqual(cfg.log_level, "ERROR")
        self.assertEqual(cfg.log_file, "/tmp/agent.log")

    def test_canonical_name_wins_when_both_are_present(self):
        cfg = load(
            env={
                "WT_MEDIA_LOG_FILE": "/canonical.log",
                "WT_MEDIA_AGENT_LOG_FILE": "/alias.log",
            }
        )
        self.assertEqual(cfg.log_file, "/canonical.log")

    def test_every_spec_field_has_at_least_one_env_name(self):
        for spec in _SPEC:
            self.assertTrue(spec.env, f"{spec.name} has no environment name")

    def test_a_blank_override_means_unset_for_a_file_key(self):
        """`WT_MEDIA_LOG_FILE=` must not pin the log to a file named ''."""
        cfg = load(env={"WT_MEDIA_ENV": "production", "WT_MEDIA_LOG_FILE": ""})
        self.assertEqual(cfg.log_file, str(cfg.paths.logs_dir / "agent.log"))


class InvalidValueTest(unittest.TestCase):
    def test_non_numeric_port_from_env_names_the_key(self):
        with self.assertRaises(ConfigError) as caught:
            load(env={"WT_MEDIA_LOCAL_API_PORT": "not-a-port"})
        self.assertIn("local_api.port", str(caught.exception))

    def test_non_numeric_timeout_from_file_names_the_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_config(Path(tmp), '[cloud]\ntimeout_seconds = "soon"\n')
            with self.assertRaises(ConfigError) as caught:
                load(config_dir=Path(tmp))
        self.assertIn("cloud.timeout_seconds", str(caught.exception))

    def test_optional_timeouts_keep_their_pre_existing_tolerance(self):
        """These two used to swallow a bad value and fall back; they still do."""
        cfg = load(
            env={
                "WT_MEDIA_BITBROWSER_CREATE_TIMEOUT_SECONDS": "abc",
                "WT_MEDIA_BITBROWSER_MUTATION_TIMEOUT_SECONDS": "   ",
            }
        )
        self.assertIsNone(cfg.bitbrowser_create_timeout_seconds)
        self.assertIsNone(cfg.bitbrowser_mutation_timeout_seconds)

    def test_optional_timeouts_read_a_real_number(self):
        cfg = load(env={"WT_MEDIA_BITBROWSER_CREATE_TIMEOUT_SECONDS": "120"})
        self.assertEqual(cfg.bitbrowser_create_timeout_seconds, 120.0)


class CredentialHandlingTest(unittest.TestCase):
    SECRET_VALUE = "sk-live-do-not-log-me"

    def test_sensitive_keys_in_the_file_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_config(
                Path(tmp),
                f"""
runtime_token = "{self.SECRET_VALUE}"
[cloud]
base_url = "http://cloud.example:9443"
password = "{self.SECRET_VALUE}"
""",
            )
            cfg = load(config_dir=Path(tmp))
        self.assertEqual(cfg.runtime_token, "")
        # The non-secret key in the same table still applies, so this is a
        # targeted ignore rather than the whole file being discarded.
        self.assertEqual(cfg.cloud_base_url, "http://cloud.example:9443")

    def test_the_ignored_value_never_reaches_the_log(self):
        """Reporting the key by name is the whole point; the value must not leak."""
        with tempfile.TemporaryDirectory() as tmp:
            write_config(Path(tmp), f'runtime_token = "{self.SECRET_VALUE}"\n')
            with self.assertLogs("wt_media_agent.runtime.config", level="WARNING") as captured:
                load(config_dir=Path(tmp))
        joined = "\n".join(captured.output)
        self.assertIn("runtime_token", joined, "the key should be named")
        self.assertNotIn(self.SECRET_VALUE, joined, "the value must not appear")

    def test_runtime_token_does_come_from_the_environment(self):
        cfg = load(env={"WT_MEDIA_AGENT_RUNTIME_TOKEN": "from-env"})
        self.assertEqual(cfg.runtime_token, "from-env")

    def test_unknown_keys_are_reported_by_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_config(Path(tmp), '[cloud]\nbase_uri = "typo"\n')
            with self.assertLogs("wt_media_agent.runtime.config", level="WARNING") as captured:
                cfg = load(config_dir=Path(tmp))
        self.assertIn("cloud.base_uri", "\n".join(captured.output))
        self.assertEqual(cfg.cloud_base_url, "http://127.0.0.1:18080")


class PathsIntegrationTest(unittest.TestCase):
    def test_data_dir_override_drives_the_paths(self):
        cfg = load(env={"WT_MEDIA_AGENT_DATA_DIR": "/tmp/wt-pinned"})
        self.assertEqual(cfg.paths.origin, "override")
        self.assertEqual(cfg.paths.data_dir, Path("/tmp/wt-pinned"))

    def test_production_environment_selects_installed_paths(self):
        cfg = load(env={"WT_MEDIA_ENV": "production"})
        self.assertTrue(cfg.is_production)
        self.assertEqual(cfg.paths.origin, "installed")
        self.assertEqual(
            cfg.paths.data_dir, FAKE_HOME / "Library/Application Support/WTMedia/Agent"
        )
        self.assertEqual(cfg.paths.logs_dir, FAKE_HOME / "Library/Logs/WTMedia/Agent")


class ConfigCacheTest(unittest.TestCase):
    def test_get_config_caches_and_reset_clears(self):
        reset_config_cache()
        saved = os.environ.get("WT_MEDIA_LOCAL_API_PORT")
        os.environ["WT_MEDIA_LOCAL_API_PORT"] = "31337"
        try:
            self.assertEqual(get_config().local_api_port, 31337)
            os.environ["WT_MEDIA_LOCAL_API_PORT"] = "31338"
            self.assertEqual(get_config().local_api_port, 31337, "should be cached")
            reset_config_cache()
            self.assertEqual(get_config().local_api_port, 31338)
        finally:
            if saved is None:
                os.environ.pop("WT_MEDIA_LOCAL_API_PORT", None)
            else:
                os.environ["WT_MEDIA_LOCAL_API_PORT"] = saved
            reset_config_cache()


class ConfigMirrorTest(unittest.TestCase):
    """`config_online/` replaces `config/` wholesale, so they must line up."""

    def test_filenames_match(self):
        dev = sorted(p.name for p in (REPO_ROOT / "config").iterdir() if p.is_file())
        online = sorted(p.name for p in (REPO_ROOT / "config_online").iterdir() if p.is_file())
        self.assertEqual(dev, online)
        self.assertIn("agent.toml", dev, "the mirror test must not pass on two empty dirs")

    def test_key_sets_match(self):
        dev = _key_paths(tomllib.loads((REPO_ROOT / "config" / "agent.toml").read_text()))
        online = _key_paths(
            tomllib.loads((REPO_ROOT / "config_online" / "agent.toml").read_text())
        )
        self.assertEqual(dev, online)

    def test_the_release_config_is_production_and_the_dev_one_is_not(self):
        cfg = load(config_dir=REPO_ROOT / "config_online")
        self.assertTrue(cfg.is_production)
        self.assertEqual(cfg.paths.origin, "installed")


def _key_paths(document: dict, prefix: str = "") -> list[str]:
    found: list[str] = []
    for key, value in document.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            found.extend(_key_paths(value, f"{path}."))
        else:
            found.append(path)
    return sorted(found)


if __name__ == "__main__":
    unittest.main()
