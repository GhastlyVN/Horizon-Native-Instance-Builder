#!/usr/bin/env python3
"""Verify that both native artifacts match the single validated Horizon revision."""
import argparse
import errno
import hashlib
import json
import os
import re
import stat
import struct
from contextlib import contextmanager
from pathlib import Path


_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


def _report(path: Path) -> dict[str, str]:
    try:
        text = _read_regular_text(path, "utf-8", 1024 * 1024, "build report")
    except FileNotFoundError as error:
        raise ValueError(f"missing build report: {path}") from error
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if ": " not in line:
            continue
        key, value = line.split(": ", 1)
        if key in fields:
            raise ValueError(f"duplicate build report field {key!r}: {path}")
        fields[key] = value
    return fields


def _file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    identity = (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
    )
    # Windows st_ctime is a creation timestamp, not a mutation timestamp.
    if os.name == "nt":
        return identity
    return (*identity, metadata.st_ctime_ns)


def _is_reparse_point(metadata: os.stat_result) -> bool:
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & reparse_attribute
    )


@contextmanager
def _open_regular_input(path: Path, description: str):
    """Yield one verified regular-file handle; never read through a swapped link."""
    path = Path(path)
    try:
        before = path.lstat()
    except FileNotFoundError:
        raise
    except OSError as error:
        raise ValueError(f"{description}: cannot inspect input path: {path}: {error}") from error
    if _is_reparse_point(before) or not stat.S_ISREG(before.st_mode):
        raise ValueError(f"{description}: input must be a regular non-symlink file (symlink or reparse-point refused): {path}")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        if getattr(error, "errno", None) in (errno.ELOOP, errno.ENXIO) or _is_reparse_point(before):
            raise ValueError(f"{description}: refusing symlink or unsafe input: {path}") from error
        raise ValueError(f"{description}: input could not be opened safely: {path}: {error}") from error

    try:
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            opened = os.fstat(stream.fileno())
            if (
                _is_reparse_point(opened)
                or not stat.S_ISREG(opened.st_mode)
                or _file_identity(before) != _file_identity(opened)
            ):
                raise ValueError(f"{description}: input path changed while it was being opened: {path}")
            try:
                after_open = path.lstat()
            except OSError as error:
                raise ValueError(
                    f"{description}: input path changed while it was being opened: {path}"
                ) from error
            if (
                _is_reparse_point(after_open)
                or not stat.S_ISREG(after_open.st_mode)
                or _file_identity(after_open) != _file_identity(opened)
            ):
                raise ValueError(f"{description}: input path changed while it was being opened: {path}")

            try:
                yield stream
            finally:
                after_read = os.fstat(stream.fileno())
                try:
                    path_after = path.lstat()
                except OSError as error:
                    raise ValueError(
                        f"{description}: input path changed during reading: {path}"
                    ) from error
                if (
                    _file_identity(opened) != _file_identity(after_read)
                    or _file_identity(after_read) != _file_identity(path_after)
                    or _is_reparse_point(path_after)
                    or not stat.S_ISREG(path_after.st_mode)
                ):
                    raise ValueError(f"{description}: input changed during reading: {path}")
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_regular_text(path: Path, encoding: str, limit: int, description: str) -> str:
    with _open_regular_input(path, description) as stream:
        raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ValueError(f"{description}: input exceeds {limit} bytes: {path}")
    try:
        return raw.decode(encoding)
    except UnicodeDecodeError as error:
        raise ValueError(f"{description}: input is not valid {encoding}: {path}") from error


def _is_regular_non_link(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(metadata.st_mode) and not _is_reparse_point(metadata)


def _inspect_binary(path: Path, target: str) -> tuple[int, str]:
    """Validate the executable header and hash one verified regular file handle."""
    with _open_regular_input(path, f"{target}: native client") as stream:
        size = os.fstat(stream.fileno()).st_size
        if size <= 0:
            raise ValueError(f"{target}: native client binary is empty")

        if target == "linux-x86_64":
            header = stream.read(20)
            if len(header) < 20 or header[:4] != b"\x7fELF":
                raise ValueError(f"{target}: native client is not an ELF executable")
            if header[4] != 2 or header[5] != 1:
                raise ValueError(f"{target}: expected a 64-bit little-endian ELF executable")
            machine = struct.unpack_from("<H", header, 18)[0]
            if machine != 62:
                raise ValueError(f"{target}: expected ELF x86_64 machine (62), got {machine}")
        elif target == "windows-x86_64":
            dos_header = stream.read(64)
            if len(dos_header) < 64 or dos_header[:2] != b"MZ":
                raise ValueError(f"{target}: native client is not a PE executable")
            pe_offset = struct.unpack_from("<I", dos_header, 0x3C)[0]
            if pe_offset < 64 or pe_offset + 6 > size:
                raise ValueError(f"{target}: invalid or truncated PE header offset")
            stream.seek(pe_offset)
            pe_header = stream.read(6)
            if len(pe_header) != 6 or pe_header[:4] != b"PE\x00\x00":
                raise ValueError(f"{target}: invalid PE signature")
            machine = struct.unpack_from("<H", pe_header, 4)[0]
            if machine != 0x8664:
                raise ValueError(
                    f"{target}: expected PE AMD64 machine (0x8664), got 0x{machine:04x}"
                )
        else:
            raise ValueError(f"unsupported native artifact target: {target}")

        stream.seek(0)
        digest = hashlib.sha256()
        bytes_read = 0
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            bytes_read += len(chunk)
            digest.update(chunk)
        if bytes_read != size:
            raise ValueError(f"{target}: native client changed during verification")

    return size, digest.hexdigest()


def _digest_regular_file(path: Path, description: str) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with _open_regular_input(path, description) as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _load_registry_selection(inputs: Path, pinned_commit: str) -> list[dict]:
    path = inputs / "build-report" / "registry-mods.json"
    try:
        text = _read_regular_text(path, "utf-8", 4 * 1024 * 1024, "registry mod report")
    except FileNotFoundError as error:
        raise ValueError(f"missing registry mod report: {path}") from error
    try:
        report = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"{path}: invalid registry mod report: {error}") from error
    if not isinstance(report, dict) or report.get("schema") != 1:
        raise ValueError(f"{path}: unsupported registry mod report schema")
    if str(report.get("horizon_commit", "")).lower() != pinned_commit:
        raise ValueError(f"{path}: registry mod report commit mismatch")
    selected = report.get("selected")
    if not isinstance(selected, list):
        raise ValueError(f"{path}: registry mod selection must be a list")
    for item in selected:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("file"), str)
            or not item["file"]
            or Path(item["file"]).name != item["file"]
            or not isinstance(item.get("sha256"), str)
            or len(item["sha256"]) != 64
            or not isinstance(item.get("size"), int)
            or isinstance(item["size"], bool)
            or item["size"] < 0
        ):
            raise ValueError(f"{path}: invalid registry mod selection entry")
    return selected


