# Bundled CA Trust Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Make the frozen Local Agent validate Cloud HTTPS with a CA bundle included in its executable and surface claim transport failures.

**Architecture:** Agent owns the trust bundle and frozen runtime setup. The build script verifies the vendored file and embeds it; the runtime config layer selects it before client construction. Desktop's existing sidecar hash and signing flow remains the packaging boundary.

**Tech Stack:** Python 3.12+, PyInstaller, `ssl`, `urllib`, `unittest`.

**Spec:** `docs/superpowers/specs/2026-10-08-bundled-ca-trust-design.md`

## Global Constraints

- Keep TLS certificate and hostname verification enabled.
- Do not modify dependency lock files or add a runtime Workspace dependency.
- Do not log credentials, signed URLs, or local download paths.

## Review Focus

- Missing or changed CA file must stop the build or frozen startup, rather than silently use an unknown host store.
- Explicit `SSL_CERT_FILE` must remain usable for private trust environments.
- A transfer claim outage must not flood the log every polling interval.
- A recovered claim must allow a later failure to be reported again.
- The frozen artifact must reach the online Cloud without a host CA override.

## Tasks

### Task 1: Pin and embed the CA bundle

- [x] Vendor the exact certifi 2026.7.22 bundle with provenance, SHA-256 and license notice.
- [x] Add failing tests for build validation and PyInstaller data inclusion.
- [x] Make `build_desktop_sidecar.py` validate and embed `certs/ca-bundle.pem`.
- [x] Run the focused build script tests.

### Task 2: Select trust during frozen startup

- [x] Add failing tests for frozen path selection, explicit override and invalid bundle.
- [x] Configure `SSL_CERT_FILE` before any Cloud client is constructed; source runs keep system trust.
- [x] Run focused runtime and bootstrap tests.

### Task 3: Surface claim transport failures

- [x] Add failing tests for warning deduplication and recovery.
- [x] Log a sanitized claim failure with its underlying TLS/transport reason once per changed failure.
- [x] Run focused runner tests.

### Task 4: Verify the artifact

- [x] Run affected and full Agent tests.
- [x] Build and smoke-test a frozen sidecar.
- [x] With no host CA override, run the non-claiming online HTTPS diagnostic and check for an HTTP response.
- [x] Review the diff and report any toolchain limits.
