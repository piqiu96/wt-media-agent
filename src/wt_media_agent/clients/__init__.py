"""Outbound clients for external systems: BitBrowser, Cloud, platform APIs.

Per ADR-0016 this is the bottom business layer. Clients may call `services/`
(one site does: `clients/bilibili/identity.py` uses `services.browser.cdp` to
run the nav probe through the browser), and must never import `executors/`.

Each client is a subpackage: `bitbrowser/`, `cloud/`, `bilibili/`, `baijiahao/`.
`platform_identity.py` is a flat module because it is dispatch only -- it holds
no protocol of its own.
"""
