"""Cloud mode: the Agent polls Cloud for tasks.

Two behaviours, and the quiet one is the default:

* **report** -- resolve the configuration, print the environment facts as a
  single JSON object, exit 0. Reads nothing and sends nothing: no HTTP request
  is made, so this is safe to run to answer "what would this Agent do".
* **run** -- `WT_MEDIA_AGENT_RUN_RUNNER` is truthy: both loops start and poll
  Cloud until interrupted. This one claims and executes real tasks.

The gate is a configuration key rather than a flag on purpose: a mistyped
command line must not be able to start an Agent that begins claiming work.
"""

from __future__ import annotations

import json

from wt_media_agent.bootstrap.app import build_components, database_path, start_task_loops
from wt_media_agent.runtime.logging import redact
from wt_media_agent.runtime.version import __version__


def environment_facts(components) -> dict[str, object]:
    """Non-sensitive facts about how this process resolved its configuration.

    `runtime_token` is deliberately absent, as is anything else that travels in
    the environment: this output is meant to be pasted into a bug report.

    Each value is masked (`redact`) because "non-sensitive" is a property of the
    *key*, not of what a configured value may contain: `cloud.base_url` can hold
    a credential (`https://user:password@host`), and that would have travelled
    into the report verbatim (measured -- CHG-057 T-06). Masking here, rather
    than at the `print`, covers every consumer of these facts.
    """
    config = components.config
    facts: dict[str, object] = {
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
    return {
        key: redact(value) if isinstance(value, str) else value
        for key, value in facts.items()
    }


def run() -> int:
    components = build_components()
    print(json.dumps(environment_facts(components), ensure_ascii=False, sort_keys=True), flush=True)
    if not components.config.run_runner:
        return 0
    loops = start_task_loops(components)
    # Run mode blocks, as it always has: this process *is* the Agent, and
    # returning here would end it with two loops that had claimed work and
    # nowhere left to report it. Joining rather than calling `runner.start()` on
    # this thread is what lets both loops be started from the same place.
    #
    # Note what does not happen here: nothing binds this process to Cloud. No
    # Desktop supervises it and the credential arrives on a local API call this
    # mode does not serve, so the transfer loop claims nothing and needs no
    # credential. That is the honest reading of run mode -- an Agent that can do
    # browser work and cannot download -- rather than a fault to report.
    for loop in loops:
        loop.join()
    return 0
