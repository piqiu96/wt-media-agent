# Frozen Agent CA bundle

`ca-bundle.pem` is the unmodified `cacert.pem` from certifi 2026.7.22. certifi
packages Mozilla's public root certificates; it is a public trust store, not a
private key or a certificate issued specifically for WT Media. Its MPL-2.0
license notice is included in `ca-bundle.LICENSE`. The source version and exact
file SHA-256 are pinned in `ca-bundle.json`.

The sidecar build checks the SHA-256 and embeds the PEM, metadata, and license
under `certs/` in the executable. A frozen Agent selects that PEM at startup.
`SSL_CERT_FILE` can override it for an explicitly managed private CA store.
Certificate and hostname verification remain enabled.

To update the bundle:

1. Select a published certifi release from its official PyPI project page.
2. In a temporary environment, obtain that release's `certifi/cacert.pem` and
   license file from the same wheel. Copy them without editing certificate text.
3. Update `ca-bundle.json` with the source version and the SHA-256 of the PEM.
4. Run `python -m unittest tests.test_bundled_ca`, build the sidecar, and check
   that a frozen instance reaches Cloud HTTPS without `SSL_CERT_FILE`.

Do not generate a new CA, copy a developer machine's system trust store, or
bundle a server leaf certificate. Those sources are not stable public release
inputs.
