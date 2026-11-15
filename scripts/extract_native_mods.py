#!/usr/bin/env python3
"""Safely unpack .hnmod ZIP bundles into the directory layout Horizon expects."""
import argparse
import json
import os
import pathlib
import re
import shutil
import stat
import tempfile
import uuid
import zipfile

try:
    from scripts.prepare import (
        MAX_ARCHIVE_ENTRIES,
        MAX_MEMBER_UNCOMPRESSED_BYTES,
        MAX_TOTAL_UNCOMPRESSED_BYTES,
        WINDOWS_RESERVED_NAMES,
        validate_zip,
    )
except ModuleNotFoundError:  # direct execution: python scripts/extract_native_mods.py
    from prepare import (
        MAX_ARCHIVE_ENTRIES,
        MAX_MEMBER_UNCOMPRESSED_BYTES,
        MAX_TOTAL_UNCOMPRESSED_BYTES,
        validate_zip,
    )


BUNDLE_STEM = re.compile(r"[a-z][a-z0-9_-]{0,63}")
CHUNK_SIZE = 1024 * 1024
REPARSE_POINT = 0x400


def _is_link_or_reparse_point(path: pathlib.Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & REPARSE_POINT
    )


def extract_bundle(archive_path: pathlib.Path, destination: pathlib.Path) -> pathlib.Path:
    # The output directory is derived from the archive's filesystem name, not a ZIP member.
    # Keep it to the same portable identifier grammar as a native mod ID before any writes.
    stem = archive_path.stem
    if (
        archive_path.suffix.lower() != ".hnmod"
        or not BUNDLE_STEM.fullmatch(stem)
        or stem.upper() in WINDOWS_RESERVED_NAMES
    ):
        raise ValueError(f"{archive_path.name}: unsafe .hnmod filename")

    _, names = validate_zip(archive_path)
    native = [name for name in names if pathlib.PurePosixPath(name).name == "horizon-native.json"]
    mixins = [name for name in names if pathlib.PurePosixPath(name).name == "horizon-mixin.toml"]
    if len(native) != 1 or len(mixins) != 1:
        raise ValueError(
            f"{archive_path.name}: expected exactly one horizon-native.json and one "
            "horizon-mixin.toml; validate the .hnmod bundle layout"
        )

    native_path = pathlib.PurePosixPath(native[0])
    root_prefix = native_path.parent.as_posix()
    root_prefix = "" if root_prefix == "." else root_prefix + "/"
    try:
        with zipfile.ZipFile(archive_path) as bundle:
            entries = bundle.infolist()
            if not entries:
                raise ValueError(f"{archive_path.name}: empty ZIP archive")
            if len(entries) > MAX_ARCHIVE_ENTRIES:
                raise ValueError(
                    f"{archive_path.name}: too many ZIP entries "
                    f"({len(entries)}; limit {MAX_ARCHIVE_ENTRIES})"
                )

            # validate_zip normalizes separators and Unicode paths. Retain the corresponding
            # ZipInfo objects so archives containing accepted backslash separators still read.
            members = {
                "/".join(pathlib.PurePosixPath(info.filename.replace(chr(92), "/")).parts): info
                for info in entries
            }
            native_info = members.get(native[0])
            if native_info is None:
                raise ValueError(f"{archive_path.name}: native manifest entry could not be resolved")
            raw_manifest = bundle.read(native_info)
            manifest = json.loads(raw_manifest)
            if not isinstance(manifest, dict):
                raise ValueError(f"{archive_path.name}: horizon-native.json must contain a JSON object")
            mod_id = manifest.get("id")
            if not isinstance(mod_id, str) or not BUNDLE_STEM.fullmatch(mod_id):
                raise ValueError(f"{archive_path.name}: invalid or missing native mod id")
            expected_mixin = root_prefix + f"mixins/{mod_id}/horizon-mixin.toml"
            if expected_mixin not in mixins:
                raise ValueError(
                    f"{archive_path.name}: expected {expected_mixin} to match native mod id {mod_id!r}"
                )

            if _is_link_or_reparse_point(destination):
                raise ValueError(f"{destination}: symlink extraction destination refused")
            destination.mkdir(parents=True, exist_ok=True)
            destination = destination.resolve()
            target = destination / stem
            if target.parent != destination:
                raise ValueError(f"{archive_path.name}: unsafe native mod extraction target")
            if _is_link_or_reparse_point(target):
                raise ValueError(f"{target}: symlink or reparse-point extraction target refused")
            if target.exists() and not target.is_dir():
                raise ValueError(f"{target}: existing extraction target is not a directory")

            # Stage on the destination filesystem. The old target is not touched until every
            # member has been streamed, size-checked and the required manifests are present.
            with tempfile.TemporaryDirectory(
                prefix=f".{stem}.stage-", dir=destination
            ) as temp:
                staging = pathlib.Path(temp)
                total_uncompressed = 0
                for name in names:
                    if root_prefix and not name.startswith(root_prefix):
                        continue
                    relative = name[len(root_prefix):] if root_prefix else name
                    if not relative:
                        continue
                    info = members.get(name)
                    if info is None:
                        raise ValueError(f"{archive_path.name}: ZIP entry could not be resolved: {name}")
                    if info.is_dir() or relative.endswith("/"):
                        continue
                    rel = pathlib.PurePosixPath(relative)
                    if rel.is_absolute() or any(part in ("", ".", "..") for part in rel.parts):
                        raise ValueError(f"{archive_path.name}: unsafe bundle path {relative!r}")
                    if info.file_size < 0 or info.file_size > MAX_MEMBER_UNCOMPRESSED_BYTES:
                        raise ValueError(
                            f"{archive_path.name}: ZIP member exceeds the uncompressed size limit: {name}"
                        )
                    output = staging.joinpath(*rel.parts)
                    output.parent.mkdir(parents=True, exist_ok=True)
                    actual_member_uncompressed = 0
                    with bundle.open(info) as source, output.open("xb") as sink:
                        while chunk := source.read(CHUNK_SIZE):
                            actual_member_uncompressed += len(chunk)
                            total_uncompressed += len(chunk)
                            if actual_member_uncompressed > MAX_MEMBER_UNCOMPRESSED_BYTES:
                                raise ValueError(
                                    f"{archive_path.name}: actual ZIP member exceeds the uncompressed size limit: {name}"
                                )
                            if actual_member_uncompressed > info.file_size:
                                raise ValueError(
                                    f"{archive_path.name}: ZIP member expands beyond its declared size: {name}"
                                )
                            if total_uncompressed > MAX_TOTAL_UNCOMPRESSED_BYTES:
                                raise ValueError(
                                    f"{archive_path.name}: actual ZIP archive exceeds the total uncompressed size limit "
                                    f"({MAX_TOTAL_UNCOMPRESSED_BYTES} bytes)"
                                )
                            sink.write(chunk)
                    if actual_member_uncompressed != info.file_size:
                        raise ValueError(
                            f"{archive_path.name}: ZIP member size does not match its declared size: {name}"
                        )

                if not (staging / "horizon-native.json").is_file():
                    raise ValueError(f"{archive_path.name}: native manifest was not extracted at bundle root")
                if not (staging / f"mixins/{mod_id}/horizon-mixin.toml").is_file():
                    raise ValueError(f"{archive_path.name}: mixin manifest was not extracted")

                backup = None
                if target.exists():
                    backup = destination / f".{stem}.backup-{uuid.uuid4().hex}"
                    os.replace(target, backup)
                try:
                    os.replace(staging, target)
                except BaseException:
                    if backup is not None:
                        try:
                            os.replace(backup, target)
                        except OSError as rollback_error:
                            raise RuntimeError(
                                f"{archive_path.name}: extraction failed and the previous bundle "
                                f"could not be restored; it remains at {backup}"
                            ) from rollback_error
                    raise
                if backup is not None:
                    shutil.rmtree(backup, ignore_errors=True)
            return target
    except (zipfile.BadZipFile, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError(f"{archive_path.name}: invalid native mod archive: {error}") from error


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=pathlib.Path, nargs="?", default=pathlib.Path("input/mods"))
    parser.add_argument("--destination", type=pathlib.Path, default=pathlib.Path("input/mods-unpacked"))
    args = parser.parse_args()
    args.destination.mkdir(parents=True, exist_ok=True)
    archives = sorted(args.source.glob("*.hnmod")) if args.source.is_dir() else []
    if not archives:
        print("No .hnmod bundles supplied; Core Build will produce the unmodified Horizon client.")
        return
    extracted = [extract_bundle(path, args.destination) for path in archives]
    print(f"Extracted {len(extracted)} native mod bundle(s): " + ", ".join(p.name for p in extracted))


if __name__ == "__main__":
    main()
