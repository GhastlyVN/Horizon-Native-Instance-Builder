#!/usr/bin/env python3
"""Resolve the immutable source commit behind the selected Horizon Nightly."""
from __future__ import annotations

import argparse
import json
import re
import urllib.parse
from pathlib import Path

try:
    from scripts.github_api import open_url
except ModuleNotFoundError:  # direct execution: python scripts/resolve_horizon_revision.py
    from github_api import open_url

UPSTREAM_REPOSITORY = "horizon-rust/horizon"
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
REF = re.compile(r"^(?:nightly|elyby-nightly)(?:-\d{4}-\d{2}-\d{2})?$")
SHA = re.compile(r"^[0-9a-f]{40}$")


def release_api(repository: str) -> str:
    if not REPOSITORY.fullmatch(repository):
        raise ValueError("Horizon repository must be OWNER/REPO (for example horizon-rust/horizon).")
    return f"https://api.github.com/repos/{repository}/releases/tags/"


def fetch_release(ref: str = "nightly", repository: str = UPSTREAM_REPOSITORY) -> dict:
    if not REF.fullmatch(ref):
        raise ValueError("Horizon ref must be 'nightly'/'elyby-nightly' or a dated -YYYY-MM-DD tag.")
    with open_url(release_api(repository) + urllib.parse.quote(ref, safe="")) as response:
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
        "repository": repository,
        "tag": value["tag_name"],
        "name": value.get("name", ""),
        "published_at": value.get("published_at", ""),
        "commit": commit,
        "install_tag": f"nightly-{value.get('published_at', '')[:10]}-{commit[:7]}",
        "url": value.get("html_url", ""),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default="nightly", help="Nightly release tag in the selected Horizon repository.")
    parser.add_argument("--repository", default=UPSTREAM_REPOSITORY, help="Horizon Nightly repository as OWNER/REPO; defaults to upstream.")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    release = fetch_release(args.ref, args.repository)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(release["commit"] + "\n", encoding="ascii")
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(release, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Resolved {release['name']} from {release['repository']} to {release['commit']}")


if __name__ == "__main__":
    main()
