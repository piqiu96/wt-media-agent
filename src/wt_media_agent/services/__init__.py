"""Capabilities built on top of `clients/`: browser control, cookies, proxy.

Per ADR-0016 these sit above `clients/` and must not import `executors/`.
`browser/` and `net/` group the capabilities by area.
"""
