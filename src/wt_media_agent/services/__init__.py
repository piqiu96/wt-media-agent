"""Capabilities built on top of `clients/`: browser control, cookies, proxy.

Per ADR-0016 these sit above `clients/` and must not import `executors/`.
`profile_guard.py` is a flat module; `browser/` and `net/` group the rest.
"""
