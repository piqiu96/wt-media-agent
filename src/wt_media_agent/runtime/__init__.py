"""Cross-cutting runtime concerns: constants, version, config, paths, logging.

Per ADR-0016 the `runtime` layer is cross-cutting: it must not reference any
business layer (clients, services, executors, storage, local_api).
"""
