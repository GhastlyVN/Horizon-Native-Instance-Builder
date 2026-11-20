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
    from .prepare import validate_zip_archive
else:  # Executed as `python scripts/package_instance.py`.
    from prepare import validate_zip_archive


def _file_identity(metadata):
    identity = (
        metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns
    )
    if os.name != "nt":
        return (*identity, metadata.st_ctime_ns)
    return identity


def _sha256_stream(stream) -> str:
    """Hash one already-open input without resolving its path a second time."""
    position = stream.tell()
    digest = hashlib.sha256()
    stream.seek(0)
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    stream.seek(position)
    return digest.hexdigest()


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


def package(instance: pathlib.Path, binary: pathlib.Path, mods: pathlib.Path,
            target: str, horizon_commit: str, output: pathlib.Path) -> dict:
    if not instance.is_file():
        raise ValueError(f"Missing or invalid original instance ZIP: {instance}")
    if not binary.is_file():
        raise ValueError(f"Missing native client binary: {binary}")
    if not re.fullmatch(r"[0-9a-f]{40}", horizon_commit):
        raise ValueError("horizon_commit must be a full 40-character commit SHA")
    instance_id = f"horizon-native-{horizon_commit[:7]}"
    mod_files = sorted(p for p in mods.glob("*.hnmod") if p.is_file())
    _reject_output_aliases(output, [instance, binary, *mod_files])

    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    os.close(descriptor)
    temporary = pathlib.Path(temporary_name)
    try:
        # Keep the validated instance archive open through extraction and hash the
        # same file descriptor. Reopening the path after preflight permits a swap
        # between validation, manifest hashing and packaging.
        with instance.open("rb") as instance_stream:
            instance_before = os.fstat(instance_stream.fileno())
            if not stat.S_ISREG(instance_before.st_mode):
                raise ValueError(f"Original instance ZIP is not a regular file: {instance}")
            try:
                source = zipfile.ZipFile(instance_stream)
            except zipfile.BadZipFile as error:
                raise ValueError(f"Missing or invalid original instance ZIP: {instance}") from error
            with source:
                members = list(safe_members(source))
                if not members:
                    raise ValueError("Original instance ZIP is empty")
                instance_files = [(info, name) for info, name in members if not info.is_dir()]
                if not instance_files:
                    raise ValueError("Original instance ZIP contains no files")
                instance_hash = _sha256_stream(instance_stream)

                with contextlib.ExitStack() as stack:
                    binary_stream = stack.enter_context(binary.open("rb"))
                    binary_before = os.fstat(binary_stream.fileno())
                    if not stat.S_ISREG(binary_before.st_mode) or binary_before.st_size == 0:
                        raise ValueError(f"Missing or invalid native client binary: {binary}")

                    mod_inputs = []
                    for mod in mod_files:
                        stream = stack.enter_context(mod.open("rb"))
                        metadata = os.fstat(stream.fileno())
                        if not stat.S_ISREG(metadata.st_mode):
                            raise ValueError(f"Native mod input is not a regular file: {mod}")
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
                            "source_archive_bytes": instance_before.st_size,
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
                                if info.is_dir():
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
