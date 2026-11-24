#!/usr/bin/env python3
"""Safely unpack a Minecraft game folder from the supplied instance ZIP."""
import argparse
import contextlib
import os
import pathlib
import stat
import tempfile
import unicodedata
import zipfile

try:
    from scripts.prepare import (
        MAX_ARCHIVE_BYTES,
        open_regular_zip_source,
        preflight_zip_stream,
        validate_zip_archive,
    )
except ModuleNotFoundError:  # direct execution: python scripts/extract_instance.py
    from prepare import (
        MAX_ARCHIVE_BYTES,
        open_regular_zip_source,
        preflight_zip_stream,
        validate_zip_archive,
    )

MAX_ARCHIVE_ENTRIES = 100_000
MAX_MEMBER_UNCOMPRESSED_BYTES = 2 * 1024**3
MAX_TOTAL_UNCOMPRESSED_BYTES = 8 * 1024**3
# Keep direct extraction's path policy identical to prepare.validate_zip_archive.
MAX_PATH_UTF8_BYTES = 4096
MAX_PATH_COMPONENTS = 64
MAX_COMPONENT_UTF8_BYTES = 255
MAX_COMPONENT_UTF16_UNITS = 255

WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL", "CLOCK$", "CONIN$", "CONOUT$",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
    "COM¹", "COM²", "COM³", "LPT¹", "LPT²", "LPT³",
}


def _portable_windows_component(part):
    """Match prepare.py's portable component limits and Windows alias checks."""
    if not part:
        return False
    try:
        utf8_bytes = len(part.encode("utf-8"))
        utf16_units = len(part.encode("utf-16-le")) // 2
    except UnicodeEncodeError:
        return False
    stem = part.split(".", 1)[0].rstrip(" .").upper()
    return (
        utf8_bytes <= MAX_COMPONENT_UTF8_BYTES
        and utf16_units <= MAX_COMPONENT_UTF16_UNITS
        and not part.endswith((" ", "."))
        and not any(ord(char) < 32 or char in '<>:"|?*' for char in part)
        and stem not in WINDOWS_RESERVED_NAMES
    )



def _metadata_is_link_or_reparse_point(metadata):
    """Recognize POSIX symlinks and Windows reparse points (including junctions)."""
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & 0x400
    )


def _is_link_or_reparse_point(path):
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    return _metadata_is_link_or_reparse_point(metadata)


def _collision_key(value):
    """Normalize canonical Unicode equivalents before case-insensitive comparison."""
    normalized = unicodedata.normalize("NFC", value)
    return unicodedata.normalize("NFC", normalized.casefold())


def find_minecraft_root(root: pathlib.Path, archive_files=None) -> pathlib.Path:
    # When extracting into an existing destination, never let an older Minecraft tree
    # satisfy validation for a new archive that contains no version metadata.
    if archive_files is not None:
        version_counts = {}
        for name in archive_files:
            parts = pathlib.PurePosixPath(name).parts
            if (
                len(parts) >= 3
                and parts[-3] == "versions"
                and parts[-1] == parts[-2] + ".json"
            ):
                relative_root = pathlib.PurePosixPath(*parts[:-3])
                version_counts[relative_root] = version_counts.get(relative_root, 0) + 1
        if not version_counts:
            raise SystemExit(
                "The instance ZIP does not contain a Minecraft versions/<id>/<id>.json tree. "
                "Export a complete game directory (with its versions folder), not only a modpack manifest."
            )
        ranked = sorted(version_counts.items(), key=lambda item: (item[1], str(item[0])), reverse=True)
        if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
            raise SystemExit("The ZIP contains multiple equally plausible Minecraft roots; export one instance per ZIP.")
        relative_root = ranked[0][0]
        return root if str(relative_root) == "." else root.joinpath(*relative_root.parts)

    candidates = set()
    for metadata in root.rglob("*.json"):
        if metadata.parent.parent.name == "versions" and metadata.stem == metadata.parent.name:
            candidates.add(metadata.parent.parent.parent)
    if not candidates:
        raise SystemExit(
            "The instance ZIP does not contain a Minecraft versions/<id>/<id>.json tree. "
            "Export a complete game directory (with its versions folder), not only a modpack manifest."
        )
    ranked = sorted(candidates, key=lambda p: (len(list((p / "versions").glob("*/*.json"))), str(p)), reverse=True)
    if len(ranked) > 1:
        top_count = len(list((ranked[0] / "versions").glob("*/*.json")))
        second_count = len(list((ranked[1] / "versions").glob("*/*.json")))
        if top_count == second_count:
            raise SystemExit("The ZIP contains multiple equally plausible Minecraft roots; export one instance per ZIP.")
    return ranked[0]

