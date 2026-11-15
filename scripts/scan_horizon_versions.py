#!/usr/bin/env python3
"""Create a verified catalog of installable official Horizon Nightly releases."""
from __future__ import annotations

import argparse
import json
import re
import urllib.request
from pathlib import Path

API = "https://api.github.com/repos/horizon-rust/horizon/releases?per_page=100"
SHA = re.compile(r"^[0-9a-f]{40}$")
NIGHTLY_TAG = re.compile(r"^nightly(?:-\d{4}-\d{2}-\d{2})?$")
ASSET = re.compile(r"^horizon-(?:linux-x86_64|windows-x64|macos-(?:arm64|x64))\.(?:tar\.gz|zip)$")


def fetch_releases() -> list[dict]:
    request = urllib.request.Request(API, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "horizon-native-instance-builder",
    })
    with urllib.request.urlopen(request, timeout=60) as response:
        value = json.loads(response.read())
    if not isinstance(value, list):
        raise ValueError("Horizon releases endpoint did not return a list.")
    return value


def source_commit(release: dict) -> str:
    commit = release.get("target_commitish", "")
    if not isinstance(commit, str) or not SHA.fullmatch(commit):
        body = release.get("body", "")
        match = re.search(r"Source: commit ([0-9a-f]{40})", body if isinstance(body, str) else "")
        commit = match.group(1) if match else ""
    if not SHA.fullmatch(commit):
        raise ValueError(f"{release.get('tag_name')}: no immutable source commit.")
    return commit


def build_catalog(releases: list[dict], limit: int) -> dict:
    versions = []
    seen = set()
    for release in releases:
        tag = release.get("tag_name", "")
        if tag in seen or not NIGHTLY_TAG.fullmatch(tag) or not release.get("prerelease"):
            continue
        assets = [
            {"name": item.get("name", ""), "size": item.get("size", 0),
             "url": item.get("browser_download_url", "")}
            for item in release.get("assets", [])
            if isinstance(item, dict) and ASSET.fullmatch(item.get("name", ""))
        ]
        if not assets:
            continue
        commit = source_commit(release)
        published = release.get("published_at", "")
        if not isinstance(published, str) or not published:
            continue
        versions.append({
            "tag": tag,
            "name": release.get("name") or tag,
            "published_at": published,
            "commit": commit,
            "install_tag": f"nightly-{published[:10]}-{commit[:7]}",
            "url": release.get("html_url", ""),
            "assets": assets,
        })
        seen.add(tag)
    versions.sort(key=lambda item: item["published_at"], reverse=True)
    if not versions:
        raise ValueError("No installable Horizon Nightly releases with immutable commits were found.")
    return {
        "schema": 1,
        "source": "https://github.com/horizon-rust/horizon/releases",
        "versions": versions[:limit],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()
    if not 1 <= args.limit <= 100:
        raise SystemExit("--limit must be between 1 and 100.")
    catalog = build_catalog(fetch_releases(), args.limit)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(catalog, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Catalogued {len(catalog['versions'])} installable Horizon Nightly versions.")
    for item in catalog["versions"][:5]:
        print(f"- {item['tag']}: {item['commit']}")


if __name__ == "__main__":
    main()
