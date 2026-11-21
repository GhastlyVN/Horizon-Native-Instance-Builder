#!/usr/bin/env python3
"""Build a Horizon game-home drop-in bundle with a real instances/<id> directory."""
import argparse
import contextlib
import hashlib
import json
import pathlib
import os
import re
import stat
import tempfile
import zipfile

if __package__:
    from .prepare import (
        MAX_ARCHIVE_BYTES,
        MAX_PATH_COMPONENTS,
        MAX_PATH_UTF8_BYTES,
        _collision_key,
        _portable_windows_component,
        preflight_zip_stream,
        validate_zip_archive,
    )
else:  # Executed as `python scripts/package_instance.py`.
    from prepare import (
        MAX_ARCHIVE_BYTES,
        MAX_PATH_COMPONENTS,
        MAX_PATH_UTF8_BYTES,
        _collision_key,
        _portable_windows_component,
        validate_zip_archive,
    )


def _file_identity(metadata):
    # ctime changes when an opened source is atomically renamed away from its path,
    # even though the descriptor still refers to the same immutable bytes.
    return (
        metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns
    )


def _copy_zip_member_to_zip(archive, arcname: str, source, info) -> None:
    """Stream a validated source member; ZipExtFile verifies its CRC at EOF."""
    target_info = zipfile.ZipInfo(arcname)
    target_info.file_size = info.file_size
    target_info.compress_type = archive.compression
    target_info._compresslevel = archive.compresslevel
    target_info.create_system = 3
    mode = (info.external_attr >> 16) & 0o777 or 0o644
    target_info.external_attr = (stat.S_IFREG | mode) << 16
    copied = 0
    with source.open(info) as input_stream, archive.open(target_info, "w") as output_stream:
        while chunk := input_stream.read(1024 * 1024):
            copied += len(chunk)
            if copied > info.file_size:
                raise ValueError(f"Instance ZIP member expanded beyond its declared size: {info.filename}")
            output_stream.write(chunk)
    if copied != info.file_size:
        raise ValueError(f"Instance ZIP member size does not match its declaration: {info.filename}")


def _copy_stream_to_zip(archive, arcname: str, stream, metadata):
    """Hash the exact bytes streamed into the package, without reopening the input path."""
    info = zipfile.ZipInfo(arcname)
    info.file_size = metadata.st_size
    info.compress_type = archive.compression
    info._compresslevel = archive.compresslevel
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | stat.S_IMODE(metadata.st_mode)) << 16
    digest = hashlib.sha256()
    copied = 0
    stream.seek(0)
    with archive.open(info, "w") as output_stream:
        while chunk := stream.read(1024 * 1024):
            copied += len(chunk)
            if copied > metadata.st_size:
                raise ValueError(f"Input changed while packaging: {arcname}")
            digest.update(chunk)
            output_stream.write(chunk)
    stream.seek(0)
    if copied != metadata.st_size:
        raise ValueError(f"Input size changed while packaging: {arcname}")
    return digest.hexdigest(), copied


def safe_members(archive: zipfile.ZipFile):
    """Use the same portable ZIP policy as the builder's validator and extractor."""
    _, names = validate_zip_archive(archive)
    infos = archive.infolist()
    if len(infos) != len(names):
        raise ValueError("Instance ZIP entries changed during validation")
    yield from zip(infos, names)


def _reject_output_aliases(output: pathlib.Path, inputs: list[pathlib.Path]) -> None:
    """Never let a package output replace or follow one of its source files."""
    if output.is_symlink():
        raise ValueError("Package output must not be a symlink")
    if output.exists() and not output.is_file():
        raise ValueError("Package output must be a regular file")

    output_resolved = output.resolve()
    for source in inputs:
        if output_resolved == source.resolve():
            raise ValueError(f"Package output must not overwrite an input: {source}")
        if output.exists() and source.exists():
            try:
                aliases = os.path.samefile(output, source)
            except OSError:
                aliases = False
            if aliases:
                raise ValueError(f"Package output must not overwrite an input: {source}")


