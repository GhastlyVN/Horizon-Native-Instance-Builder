#!/usr/bin/env python3
"""Resolve the immutable source commit behind the selected Horizon Nightly."""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import tempfile
import urllib.parse
from pathlib import Path

try:
    from scripts.github_api import open_url, read_json_limited
except ModuleNotFoundError:  # direct execution: python scripts/resolve_horizon_revision.py
    from github_api import open_url, read_json_limited

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
    value = read_json_limited(
        release_api(repository) + urllib.parse.quote(ref, safe=""), opener=open_url
    )
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


def _metadata_is_link_or_reparse_point(metadata) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & 0x400
    )


def canonical_output_path(path: Path) -> Path:
    """Resolve parent aliases before creating the output directory and leaf."""
    path = Path(path)
    if path.name in {"", ".", ".."}:
        raise ValueError(f"Output path must name a file: {path}")
    parent = path.parent.resolve(strict=False)
    parent.mkdir(parents=True, exist_ok=True)
    return parent / path.name


def refuse_linked_output(path: Path) -> None:
    """Reject existing symlink/reparse, directory, FIFO or other non-regular outputs."""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise ValueError(f"Cannot inspect output path safely: {path}: {error}") from error
    if _metadata_is_link_or_reparse_point(metadata) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"Refusing to write to a linked or non-regular path: {path}")


def write_text_atomically(path: Path, text: str, *, encoding: str) -> None:
    """Publish complete text by same-directory replace; never open the destination for writing."""
    path = canonical_output_path(path)
    refuse_linked_output(path)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    descriptor_owned = True
    try:
        with os.fdopen(descriptor, "w", encoding=encoding, newline="\n") as stream:
            descriptor_owned = False
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())

        # A link inserted while the temporary file was written is refused. If one appears
        # after this check, os.replace replaces the link itself; it never follows its target.
        refuse_linked_output(path)
        os.replace(temporary, path)
    finally:
        if descriptor_owned:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _same_output_target(left: Path, right: Path) -> bool:
    if left == right:
        return True
    try:
        return os.path.samefile(left, right)
    except FileNotFoundError:
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default="nightly", help="Nightly release tag in the selected Horizon repository.")
    parser.add_argument("--repository", default=UPSTREAM_REPOSITORY, help="Horizon Nightly repository as OWNER/REPO; defaults to upstream.")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    output = canonical_output_path(args.output)
    report = canonical_output_path(args.report) if args.report is not None else None
    refuse_linked_output(output)
    if report is not None:
        refuse_linked_output(report)
        if _same_output_target(output, report):
            raise ValueError("Revision commit output and JSON report must be different files.")

    release = fetch_release(args.ref, args.repository)
    write_text_atomically(output, release["commit"] + "\n", encoding="ascii")
    if report is not None:
        write_text_atomically(
            report,
            json.dumps(release, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(f"Resolved {release['name']} from {release.get('repository', args.repository)} to {release['commit']}")


if __name__ == "__main__":
    main()
