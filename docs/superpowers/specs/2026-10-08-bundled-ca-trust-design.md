# Bundled CA Trust for the Frozen Local Agent

## Problem

The macOS 0.1.0 sidecar uses Python's `urllib` for Cloud HTTPS calls. Its frozen Python/OpenSSL has no usable default CA file on the target Mac, so task polling fails at TLS verification before the request reaches Cloud. The transfer loop currently hides that failure at DEBUG level.

## Design

- Vendor the unmodified `certifi` 2026.7.22 `cacert.pem` (Mozilla root trust collection) and its MPL-2.0 notice in the Agent repository. Record its SHA-256. Do not generate a CA, copy the build host's trust store, or pin the current Cloud leaf certificate.
- The sidecar build validates the pinned bundle and embeds it in the PyInstaller executable under `certs/`. The existing sidecar manifest hashes the resulting executable, and Desktop packages that executable without a new resource contract.
- During frozen Agent bootstrap, set `SSL_CERT_FILE` to the embedded bundle before constructing network clients, unless an explicit `SSL_CERT_FILE` override is already present. Verify the selected file can load as a CA store. Source runs keep their existing system trust behavior.
- Keep hostname and certificate verification enabled. A missing or malformed embedded bundle fails startup visibly. Transfer claim transport errors are logged once per changed failure reason at WARNING level, with no credentials or signed URLs.

## Verification

- Unit tests cover bundle hash validation, PyInstaller inclusion, path resolution, explicit override, invalid bundle, and transfer claim logging.
- Build and launch a frozen sidecar with no host CA override. A read-only online health request or a claim with an intentionally invalid node credential must reach Cloud and return an HTTP response, rather than a TLS certificate error. No real download task is claimed by the diagnostic.
