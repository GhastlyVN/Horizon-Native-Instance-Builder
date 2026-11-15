#!/usr/bin/env python3
"""Resolve the immutable source commit behind Horizon's current official Nightly."""
from __future__ import annotations

import argparse
import json
import re
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.github.com/repos/horizon-rust/horizon/releases/tags/"
SHA = re.compile(r"^[0-9a-f]{40}$")


def fetch_release(ref: str = "nightly") -> dict:
    if not re.fullmatch(r"nightly(?:-\\d{4}-\\d{2}-\\d{2})?", ref):
        raise ValueError("Horizon ref must be 'nightly' or a catalogued nightly-YYYY-MM-DD tag.")
    request = urllib.request.Request(
        API + urllib.parse.quote(ref, safe=""),
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "horizon-native-instance-builder",
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        value = json.loads(response.read())
    if value.get("tag_name") != ref or not value.get("prerelease"):
        raise ValueError(f"GitHub did not return the requested Horizon Nightly release: {ref}.")
    commit = value.get("target_commitish", "")
    if not isinstance(commit, str) or not SHA.fullmatch(commit):
        body = value.get("body", "")
        match = re.search(r"Source: commit ([0-9a-f]{40})", body)
        commit = match.group(1) if match else ""
    if not SHA.fullmatch(commit):
        raise ValueError("The Nightly release does not identify an immutable 40-character commit.")
    return {
        "tag": value["tag_name"],
        "name": value.get("name", ""),
        "published_at": value.get("published_at", ""),
        "commit": commit,
        "install_tag": f"nightly-{value.get('published_at', '')[:10]}-{commit[:7]}",
        "url": value.get("html_url", ""),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default="nightly", help="Official Nightly release tag from catalog/horizon-versions.json.")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    release = fetch_release(args.ref)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(release["commit"] + "\n", encoding="ascii")
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(release, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Resolved {release['name']} to {release['commit']}")


if __name__ == "__main__":
    main()
