"""Browser-facing services: CDP control and cookie extraction.

Per ADR-0016 these sit above `clients/` (they may use a client) and must not
import `executors/`.
"""
