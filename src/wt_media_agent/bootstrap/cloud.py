"""Cloud mode: the Agent polls Cloud for tasks.

Two behaviours, and the quiet one is the default:

* **report** -- resolve the configuration, print the environment facts as a
  single JSON object, exit 0. Reads nothing and sends nothing: no HTTP request
  is made, so this is safe to run to answer "what would this Agent do".
* **run** -- `WT_MEDIA_AGENT_RUN_RUNNER` is truthy: `runner.start()` polls Cloud
  until interrupted. This one claims and executes real tasks.

The gate is a configuration key rather than a flag on purpose: a mistyped
command line must not be able to start an Agent that begins claiming work.
"""

from __future__ import annotations

import json

from wt_media_agent.bootstrap.app import build_components, database_path
from wt_media_agent.runtime.version import __version__


def environment_facts(components) -> dict[str, object]:
    """Non-sensitive facts about how this process resolved its configuration.

    `runtime_token` is deliberately absent, as is anything else that travels in
    the environment: this output is meant to be pasted into a bug report.
    """
    config = components.config
    return {
        "mode": "cloud",
        "version": __version__,
        "environment": config.environment,
        "agent_id": config.agent_id,
        "cloud_base_url": config.cloud_base_url,
        "database_path": str(database_path(config)),
        "data_dir": str(config.paths.data_dir),
        "log_file": config.log_file,
        "run_runner": config.run_runner,
    }


def run() -> int:
    components = build_components()
    print(json.dumps(environment_facts(components), ensure_ascii=False, sort_keys=True), flush=True)
    if not components.config.run_runner:
        return 0
    components.runner.start()
    return 0