def _open_regular_file_no_follow(path: pathlib.Path, description: str):
    """Open one regular input without following a symlink or accepting a path swap."""
    try:
        before = path.lstat()
    except OSError as error:
        raise ValueError(f"{description} is unavailable: {path}") from error
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(
            f"{description} must be a regular file (symlinks and special files are refused): {path}"
        )

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValueError(
            f"{description} could not be opened without following a symlink: {path}"
        ) from error

    stream = os.fdopen(descriptor, "rb")
    try:
        opened = os.fstat(stream.fileno())
        try:
            current = path.lstat()
        except OSError as error:
            raise ValueError(f"{description} changed while it was being opened: {path}") from error
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or _file_identity(before) != _file_identity(opened)
            or _file_identity(opened) != _file_identity(current)
        ):
            raise ValueError(f"{description} changed while it was being opened: {path}")
        return stream, opened
    except BaseException:
        stream.close()
        raise


def _member_is_directory(info: zipfile.ZipInfo) -> bool:
    """Match the validator's normalized backslash-directory policy."""
    return info.is_dir() or info.filename.replace(chr(92), "/").endswith("/")


def _validate_portable_basename(name: str, description: str) -> None:
    if (
        not name
        or "/" in name
        or chr(92) in name
        or not _portable_windows_component(name)
    ):
        raise ValueError(f"{description} has a non-portable filename: {name!r}")