def verify_artifacts(inputs: Path, linux: Path, windows: Path) -> dict:    pinned_path = inputs / "build-report" / "horizon-commit.txt"
    try:
        pinned_commit = _read_regular_text(
            pinned_path, "ascii", 128, "validated Horizon commit"
        ).strip().lower()
    except FileNotFoundError as error:
        raise ValueError(f"missing validated Horizon commit: {pinned_path}") from error
    if not _COMMIT_RE.fullmatch(pinned_commit):
        raise ValueError("validated Horizon commit must be a full 40-character SHA-1")

    targets = (
        ("linux-x86_64", linux, "horizon-client"),
        ("windows-x86_64", windows, "horizon-client.exe"),
    )
    verified = {}
    for target, root, client_name in targets:
        report_path = root / "build-report" / target / "build.txt"
        fields = _report(report_path)
        if fields.get("Target") != target:
            raise ValueError(f"{target}: build report target mismatch")
        mod_inputs = [
            path for path in (inputs / "input" / "mods-latest").glob("*.hnmod")
            if _is_regular_non_link(path)
        ]
        if not mod_inputs:
            raise ValueError("no registry-verified native mod packages were supplied")
        if (
            fields.get("Optional native mods") != "built"
            or fields.get("Build mode") != "core+native-mods"
        ):
            raise ValueError(
                f"{target}: {len(mod_inputs)} .hnmod input(s) were supplied, "
                "but the artifact is not confirmed as a native-mod build"
            )
        actual_commit = fields.get("Horizon commit", "").lower()
        if actual_commit != pinned_commit:
            raise ValueError(
                f"{target}: Horizon commit mismatch; expected {pinned_commit}, got {actual_commit or '<missing>'}"
            )
        native_version = fields.get("Native version", "")
        if mod_inputs and not native_version.startswith(f"native-{pinned_commit[:7]}-"):
            raise ValueError(
                f"{target}: native build was not produced from the selected Nightly; "
                f"expected native-{pinned_commit[:7]}..., got {native_version or '<missing>'}"
            )
        binary = root / "native-runtime" / client_name
        if not binary.is_file():
            raise ValueError(f"{target}: missing native client binary: {binary}")
        size, digest = _inspect_binary(binary, target)
        verified[target] = {
            "client": client_name,
            "bytes": size,
            "sha256": digest,
            "horizon_commit": actual_commit,
        }
        core_name = "horizon-client-core.exe" if target == "windows-x86_64" else "horizon-client-core"
        core_binary = root / "native-runtime" / core_name
        if core_binary.is_file():
            core_size, core_digest = _inspect_binary(core_binary, target)
            verified[target]["core_client"] = {
                "client": core_name,
                "bytes": core_size,
                "sha256": core_digest,
            }

    return {
        "schema": 1,
        "status": "VERIFIED",
        "horizon_commit": pinned_commit,
        "targets": verified,
    }


def refuse_linked_output(path: Path) -> None:
    """Keep the verification report from being redirected or replaced by a link/directory."""
    if path.is_symlink():
        raise ValueError(f"Refusing to write through a symlink: {path}")
    if path.exists() and not path.is_file():
        raise ValueError(f"Refusing to write to a non-regular path: {path}")

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--linux", type=Path, required=True)
    parser.add_argument("--windows", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = verify_artifacts(args.inputs, args.linux, args.windows)
    # shortcut: the check and write are separate calls; upgrade to O_NOFOLLOW if outputs can be shared.
    refuse_linked_output(args.output)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        f"Verified Linux and Windows native artifacts from Horizon "
        f"{result['horizon_commit']}."
    )


if __name__ == "__main__":
    main()
