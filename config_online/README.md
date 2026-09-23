# Agent config -- release replacement source

Nothing reads this directory at runtime. Shipping copies it over `config/`,
wholesale, so that the release artifact carries the production configuration.

- Keep this directory 1:1 with `config/` in filenames and in key sets. A test
  asserts it, because replacing one directory with the other must not be able to
  silently drop a key the runtime expects.
- Credentials. Same rule as `config/`: `runtime_token` is read from the
  environment only and the loader ignores every sensitive key it finds in a TOML
  file. ADR-0016 §7 permits credentials in this directory; the loader is
  deliberately narrower today (recorded as a deviation in CHG-20260923-056), so
  a credential placed here would be ignored, not honoured.
- `cloud.base_url` currently holds the loopback value because Q-01
  (CHG-20260923-056) is still open -- the production Cloud address is undecided.
  That is the safe reading, not a finished value: resolve it before release.
