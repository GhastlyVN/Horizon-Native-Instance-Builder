#!/usr/bin/env python3
"""Inspect the supplied Horizon instance and every .hnmod without modifying inputs."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
import pathlib
import re
import stat
import sys
import tempfile
import tomllib
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Match the builder's 8 GiB expansion budget while bounding the compressed input
# itself. The extra byte is sufficient to reject a growing file without hashing it all.
MAX_ARCHIVE_BYTES = 9 * 1024**3
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_MIXIN_BYTES = 1024 * 1024
MAX_SOURCE_FILES = 1024
MAX_SOURCE_FILE_BYTES = 1024 * 1024
MAX_SOURCE_TOTAL_BYTES = 16 * 1024 * 1024
CHUNK_SIZE = 1024 * 1024
REPARSE_POINT = 0x400

# When invoked as `python scripts/inspect_inputs.py`, Python starts with scripts/
# on sys.path. Add the repository root so the inspector uses the same validator as
# the build/preflight path instead of maintaining a second ZIP policy.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.prepare import open_regular_zip_source, preflight_zip_stream, validate_zip_archive  # noqa: E402


def _file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    # Windows st_ctime is a creation timestamp, not POSIX metadata-change time.
    # File identity, size and mtime are the portable signals for content mutation.
    identity = (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
    )
    if sys.platform == "win32":
        return identity
    return (*identity, metadata.st_ctime_ns)


def _metadata_is_link_or_reparse_point(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & REPARSE_POINT
    )


def _hash_stream_bounded(stream) -> str:
    """Hash one open archive, consuming no more than the limit plus one byte."""
    digest = hashlib.sha256()
    total_bytes = 0
    while True:
        remaining = MAX_ARCHIVE_BYTES - total_bytes
        chunk = stream.read(min(CHUNK_SIZE, remaining + 1))
        if not chunk:
            break
        total_bytes += len(chunk)
        if total_bytes > MAX_ARCHIVE_BYTES:
            raise ValueError(
                f"ZIP archive exceeds inspection size limit while hashing "
                f"({MAX_ARCHIVE_BYTES} bytes)"
            )
        digest.update(chunk)
    return digest.hexdigest()


def _copy_stream_to_snapshot(stream, snapshot):
    """Hash and copy one bounded archive so validation reads the exact hashed bytes."""
    digest = hashlib.sha256()
    total_bytes = 0
    position = stream.tell()
    try:
        stream.seek(0)
        snapshot.seek(0)
        snapshot.truncate(0)
        while True:
            remaining = MAX_ARCHIVE_BYTES - total_bytes
            chunk = stream.read(min(CHUNK_SIZE, remaining + 1))
            if not chunk:
                break
            total_bytes += len(chunk)
            if total_bytes > MAX_ARCHIVE_BYTES:
                raise ValueError(
                    f"ZIP archive exceeds inspection size limit while snapshotting "
                    f"({MAX_ARCHIVE_BYTES} bytes)"
                )
            digest.update(chunk)
            snapshot.write(chunk)
        snapshot.flush()
        snapshot.seek(0)
        return digest.hexdigest(), total_bytes
    finally:
        stream.seek(position)


@contextmanager
def _validated_archive(path: pathlib.Path):
    """Validate an immutable snapshot and detect replacement of the original path."""
    path = pathlib.Path(path)
    try:
        path_before = path.lstat()
    except OSError as error:
        raise ValueError(f"Could not inspect archive path {path}: {error}") from error
    # open_regular_zip_source already applies the no-follow, regular-file and
    # O_NONBLOCK guards; keep the earlier metadata for the outer identity check.
    with open_regular_zip_source(path) as stream:
        before = os.fstat(stream.fileno())
        if (
            _metadata_is_link_or_reparse_point(before)
            or not stat.S_ISREG(before.st_mode)
            or _file_identity(before) != _file_identity(path_before)
        ):
            raise ValueError(f"Archive path changed while it was being opened: {path}")
        try:
            path_after_open = path.lstat()
        except OSError as error:
            raise ValueError(f"Archive path changed while it was being opened: {path}") from error
        if (
            _metadata_is_link_or_reparse_point(path_after_open)
            or not stat.S_ISREG(path_after_open.st_mode)
            or _file_identity(path_after_open) != _file_identity(before)
        ):
            raise ValueError(f"Archive path changed while it was being opened: {path}")
        if before.st_size > MAX_ARCHIVE_BYTES:
            raise ValueError(
                f"ZIP archive exceeds inspection size limit "
                f"({MAX_ARCHIVE_BYTES} bytes): {path}"
            )

        # Hashing the source and then asking ZipFile to read it again leaves a
        # same-size/same-mtime mutation window on Windows. Inspect a disk-backed
        # snapshot instead, so the reported digest and every parsed member refer
        # to exactly the same bytes.
        with tempfile.TemporaryFile(mode="w+b") as snapshot:
            digest, archive_bytes = _copy_stream_to_snapshot(stream, snapshot)
            preflight_zip_stream(snapshot, path)
            try:
                archive = zipfile.ZipFile(snapshot)
            except (OSError, zipfile.BadZipFile) as error:
                raise ValueError(f"Invalid ZIP archive: {path}") from error

            with archive:
                # This is the exact shared policy used by preparation. It runs before
                # testzip() can decompress any member from an untrusted archive.
                _, names = validate_zip_archive(archive)
                bad = archive.testzip()
                if bad:
                    raise ValueError(f"ZIP CRC failure in {path.name}: {bad}")
                yield archive, digest, archive_bytes, names
    
        after = os.fstat(stream.fileno())
        try:
            path_after = path.lstat()
        except OSError as error:
            raise ValueError(f"Archive path changed during inspection: {path}") from error
        if (
            not stat.S_ISREG(path_after.st_mode)
            or stat.S_ISLNK(path_after.st_mode)
            or bool(getattr(path_after, "st_file_attributes", 0) & 0x400)
            or _file_identity(before) != _file_identity(after)
            or _file_identity(after) != _file_identity(path_after)
            or _metadata_is_link_or_reparse_point(path_after)
            or not stat.S_ISREG(path_after.st_mode)
        ):
            raise ValueError(f"Archive changed during inspection: {path}")
    

def _normalized_member_name(info: zipfile.ZipInfo) -> str:
    raw_name = info.filename.replace(chr(92), "/")
    return "/".join(pathlib.PurePosixPath(raw_name).parts)


def _read_member_bounded(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    limit: int,
    description: str,
) -> bytes:
    if info.file_size < 0 or info.file_size > limit:
        raise ValueError(
            f"{description} exceeds the inspection limit of {limit} bytes: {info.filename}"
        )
    with archive.open(info) as member:
        data = member.read(limit + 1)
    if len(data) > limit:
        raise ValueError(
            f"{description} exceeds the inspection limit of {limit} bytes: {info.filename}"
        )
    if len(data) != info.file_size:
        raise ValueError(f"{description} size does not match ZIP metadata: {info.filename}")
    return data


def _display_path(path: pathlib.Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(ROOT).as_posix()
    except ValueError:
        # The CLI accepts user-supplied archives outside the checkout.
        return str(resolved)


def read_mod(path: pathlib.Path) -> dict:
    with _validated_archive(path) as (archive, archive_hash, archive_bytes, _):
        members = {
            _normalized_member_name(info): info
            for info in archive.infolist()
        }
        manifest_names = [
            name for name in members
            if pathlib.PurePosixPath(name).name == "horizon-native.json"
        ]
        mixin_names = [
            name for name in members
            if pathlib.PurePosixPath(name).name == "horizon-mixin.toml"
        ]
        if len(manifest_names) != 1 or len(mixin_names) != 1:
            raise ValueError(f"{path.name}: expected one manifest and one mixin config")

        manifest_raw = _read_member_bounded(
            archive, members[manifest_names[0]], MAX_MANIFEST_BYTES, "Native manifest"
        )
        mixin_raw = _read_member_bounded(
            archive, members[mixin_names[0]], MAX_MIXIN_BYTES, "Mixin config"
        )
        try:
            manifest = json.loads(manifest_raw.decode("utf-8"))
            mixin = tomllib.loads(mixin_raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, tomllib.TOMLDecodeError) as error:
            raise ValueError(f"{path.name}: invalid manifest or mixin metadata: {error}") from error
        if not isinstance(manifest, dict):
            raise ValueError(f"{path.name}: native manifest must be a JSON object")

        source_infos = [
            (name, info)
            for name, info in members.items()
            if name.endswith(".rs") and not info.is_dir()
        ]
        if len(source_infos) > MAX_SOURCE_FILES:
            raise ValueError(
                f"{path.name}: too many Rust source files for inspection "
                f"({len(source_infos)}; limit {MAX_SOURCE_FILES})"
            )
        declared_source_bytes = sum(info.file_size for _, info in source_infos)
        if declared_source_bytes > MAX_SOURCE_TOTAL_BYTES:
            raise ValueError(
                f"{path.name}: Rust source exceeds the total inspection limit "
                f"({MAX_SOURCE_TOTAL_BYTES} bytes)"
            )

        source_files: dict[str, str] = {}
        source_summaries: dict[str, dict[str, object]] = {}
        for name, info in source_infos:
            raw = _read_member_bounded(
                archive, info, MAX_SOURCE_FILE_BYTES, "Rust source file"
            )
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError as error:
                raise ValueError(f"{path.name}: Rust source is not valid UTF-8: {name}") from error
            source_files[name] = content
            # Hash the exact bytes stored in the ZIP, not a decoded/re-encoded string.
            source_summaries[name] = {
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }

        interesting = {}
        for name, content in source_files.items():
            lines = content.splitlines()
            matches = [
                index for index, line in enumerate(lines)
                if re.search(r"\b(Button|action|title\.rs|struct\s+|impl\s+|fn\s+)", line, re.I)
            ]
            if matches:
                spans = []
                used = set()
                for index in matches:
                    for position in range(max(0, index - 3), min(len(lines), index + 5)):
                        if position not in used:
                            spans.append(f"{position + 1}: {lines[position]}")
                            used.add(position)
                interesting[name] = "\n".join(spans)

        return {
            "file": path.name,
            "bytes": archive_bytes,
            "sha256": archive_hash,
            "manifest": manifest,
            "mixin": mixin,
            "source_files": source_summaries,
            "source_context": interesting,
            "archive_members": [
                {"name": name, "bytes": info.file_size}
                for name, info in members.items()
                if not info.is_dir()
            ],
        }


def inspect_instance(path: pathlib.Path) -> dict:
    with _validated_archive(path) as (archive, archive_hash, archive_bytes, names):
        roots = sorted({
            pathlib.PurePosixPath(name).parts[0]
            for name in names
            if pathlib.PurePosixPath(name).parts
        })
        candidates = []
        for name in names:
            member_path = pathlib.PurePosixPath(name)
            if (
                len(member_path.parts) >= 3
                and member_path.parts[-1].endswith(".json")
                and member_path.parts[-3] == "versions"
            ):
                candidates.append(name)
        members = {
            _normalized_member_name(info): info
            for info in archive.infolist()
        }
        return {
            "file": _display_path(path),
            "bytes": archive_bytes,
            "sha256": archive_hash,
            "entry_count": len(names),
            "top_level_entries": roots,
            "minecraft_version_metadata": candidates[:100],
            "selected_entries": [
                {"name": name, "bytes": members[name].file_size}
                for name in names
                if any(token in name.lower() for token in (
                    "instance.json", "profile.json", "launcher_profiles.json",
                    "versions/", "mods/", "config/", "resourcepacks/",
                    "shaderpacks/", "options.txt", "servers.dat", "saves/"
                ))
            ][:500],
        }


def ensure_output_does_not_alias_inputs(
    output: pathlib.Path,
    inputs: list[pathlib.Path],
) -> None:
    """Refuse report paths that resolve to, link to, or replace any inspected input."""
    output = pathlib.Path(output)
    if output.is_symlink():
        raise ValueError(f"Report output must not be a symlink: {output}")
    if output.exists() and not output.is_file():
        raise ValueError(f"Report output must be a regular file path: {output}")

    try:
        output_resolved = output.resolve(strict=False)
    except OSError as error:
        raise ValueError(f"Cannot resolve report output path: {output}") from error

    for candidate in inputs:
        candidate = pathlib.Path(candidate)
        try:
            if output_resolved == candidate.resolve(strict=False):
                raise ValueError(
                    f"Report output must not overwrite an inspected input: {candidate}"
                )
            if output.exists() and candidate.exists() and os.path.samefile(output, candidate):
                raise ValueError(
                    f"Report output must not overwrite an inspected input: {candidate}"
                )
        except OSError as error:
            raise ValueError(f"Cannot compare report output with input: {candidate}") from error


def write_report_atomic(output: pathlib.Path, report: dict) -> None:
    """Write beside the destination and replace it only after a complete fsynced report."""
    output = pathlib.Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.",
        suffix=".tmp",
        dir=output.parent,
    )
    temporary = pathlib.Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(report, indent=2, sort_keys=True))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
        temporary = None
        if hasattr(os, "O_DIRECTORY"):
            try:
                directory_fd = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                # Some filesystems do not support directory fsync; file data is already durable.
                pass
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance", type=pathlib.Path, default=ROOT / "input/instance/instance.zip")
    parser.add_argument("--mods", type=pathlib.Path, default=ROOT / "input/mods")
    parser.add_argument("--output", type=pathlib.Path, default=ROOT / "build-report/input-inspection.json")
    args = parser.parse_args(argv)

    mods = sorted(
        path for path in args.mods.glob("*.hnmod")
        if path.is_file()
    )
    ensure_output_does_not_alias_inputs(args.output, [args.instance, args.mods, *mods])
    report = {
        "schema": 1,
        "read_only": True,
        "instance": inspect_instance(args.instance),
        "mods": [read_mod(path) for path in mods],
    }
    write_report_atomic(args.output, report)
    print(json.dumps({
        "instance_sha256": report["instance"]["sha256"],
        "instance_entries": report["instance"]["entry_count"],
        "instance_top_level": report["instance"]["top_level_entries"],
        "mod_count": len(mods),
        "mods": [{
            "file": mod["file"],
            "id": mod["manifest"].get("id"),
            "version": mod["manifest"].get("version"),
            "horizon_version": mod["manifest"].get("horizon_version"),
            "horizon_commit": mod["manifest"].get("horizon_commit"),
            "source_files": list(mod["source_files"]),
        } for mod in report["mods"]],
        "report": str(args.output),
    }, indent=2))


if __name__ == "__main__":
    main()