def _validate_existing_destination_collisions(root, validated_entries):
    """Reject case-fold aliases and file/directory mismatches with existing paths."""
    expected_children = {}
    expected_types = {}
    for _, path, is_directory in validated_entries:
        for index, part in enumerate(path.parts):
            parent_key = tuple(_collision_key(value) for value in path.parts[:index])
            child_key = _collision_key(part)
            expected_children.setdefault(parent_key, {})[child_key] = part
            expected_types[(parent_key, child_key)] = (
                "directory" if index < len(path.parts) - 1 or is_directory else "file"
            )

    pending = [(root, ())]
    while pending:
        directory, parent_key = pending.pop()
        try:
            existing_children = list(directory.iterdir())
        except OSError as error:
            raise SystemExit(f"Could not inspect existing extraction directory {directory}: {error}") from error
        for existing in existing_children:
            child_key = _collision_key(existing.name)
            expected_name = expected_children.get(parent_key, {}).get(child_key)
            if expected_name is None:
                continue
            if existing.name != expected_name:
                raise SystemExit(
                    "Case-insensitive destination path collision: "
                    f"archive uses {expected_name!r}, destination already contains {existing.name!r}"
                )
            if _is_link_or_reparse_point(existing):
                raise SystemExit(
                    f"Existing symlink or reparse point in extraction path refused: {existing}"
                )
            expected_type = expected_types[(parent_key, child_key)]
            if expected_type == "directory":
                if not existing.is_dir():
                    raise SystemExit(
                        f"Destination file/directory path collision: expected directory at {existing}"
                    )
                pending.append((existing, parent_key + (child_key,)))
            elif existing.is_dir():
                raise SystemExit(
                    f"Destination file/directory path collision: expected file at {existing}"
                )


@contextlib.contextmanager
def _open_bounded_zip(archive):
    """Validate and extract one immutable snapshot of a regular, no-follow ZIP source."""
    archive = pathlib.Path(archive)
    with contextlib.ExitStack() as stack:
        try:
            source = stack.enter_context(open_regular_zip_source(archive))
        except ValueError as error:
            raise SystemExit(f"Invalid ZIP archive {archive}: {error}") from error

        # Parsing a path-backed ZIP and extracting it later from the same open handle still
        # leaves a same-size/same-mtime mutation window on Windows. Snapshot the bounded source
        # first, then run the shared validator and every member read against those exact bytes.
        before = os.fstat(source.fileno())
        source_identity = _file_identity(before)
        if before.st_size > MAX_ARCHIVE_BYTES:
            raise SystemExit(
                f"ZIP archive exceeds the compressed size limit ({MAX_ARCHIVE_BYTES} bytes)"
            )
        snapshot = stack.enter_context(tempfile.TemporaryFile(mode="w+b"))
        copied = 0
        try:
            source.seek(0)
            while chunk := source.read(1024 * 1024):
                copied += len(chunk)
                if copied > MAX_ARCHIVE_BYTES:
                    raise SystemExit(
                        f"ZIP archive exceeds the compressed size limit ({MAX_ARCHIVE_BYTES} bytes)"
                    )
                snapshot.write(chunk)
            snapshot.flush()
            snapshot.seek(0)
            after = os.fstat(source.fileno())
            path_after = archive.lstat()
        except OSError as error:
            raise SystemExit(f"Invalid ZIP archive {archive}: {error}") from error
        if (
            _file_identity(after) != source_identity
            or _metadata_is_link_or_reparse_point(path_after)
            or not stat.S_ISREG(path_after.st_mode)
            or _file_identity(path_after) != source_identity
        ):
            raise SystemExit(f"ZIP archive changed while creating its extraction snapshot: {archive}")

        try:
            preflight_zip_stream(snapshot, archive)
            zf = zipfile.ZipFile(snapshot)
        except (OSError, zipfile.BadZipFile, ValueError) as error:
            raise SystemExit(f"Invalid ZIP archive {archive}: {error}") from error
        with zf:
            try:
                validate_zip_archive(zf)
            except ValueError as error:
                raise SystemExit(str(error)) from error
            # Keep member-read failures outside the parse-time conversion so the
            # extraction rollback handler receives the original exception.
            yield zf


