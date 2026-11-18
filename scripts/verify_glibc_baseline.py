#!/usr/bin/env python3
"""Gate the Linux x86_64 native client against a documented GLIBC baseline.

The Linux artifact is built on a pinned Ubuntu 22.04 runner (GLIBC 2.35) so
that it keeps running on older distributions. Hosted runner images drift, so
this gate re-reads the required symbol versions from the produced binary with
``readelf --version-info`` and fails the build when the artifact needs a newer
GLIBC than the documented baseline.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

DEFAULT_BASELINE = "2.35"
ELF_MAGIC = b"\x7fELF"
_VERSION_RE = re.compile(r"GLIBC_(\d+)\.(\d+)(?:\.(\d+))?")


def parse_version(text: str) -> tuple[int, ...]:
    """Parse a dotted GLIBC version such as ``2.35`` into a comparable tuple."""
    parts = text.strip().split(".")
    if not 2 <= len(parts) <= 3 or not all(part.isdigit() for part in parts):
        raise ValueError(f"invalid GLIBC version: {text!r}")
    return tuple(int(part) for part in parts)


def _comparable(version: tuple[int, ...], width: int = 3) -> tuple[int, ...]:
    return version + (0,) * (width - len(version))


def required_versions(readelf_output: str) -> list[tuple[int, ...]]:
    """Return every distinct ``GLIBC_x.y[.z]`` version named by readelf."""
    found = set()
    for match in _VERSION_RE.finditer(readelf_output):
        found.add(tuple(int(group) for group in match.groups() if group is not None))
    return sorted(found)


def evaluate(readelf_output: str, baseline: str) -> dict:
    """Compare the symbol versions required by a binary with ``baseline``."""
    baseline_version = parse_version(baseline)
    versions = required_versions(readelf_output)
    if not versions:
        raise ValueError(
            "readelf output does not declare any GLIBC symbol versions; "
            "refusing to certify the artifact"
        )
    offenders = [
        version
        for version in versions
        if _comparable(version) > _comparable(baseline_version)
    ]
    if offenders:
        formatted = ", ".join(f"GLIBC_{'.'.join(map(str, v))}" for v in offenders)
        raise ValueError(
            f"artifact requires {formatted}, which is newer than the documented "
            f"GLIBC_{baseline} baseline"
        )
    highest = max(versions, key=_comparable)
    return {
        "schema": 1,
        "status": "VERIFIED",
        "baseline": f"GLIBC_{baseline}",
        "required_max": "GLIBC_" + ".".join(map(str, highest)),
        "required": ["GLIBC_" + ".".join(map(str, v)) for v in versions],
    }


def read_versions(binary: Path, readelf: str = "readelf") -> str:
    """Run ``readelf --version-info`` against ``binary`` and return its output."""
    if not binary.is_file():
        raise ValueError(f"missing native client binary: {binary}")
    with binary.open("rb") as stream:
        if stream.read(4) != ELF_MAGIC:
            raise ValueError(f"{binary}: not an ELF executable")
    command = [readelf, "--wide", "--version-info", str(binary)]
    if callable(readelf):
        # Test hook: the workflow always passes the real readelf binary name.
        return readelf(command).stdout
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as error:
        raise ValueError(f"readelf is not available: {error}") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or "").strip() or f"exit {error.returncode}"
        raise ValueError(f"readelf failed on {binary}: {detail}") from error
    return completed.stdout


def verify_binary(binary: Path, baseline: str, readelf: str = "readelf") -> dict:
    """Verify one ELF binary and return the report used by the release gate."""
    report = evaluate(read_versions(binary, readelf), baseline)
    report["binary"] = binary.name
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--max-glibc", default=DEFAULT_BASELINE)
    parser.add_argument("--readelf", default="readelf")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    report = verify_binary(args.binary, args.max_glibc, args.readelf)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(
        f"{args.binary.name} requires at most {report['required_max']}; "
        f"baseline {report['baseline']} verified."
    )


if __name__ == "__main__":
    main()
