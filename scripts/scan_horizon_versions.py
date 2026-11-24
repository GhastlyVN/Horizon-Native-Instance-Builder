#!/usr/bin/env python3
"""Create a verified catalog of installable official Horizon Nightly releases."""
from __future__ import annotations

import argparse
import json
import os
import stat
import tempfile
import re
import urllib.parse
from pathlib import Path

try:
    from scripts.github_api import open_url, read_json_limited
except ModuleNotFoundError:  # direct execution: python scripts/scan_horizon_versions.py
    from github_api import open_url, read_json_limited

API = "https://api.github.com/repos/horizon-rust/horizon/releases?per_page=100"
SHA = re.compile(r"^[0-9a-f]{40}$")
NIGHTLY_TAG = re.compile(r"^nightly(?:-\d{4}-\d{2}-\d{2})?$")
ASSET = re.compile(r"^horizon-(?:linux-x86_64|windows-x64|macos-(?:arm64|x64))\.(?:tar\.gz|zip)$")


def _github_https_url(value: str) -> bool:
    parsed = urllib.parse.urlparse(value)
    return (
        parsed.scheme == "https"
        and parsed.hostname == "github.com"
        and parsed.username is None
        and parsed.password is None
        and parsed.port in (None, 443)
    )


def fetch_releases() -> list[dict]:
    value = read_json_limited(API, opener=open_url)
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


def _asset_url(url: str, name: str) -> str:
    """Only accept HTTPS GitHub release URLs whose basename is the asset name."""
    parsed = urllib.parse.urlparse(url) if isinstance(url, str) else None
    if (
        parsed is None
        or parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.path.rstrip("/").rsplit("/", 1)[-1] != name
    ):
        raise ValueError(f"unsafe download URL (unapproved asset download URL) for {name}: {url}")
    return url


def build_catalog(releases: list[dict], limit: int) -> dict:
    versions = []
    seen = set()
    for release in releases:
        tag = release.get("tag_name", "")
        if not isinstance(tag, str):
            continue
        if tag in seen or not NIGHTLY_TAG.fullmatch(tag) or not release.get("prerelease"):
            continue
        assets = [
            {"name": item.get("name", ""), "size": item.get("size", 0),
             "url": _asset_url(item.get("browser_download_url", ""), item.get("name", ""))}
            for item in release.get("assets", [])
            if isinstance(item, dict) and ASSET.fullmatch(item.get("name", ""))
        ]
        if not assets:
            continue
        page = release.get("html_url", "")
        if not isinstance(page, str) or not _github_https_url(page):
            raise ValueError(f"{tag}: unapproved release URL: {page}")
        for asset in assets:
            if not _github_https_url(asset["url"]):
                raise ValueError(f"{tag}: unapproved asset download URL: {asset['url']}")
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


def _metadata_is_link_or_reparse_point(metadata) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & 0x400
    )


def refuse_linked_output(path: Path) -> None:
    """Reject existing links, reparse points, directories, FIFOs and special outputs."""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise ValueError(f"Cannot inspect catalog output safely: {path}: {error}") from error
    if _metadata_is_link_or_reparse_point(metadata) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"Refusing to write to a symlink/reparse point or non-regular path: {path}")


def write_catalog_atomically(path: Path, catalog: dict) -> None:
    """Publish a complete catalog with same-directory replace, never opening the leaf."""
    path = Path(path)
    if path.name in {"", ".", ".."}:
        raise ValueError(f"Catalog output must name a file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path = path.parent.resolve(strict=False) / path.name
    refuse_linked_output(path)

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    descriptor_owned = True
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            descriptor_owned = False
            stream.write(json.dumps(catalog, indent=2, sort_keys=True))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())

        # os.replace replaces a link inserted after this check; it never follows its target.
        refuse_linked_output(path)
        os.replace(temporary, path)
        temporary = None
        if hasattr(os, "O_DIRECTORY"):
            try:
                directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
    finally:
        if descriptor_owned:
            os.close(descriptor)
        if temporary is not None:
            temporary.unlink(missing_ok=True)

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()
    if not 1 <= args.limit <= 100:
        raise SystemExit("--limit must be between 1 and 100.")
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output = output.parent.resolve(strict=False) / output.name
    refuse_linked_output(output)
    catalog = build_catalog(fetch_releases(), args.limit)
    write_catalog_atomically(output, catalog)
    print(f"Catalogued {len(catalog['versions'])} installable Horizon Nightly versions.")
    for item in catalog["versions"][:5]:
        print(f"- {item['tag']}: {item['commit']}")


if __name__ == "__main__":
    main()