def _file_identity(metadata):
    """Identity tuple used to detect in-place archive mutation during extraction."""
    identity = (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)
    if os.name != "nt":
        return (*identity, metadata.st_ctime_ns)
    return identity


def extract_safe(archive: pathlib.Path, destination: pathlib.Path) -> pathlib.Path:
    # Path.is_symlink() misses Windows junctions. Reject reparse points before resolve()
    # can silently redirect the caller-selected destination.
    if _is_link_or_reparse_point(destination):
        raise SystemExit(
            f"symlink extraction destination refused (including Windows reparse points): {destination}"
        )
    # Resolve without creating the destination. An invalid archive must not leave
    # even an empty output directory behind.
    root = destination.resolve()
    seen = set()
    seen_casefold = set()
    files_casefold = set()
    prefix_spellings = {}
    validated_entries = []

    # Validate the complete archive before writing anything. This prevents a late malicious
    # entry from leaving a partially extracted tree behind.
    with _open_bounded_zip(archive) as zf:
        archive_identity = _file_identity(os.fstat(zf.fp.fileno()))
        entries = zf.infolist()
        if not entries:
            raise SystemExit("Empty ZIP archive")
        if len(entries) > MAX_ARCHIVE_ENTRIES:
            raise SystemExit(
                f"ZIP archive has too many entries: {len(entries)} "
                f"(limit {MAX_ARCHIVE_ENTRIES})"
            )
        total_uncompressed = 0
        for info in entries:
            raw_name = info.filename.replace(chr(92), "/")
            is_directory = info.is_dir() or raw_name.endswith("/")
            if info.flag_bits & 0x1:
                raise SystemExit(f"Encrypted ZIP entry refused: {info.filename}")
            if not is_directory:
                if info.file_size < 0 or info.file_size > MAX_MEMBER_UNCOMPRESSED_BYTES:
                    raise SystemExit(
                        f"ZIP member exceeds the uncompressed size limit: {info.filename}"
                    )
                total_uncompressed += info.file_size
                if total_uncompressed > MAX_TOTAL_UNCOMPRESSED_BYTES:
                    raise SystemExit(
                        "ZIP archive exceeds the total uncompressed size limit "
                        f"({MAX_TOTAL_UNCOMPRESSED_BYTES} bytes)"
                    )
            path = pathlib.PurePosixPath(raw_name)
            try:
                path_utf8_bytes = len(raw_name.encode("utf-8"))
            except UnicodeEncodeError as error:
                raise SystemExit(f"ZIP path is not valid Unicode: {info.filename}") from error
            if path_utf8_bytes > MAX_PATH_UTF8_BYTES or len(path.parts) > MAX_PATH_COMPONENTS:
                raise SystemExit(f"ZIP path exceeds portable path limits: {info.filename}")
            if (
                not path.parts
                or raw_name.startswith("/")
                or chr(0) in raw_name
                or ".." in path.parts
                or any(not _portable_windows_component(part) for part in path.parts)
            ):
                raise SystemExit(f"Unsafe ZIP path: {info.filename}")
            normalized = "/".join(path.parts)
            if normalized in seen:
                raise SystemExit(f"Duplicate ZIP path after normalization: {info.filename}")
            folded = _collision_key(normalized)
            if folded in seen_casefold:
                raise SystemExit(f"Case-insensitive ZIP path collision: {info.filename}")
            parents = ["/".join(path.parts[:index]) for index in range(1, len(path.parts))]
            conflicting_parent = next(
                (_collision_key(parent) for parent in parents if _collision_key(parent) in files_casefold),
                None,
            )
            if conflicting_parent is not None:
                raise SystemExit(
                    f"ZIP file/directory path collision: {info.filename} is below file {conflicting_parent}"
                )
            # Keep every directory prefix's spelling stable so Windows and POSIX do not
            # materialize different directory trees from the same archive.
            prefix_count = len(path.parts) if is_directory else len(path.parts) - 1
            for index in range(1, prefix_count + 1):
                prefix = "/".join(path.parts[:index])
                folded_prefix = _collision_key(prefix)
                previous_prefix = prefix_spellings.get(folded_prefix)
                if previous_prefix is not None and previous_prefix != prefix:
                    raise SystemExit(
                        f"Case-insensitive ZIP directory prefix collision: {info.filename}"
                    )
                prefix_spellings[folded_prefix] = prefix
            # Prior entries register every directory prefix in prefix_spellings.
            # A file cannot replace a path already used by a descendant; O(1) lookup
            # avoids a quadratic scan for archives with many sibling files.
            if not is_directory and folded in prefix_spellings:
                raise SystemExit(
                    f"ZIP file/directory path collision: {info.filename} replaces a directory"
                )
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise SystemExit(f"Symlink entry refused: {info.filename}")
            if mode not in (0, 0o100000, 0o040000):
                raise SystemExit(f"Unsupported special ZIP entry refused: {info.filename}")
            if mode == 0o040000 and not is_directory:
                raise SystemExit(f"ZIP directory mode/name mismatch: {info.filename}")
            if mode == 0o100000 and is_directory:
                raise SystemExit(f"ZIP file mode/name mismatch: {info.filename}")
            target = root.joinpath(*path.parts)
            if not target.resolve().is_relative_to(root):
                raise SystemExit(f"ZIP entry escapes extraction directory: {info.filename}")
            seen.add(normalized)
            seen_casefold.add(folded)
            if not is_directory:
                files_casefold.add(folded)
            validated_entries.append((info, path, is_directory))

        # Only create the output directory after every archive entry has passed preflight.
        created_files = []
        created_directories = []

        def ensure_directory(path, *, stop=None):
            path = pathlib.Path(path).absolute()
            stop = pathlib.Path(stop).absolute() if stop is not None else None
            if stop is not None and not path.is_relative_to(stop):
                raise SystemExit(f"Extraction path escapes destination root: {path}")

            missing = []
            current = path
            while True:
                try:
                    metadata = current.lstat()
                except FileNotFoundError:
                    missing.append(current)
                else:
                    if _metadata_is_link_or_reparse_point(metadata):
                        raise SystemExit(
                            f"Existing symlink or reparse point in extraction path refused: {current}"
                        )
                    if not stat.S_ISDIR(metadata.st_mode):
                        raise SystemExit(f"Extraction path component is not a directory: {current}")

                if stop is not None and current == stop:
                    break
                parent = current.parent
                if parent == current:
                    break
                current = parent

            for directory in reversed(missing):
                try:
                    directory.mkdir()
                except FileExistsError:
                    # A concurrent creator won the race. Validate its directory,
                    # but never register it for this invocation's rollback.
                    metadata = directory.lstat()
                    if (
                        _metadata_is_link_or_reparse_point(metadata)
                        or not stat.S_ISDIR(metadata.st_mode)
                    ):
                        raise SystemExit(
                            f"Unsafe concurrent extraction directory: {directory}"
                        )
                else:
                    created_directories.append(directory)

            # Descriptor-relative writes make the final checks and the open one operation:
            # a checked parent directory can be renamed after lstat() but the open below still
            # resolves through the descriptor opened here, not through the mutable path.
            use_directory_descriptors = (
                os.name == "posix"
                and hasattr(os, "O_NOFOLLOW")
                and hasattr(os, "O_DIRECTORY")
            )
            if use_directory_descriptors:
                directory_open_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                member_open_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            else:
                directory_open_flags = 0
                member_open_flags = 0

            @contextlib.contextmanager
            def open_parent_directory(parts):
                descriptors = []
                try:
                    try:
                        descriptor = os.open(root, directory_open_flags)
                    except OSError as error:
                        raise SystemExit(
                            f"Extraction root changed while opening it: {root}"
                        ) from error
                    descriptors.append(descriptor)
                    for index, part in enumerate(parts[:-1], 1):
                        directory = root.joinpath(*parts[:index])
                        try:
                            child = os.open(part, directory_open_flags, dir_fd=descriptor)
                        except FileNotFoundError:
                            try:
                                os.mkdir(part, dir_fd=descriptor)
                            except FileExistsError:
                                pass
                            else:
                                created_directories.append(directory)
                            try:
                                child = os.open(part, directory_open_flags, dir_fd=descriptor)
                            except OSError as error:
                                raise SystemExit(
                                    f"Existing symlink or reparse point in extraction path refused: {directory}"
                                ) from error
                        except OSError as error:
                            raise SystemExit(
                                f"Existing symlink or reparse point in extraction path refused: {directory}"
                            ) from error
                        descriptors.append(child)
                        descriptor = child
                    yield descriptor
                finally:
                    for descriptor in reversed(descriptors):
                        os.close(descriptor)

            @contextlib.contextmanager
            def open_member(parts, target):
                if not use_directory_descriptors:
                    ensure_directory(target.parent, stop=root)
                    with target.open("xb") as output:
                        created_files.append(target)
                        yield output
                    return
                with open_parent_directory(parts) as parent_descriptor:
                    try:
                        descriptor = os.open(
                            parts[-1], member_open_flags, 0o666, dir_fd=parent_descriptor
                        )
                    except FileExistsError as error:
                        raise SystemExit(f"Extraction destination already exists: {target}") from error
                    except OSError as error:
                        raise SystemExit(
                            f"Existing symlink or reparse point in extraction path refused: {target}"
                        ) from error
                    with os.fdopen(descriptor, "wb") as output:
                        created_files.append(target)
                        yield output

        try:
            # Create components one at a time so rollback owns only successful mkdir calls.
            # Use the already-resolved destination so intentional parent symlinks
            # (for example macOS /tmp) cannot redirect later path-based writes.
            ensure_directory(root)
            if _is_link_or_reparse_point(root):
                raise SystemExit(
                    f"symlink extraction destination refused (including Windows reparse points): {root}"
                )

            _validate_existing_destination_collisions(root, validated_entries)
            actual_total_uncompressed = 0
            for info, path, is_directory in validated_entries:
                target = root.joinpath(*path.parts)
                current = root
                for part in path.parts:
                    current = current / part
                    # Recheck every existing component immediately before writing. This
                    # includes junctions and other Windows reparse points, not just symlinks.
                    if _is_link_or_reparse_point(current):
                        raise SystemExit(
                            f"Existing symlink or reparse point in extraction path refused: {current}"
                        )
                if is_directory:
                    if use_directory_descriptors:
                        with open_parent_directory((*path.parts, "")):
                            pass
                    else:
                        ensure_directory(target, stop=root)
                    continue
                actual_member_uncompressed = 0
                with zf.open(info) as source:
                    with open_member(path.parts, target) as output:
                        while chunk := source.read(1024 * 1024):
                            actual_member_uncompressed += len(chunk)
                            actual_total_uncompressed += len(chunk)
                            if actual_member_uncompressed > MAX_MEMBER_UNCOMPRESSED_BYTES:
                                raise SystemExit(
                                    f"Actual ZIP member exceeds the uncompressed size limit: {info.filename}"
                                )
                            if actual_member_uncompressed > info.file_size:
                                raise SystemExit(
                                    f"ZIP member expands beyond its declared size: {info.filename}"
                                )
                            if actual_total_uncompressed > MAX_TOTAL_UNCOMPRESSED_BYTES:
                                raise SystemExit(
                                    "Actual ZIP archive exceeds the total uncompressed size limit "
                                    f"({MAX_TOTAL_UNCOMPRESSED_BYTES} bytes)"
                                )
                            output.write(chunk)
                if actual_member_uncompressed != info.file_size:
                    raise SystemExit(
                        f"ZIP member size does not match its declared size: {info.filename}"
                    )

            archive_files = [path.as_posix() for _, path, is_directory in validated_entries if not is_directory]
            game_root = find_minecraft_root(root, archive_files)
            if _file_identity(os.fstat(zf.fp.fileno())) != archive_identity:
                raise SystemExit(f"Validated ZIP snapshot changed during extraction: {archive}")
            return game_root
        except BaseException:
            # Extraction is exclusive: only remove files and directories created by this
            # invocation, never pre-existing destination content.
            for created in reversed(created_files):
                created.unlink(missing_ok=True)
            for created in reversed(created_directories):
                try:
                    created.rmdir()
                except OSError:
                    pass
            raise

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=pathlib.Path)
    parser.add_argument("--destination", type=pathlib.Path, required=True)
    parser.add_argument("--print-root", action="store_true")
    args = parser.parse_args()
    game_root = extract_safe(args.archive, args.destination)
    if args.print_root:
        print(game_root)
    else:
        print(f"Minecraft game files found at: {game_root}")

if __name__ == "__main__":
    main()