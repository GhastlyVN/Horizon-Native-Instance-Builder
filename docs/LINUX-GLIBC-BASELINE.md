# Linux x86_64 GLIBC baseline

The Linux `horizon-client` artifact is built on **Ubuntu 22.04**, which ships
**GLIBC 2.35**. That is the oldest baseline this repository currently targets
and tests; the artifact is not expected to run on distributions with an older
`glibc` (for example Debian 11 / Ubuntu 20.04).

Building on `ubuntu-latest` is deliberately avoided: the symbol versions in the
produced binary follow the hosted runner image, so a runner upgrade silently
raises the GLIBC requirement (the earlier artifact required `GLIBC_2.43` and
failed to start on Debian 13 / GLIBC 2.41).

## What the release gate enforces

`verify-native-artifacts` performs two checks on the Linux binary before any
instance bundle is packaged:

1. **Symbol-version gate** — `scripts/verify_glibc_baseline.py` runs
   `readelf --version-info` on `native-runtime/horizon-client`, extracts every
   `GLIBC_x.y[.z]` requirement, and fails the build if any requirement is newer
   than the documented baseline. The report is uploaded as
   `linux-glibc-baseline.json` inside the `native-build-verification` artifact.
2. **Oldest-image smoke test** — the same binary is exercised with `ldd` inside
   an `ubuntu:22.04` container with the expected runtime libraries installed.
   Unresolved libraries fail the build.

The baseline is configured once as `LINUX_GLIBC_BASELINE` in
`.github/workflows/build.yml` and recorded in each Linux `build.txt` report.

## Supported / tested matrix

| Distribution | GLIBC | Status |
| --- | --- | --- |
| Ubuntu 22.04 | 2.35 | Built and smoke-tested in CI |
| Debian 12 | 2.36 | Expected to work (newer GLIBC) |
| Ubuntu 24.04 | 2.39 | Expected to work (newer GLIBC) |
| Debian 11 / Ubuntu 20.04 | 2.31 | Not supported by this baseline |

"Expected to work" means the binary requires no symbol newer than GLIBC 2.35;
runtime behaviour still depends on graphics, audio and Vulkan drivers being
available on the target system.

## Changing the baseline

1. Update `LINUX_GLIBC_BASELINE` in `.github/workflows/build.yml`.
2. Change the pinned Linux runner to a matching image.
3. Update this document and the README note.
4. Re-run the full `Build Horizon Native Instance` workflow with
   `[build-native]` and confirm both the symbol gate and the container smoke
   test pass on the new image.