def _validate_generated_zip_paths(paths: list[str]) -> None:
    """Validate the final names written to the package, not only their source names."""
    seen_casefold = set()
    files_casefold = set()
    prefix_spellings = {}

    for raw_name in paths:
        try:
            path_bytes = len(raw_name.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise ValueError(f"Generated ZIP path is not valid Unicode: {raw_name!r}") from error
        parts = pathlib.PurePosixPath(raw_name.replace(chr(92), "/")).parts
        if (
            path_bytes > MAX_PATH_UTF8_BYTES
            or len(parts) > MAX_PATH_COMPONENTS
            or not parts
            or raw_name.startswith("/")
            or chr(0) in raw_name
            or ".." in parts
            or any(not _portable_windows_component(part) for part in parts)
        ):
            raise ValueError(f"Unsafe generated ZIP path: {raw_name!r}")

        normalized = "/".join(parts)
        folded = _collision_key(normalized)
        if folded in seen_casefold:
            raise ValueError(f"Case-insensitive generated ZIP path collision: {raw_name}")

        parents = ["/".join(parts[:index]) for index in range(1, len(parts))]
        folded_parents = [_collision_key(parent) for parent in parents]
        conflict = next((parent for parent in folded_parents if parent in files_casefold), None)
        if conflict is not None:
            raise ValueError(
                f"Generated ZIP file/directory path collision: {raw_name} is below file {conflict}"
            )

        for parent, folded_parent in zip(parents, folded_parents):
            previous = prefix_spellings.get(folded_parent)
            if previous is not None and previous != parent:
                raise ValueError(f"Case-insensitive generated ZIP directory collision: {raw_name}")
            prefix_spellings[folded_parent] = parent

        if folded in prefix_spellings:
            raise ValueError(f"Generated ZIP file/directory path collision: {raw_name}")

        seen_casefold.add(folded)
        files_casefold.add(folded)


def _copy_stream_to_snapshot(stream, snapshot) -> tuple[str, int]:
    """Hash and copy one bounded archive so parsing and extraction use immutable bytes."""
    position = stream.tell()
    digest = hashlib.sha256()
    copied = 0
    try:
        stream.seek(0)
        snapshot.seek(0)
        snapshot.truncate(0)
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            copied += len(chunk)
            if copied > MAX_ARCHIVE_BYTES:
                raise ValueError(
                    f"Original instance ZIP exceeds the compressed size limit ({MAX_ARCHIVE_BYTES} bytes)"
                )
            digest.update(chunk)
            snapshot.write(chunk)
        snapshot.flush()
        snapshot.seek(0)
        return digest.hexdigest(), copied
    finally:
        stream.seek(position)


@contextlib.contextmanager
def _open_snapshot_zip(path: pathlib.Path):
    """Keep a verified source descriptor open and consume a disk-backed immutable snapshot."""
    stream, metadata = _open_regular_file_no_follow(path, "Original instance ZIP")
    with contextlib.ExitStack() as stack:
        stack.enter_context(stream)
        if metadata.st_size > MAX_ARCHIVE_BYTES:
            raise ValueError(
                "Original instance ZIP exceeds the compressed size limit "
                f"({MAX_ARCHIVE_BYTES} bytes)"
            )
        snapshot = stack.enter_context(tempfile.TemporaryFile(mode="w+b"))
        archive_hash, archive_bytes = _copy_stream_to_snapshot(stream, snapshot)
        if archive_bytes != metadata.st_size:
            raise ValueError("Original instance ZIP size changed while creating its snapshot")
        snapshot.seek(0)
        preflight_zip_stream(snapshot, path)
        try:
            source = stack.enter_context(zipfile.ZipFile(snapshot))
        except zipfile.BadZipFile as error:
            raise ValueError(f"Missing or invalid original instance ZIP: {path}") from error
        yield source, stream, metadata, archive_hash, archive_bytes


def package(instance: pathlib.Path, binary: pathlib.Path, mods: pathlib.Path,
            target: str, horizon_commit: str, output: pathlib.Path) -> dict:
    if target not in ("windows-x86_64", "linux-x86_64"):
        raise ValueError(f"Unsupported native build target: {target!r}")
    if not instance.is_file():
        raise ValueError(f"Missing or invalid original instance ZIP: {instance}")
    if not binary.is_file():
        raise ValueError(f"Missing native client binary: {binary}")
    if not re.fullmatch(r"[0-9a-f]{40}", horizon_commit):
        raise ValueError("horizon_commit must be a full 40-character commit SHA")
    instance_id = f"horizon-native-{horizon_commit[:7]}"
    mod_files = []
    for candidate in sorted(mods.glob("*.hnmod")):
        try:
            metadata = candidate.lstat()
        except OSError as error:
            raise ValueError(f"Native mod input is unavailable: {candidate}") from error
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(
                "Native mod input must be a regular file "
                f"(symlinks and special files are refused): {candidate}"
            )
        mod_files.append(candidate)
    _validate_portable_basename(binary.name, "Native client")
    for mod in mod_files:
        _validate_portable_basename(mod.name, "Native mod")
    _reject_output_aliases(output, [instance, binary, *mod_files])

    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    os.close(descriptor)
    temporary = pathlib.Path(temporary_name)
    try:
        # Snapshot the exact input bytes once; hash and ZIP extraction both consume
        # that immutable snapshot, while the original descriptor remains open for
        # identity checks. This prevents same-size in-place rewrites from splitting
        # the manifest hash and packaged member contents.
        with _open_snapshot_zip(instance) as (source, instance_stream, instance_before, instance_hash, instance_bytes):
            members = list(safe_members(source))
            if not members:
                raise ValueError("Original instance ZIP is empty")
            instance_files = [(info, name) for info, name in members if not _member_is_directory(info)]
            if not instance_files:
                raise ValueError("Original instance ZIP contains no files")

            generated_paths = [
                f"instances/{instance_id}/{name}" for _, name in instance_files
            ]
            generated_paths.extend(
                [f"native-build/{target}/{binary.name}"]
                + [f"build-inputs/mods/{mod.name}" for mod in mod_files]
                + ["build-manifest.json", "README-INSTALL.txt"]
            )
            _validate_generated_zip_paths(generated_paths)

            with contextlib.ExitStack() as stack:
                binary_stream, binary_before = _open_regular_file_no_follow(
                    binary, "Native client input"
                )
                stack.enter_context(binary_stream)
                if not stat.S_ISREG(binary_before.st_mode) or binary_before.st_size == 0:
                    raise ValueError(f"Missing or invalid native client binary: {binary}")

                mod_inputs = []
                for mod in mod_files:
                    stream, metadata = _open_regular_file_no_follow(
                        mod, "Native mod input"
                    )
                    stack.enter_context(stream)
                    mod_inputs.append((mod, stream, metadata))

                manifest = {
                    "schema": 2,
                    "package": "Horizon drop-in instance",
                    "target": target,
                    "horizon_commit": horizon_commit,
                    "instance": {
                        "id": instance_id,
                        "relative_path": f"instances/{instance_id}",
                        "source_archive_sha256": instance_hash,
                        "source_archive_bytes": instance_bytes,
                        "files": len(instance_files),
                    },
                    "native_client": {
                        "path": f"native-build/{target}/{binary.name}",
                        "sha256": None,
                        "bytes": binary_before.st_size,
                        "imported_version_prefix": "imported-",
                    },
                    "mods": [],
                }
                instructions = f"""Horizon drop-in instance

Target: {target}
Horizon source: {horizon_commit}
Instance folder: instances/{instance_id}

INSTALL
1. Extract this package into Horizon's GAME HOME folder (the folder that already contains
   saves/options.txt for the default Horizon instance). Do not extract it into the launcher
   program folder. The result must be GAME_HOME/instances/{instance_id}/...
2. Import the included native client using the Horizon Launcher CLI:
   horizon-launcher import-build "native-build/{target}/{binary.name}" --trust-unsigned
3. Import and pin the exact imported build to this instance (PowerShell):
   $build = horizon-launcher --json import-build "native-build/{target}/{binary.name}" --trust-unsigned | ConvertFrom-Json
   horizon-launcher instance version "{instance_id}" $build.imported
   horizon-launcher instance select "{instance_id}"
4. Open Horizon Launcher. The instance should appear in the instance list.

The instance's game files are now a real Horizon instances/<id> directory; no ZIP restore/import
is required. The native build is installed in Horizon's separate version store and associated
with this instance using Horizon's own CLI. The CLI returns the real imported version tag
(imported-<date>-<binary-hash>); it must not be guessed from the Horizon source commit.

The original instance ZIP is unpacked without changing file contents. Its archive checksum,
the native executable checksum, and mod input checksums are recorded in build-manifest.json.
Native builds execute with your user permissions. Only import builds and mods you trust.
"""
                previous_mode = (
                    stat.S_IMODE(output.stat().st_mode)
                    if output.exists() and output.is_file()
                    else None
                )
                try:
                    with zipfile.ZipFile(
                        temporary, "w", compression=zipfile.ZIP_DEFLATED,
                        compresslevel=6, allowZip64=True
                    ) as archive:
                        for info, name in members:
                            if _member_is_directory(info):
                                continue
                            _copy_zip_member_to_zip(
                                archive, f"instances/{instance_id}/{name}", source, info
                            )

                        binary_hash, binary_bytes = _copy_stream_to_zip(
                            archive, manifest["native_client"]["path"],
                            binary_stream, binary_before
                        )
                        if _file_identity(binary_before) != _file_identity(
                            os.fstat(binary_stream.fileno())
                        ):
                            raise ValueError(f"Native client changed while packaging: {binary}")
                        manifest["native_client"]["sha256"] = binary_hash
                        manifest["native_client"]["bytes"] = binary_bytes

                        for mod, stream, before in mod_inputs:
                            mod_hash, mod_bytes = _copy_stream_to_zip(
                                archive, f"build-inputs/mods/{mod.name}", stream, before
                            )
                            if _file_identity(before) != _file_identity(os.fstat(stream.fileno())):
                                raise ValueError(f"Native mod changed while packaging: {mod}")
                            manifest["mods"].append({
                                "file": mod.name, "sha256": mod_hash, "bytes": mod_bytes
                            })

                        if _file_identity(instance_before) != _file_identity(
                            os.fstat(instance_stream.fileno())
                        ):
                            raise ValueError(f"Original instance ZIP changed while packaging: {instance}")

                        archive.writestr(
                            "build-manifest.json",
                            json.dumps(manifest, indent=2, sort_keys=True) + "\n"
                        )
                        archive.writestr("README-INSTALL.txt", instructions)

                    with zipfile.ZipFile(temporary) as archive:
                        bad = archive.testzip()
                        if bad:
                            raise ValueError(f"Packaged ZIP failed CRC verification at {bad}")
                        required = {
                            f"instances/{instance_id}/" + instance_files[0][1],
                            manifest["native_client"]["path"],
                            "build-manifest.json",
                            "README-INSTALL.txt",
                        }
                        missing = required.difference(archive.namelist())
                        if missing:
                            raise ValueError(f"Package is missing required entries: {sorted(missing)}")
                    # Keep existing output permissions when replacing a verified bundle.
                    if previous_mode is not None and os.name != "nt":
                        os.chmod(temporary, previous_mode)
                    os.replace(temporary, output)
                finally:
                    temporary.unlink(missing_ok=True)
        return manifest
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance", type=pathlib.Path, required=True)
    parser.add_argument("--binary", type=pathlib.Path, required=True)
    parser.add_argument("--mods", type=pathlib.Path, required=True)
    parser.add_argument("--target", required=True,
                        choices=("windows-x86_64", "linux-x86_64"))
    parser.add_argument("--horizon-commit", required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()
    result = package(args.instance, args.binary, args.mods, args.target,
                     args.horizon_commit, args.output)
    print(json.dumps({
        "status": "PACKAGED_DROP_IN_INSTANCE",
        "output": str(args.output),
        "target": args.target,
        "instance_id": result["instance"]["id"],
        "instance_files": result["instance"]["files"],
        "mod_count": len(result["mods"]),
        "instance_source_sha256": result["instance"]["source_archive_sha256"],
        "client_sha256": result["native_client"]["sha256"],
    }, indent=2))


if __name__ == "__main__":
    main()
