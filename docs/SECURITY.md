# Security model

Native `.hnmod` files contain Rust code and are not sandboxed. Build only mods you trust.

- Use GitHub-hosted ephemeral runners, not a personal self-hosted runner.
- Workflow token is read-only.
- Checkout does not persist credentials.
- No secrets are supplied to the native build.
- ZIP traversal and symlink entries are rejected.
- The extractor validates and reads from one bounded disk-backed snapshot, so a source ZIP changed after preflight cannot change the bytes being extracted.
- Large instance ZIPs should be same-repository Release assets; the workflow does not accept arbitrary URLs.
- Do not add shared build caches for untrusted native mod sources.
