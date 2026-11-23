#!/usr/bin/env python3
"""Download the newest registry-reviewed .hnmods that passed Horizon's latest-Nightly health check."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import pathlib
import re
import shutil
import tempfile
import tomllib
import urllib.parse
import zipfile

try:
    from scripts.github_api import open_url
except ModuleNotFoundError:  # direct execution: python scripts/download_registry_mods.py
    from github_api import open_url

try:
    from scripts.prepare import preflight_zip_stream, validate_zip_archive
except ModuleNotFoundError:  # direct execution: python scripts/download_registry_mods.py
    from prepare import preflight_zip_stream, validate_zip_archive

INDEX_URL = "https://horizon-rust.github.io/horizon-mods/index.json"
HEALTH_URL = "https://github.com/horizon-rust/horizon-mods/releases/download/native-health/native-health.json"
HEALTH_RELEASE_API = "https://api.github.com/repos/horizon-rust/horizon-mods/releases/tags/native-health"
DEFAULT_IDS = (
    "appleskin",
    "hud_plus",
    "fast_items",
    "freecam",
    "fullbright_horizon",
    "jade",
    "jei",
    "zoom",
    "enchantmenthints",
    "inventory_profiles",
)
SHA256 = re.compile(r"^[0-9a-f]{64}$")
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_BUNDLE_MEMBER_BYTES = 64 * 1024 * 1024
MAX_BUNDLE_TOTAL_BYTES = 256 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_MIXIN_BYTES = 1024 * 1024
CHUNK_BYTES = 1024 * 1024

APPROVED_DOWNLOAD_HOSTS = frozenset({
    "api.github.com",
    "github.com",
    "horizon-rust.github.io",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
})


def _validate_download_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in APPROVED_DOWNLOAD_HOSTS:
        raise ValueError(f"Refusing unapproved download URL: {url}")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"Refusing download URL with credentials: {url}")
    if parsed.port is not None and parsed.port != 443:
        raise ValueError(f"Refusing download URL with non-standard port: {url}")
    return url


def fetch_bytes(url: str, max_bytes: int = MAX_FILE_BYTES) -> bytes:
    _validate_download_url(url)
    with open_url(
        url,
        timeout=180,
        accept="application/json, application/octet-stream",
    ) as response:
        _validate_download_url(response.geturl())
        length = response.headers.get("Content-Length")
        if length and int(length) > max_bytes:
            raise ValueError(f"Download exceeds the {max_bytes}-byte safety limit: {url}")
        data = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError(f"Download exceeds the {max_bytes}-byte safety limit: {url}")
        return data


def fetch_json(url: str) -> dict:
    try:
        value = json.loads(fetch_bytes(url, max_bytes=MAX_FILE_BYTES))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"Invalid JSON from {url}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object from {url}")
    return value


def choose_versions(index: dict, health: dict, health_updated_at: str,
                    ids: tuple[str, ...], game: str, horizon_commit: str) -> tuple[list[dict], list[dict]]:
    if health.get("horizon") != horizon_commit:
        raise ValueError(
            "The official native-health report is for Horizon "
            f"{health.get('horizon')!r}, not the selected Nightly {horizon_commit!r}. "
            "Refusing to guess compatibility."
        )
    if not re.fullmatch(r"[0-9a-f]{40}", horizon_commit):
        raise ValueError("Horizon commit must be a full 40-character SHA-1.")
    health_date = health_updated_at[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", health_date):
        raise ValueError("The native-health release has no valid updated_at date.")

    projects = {p.get("id"): p for p in index.get("projects", []) if isinstance(p, dict)}
    revoked = index.get("revoked", [])
    revoked_ids = {r.get("id") for r in revoked if isinstance(r, dict) and not r.get("sha256")}
    revoked_hashes = {r.get("sha256") for r in revoked if isinstance(r, dict) and r.get("sha256")}
    broken = health.get("broken", {})
    selected: list[dict] = []
    skipped: list[dict] = []

    for mod_id in ids:
        project = projects.get(mod_id)
        if not project or project.get("kind") != "native-mod":
            raise ValueError(f"Registry project {mod_id!r} is missing or is not a native mod.")
        if mod_id in revoked_ids:
            skipped.append({"id": mod_id, "reason": "project revoked by the Horizon registry"})
            continue
        versions = project.get("versions")
        if not isinstance(versions, list) or not versions:
            raise ValueError(f"Registry project {mod_id!r} has no published versions.")
        version = versions[0]  # The registry publishes newest first.
        if version.get("game") != game:
            skipped.append({
                "id": mod_id,
                "version": version.get("version"),
                "reason": f"latest release targets game {version.get('game')!r}, not {game}",
            })
            continue
        published = version.get("published")
        if not isinstance(published, str) or published >= health_date:
            raise ValueError(
                f"{mod_id}: latest version {version.get('version')} was published on "
                f"{published!r}, not demonstrably before the health report ({health_date})."
            )
        file_info = version.get("file")
        if not isinstance(file_info, dict):
            raise ValueError(f"{mod_id}: registry index has no structured file metadata.")
        digest = file_info.get("sha256")
        if not isinstance(digest, str) or not SHA256.fullmatch(digest):
            raise ValueError(f"{mod_id}: registry SHA-256 is missing or malformed.")
        if digest in revoked_hashes:
            skipped.append({"id": mod_id, "version": version.get("version"), "reason": "file revoked"})
            continue

        failure = broken.get(mod_id) if isinstance(broken, dict) else None
        if failure:
            if failure.get("version") != version.get("version"):
                raise ValueError(
                    f"{mod_id}: health report checked version {failure.get('version')!r}, "
                    f"but registry now offers {version.get('version')!r}."
                )
            skipped.append({
                "id": mod_id,
                "version": version.get("version"),
                "reason": f"official Nightly health check: {failure.get('code', 'broken')} "
                          f"({failure.get('stage', 'unknown')})",
                "detail": failure.get("message", ""),
            })
            continue

        url = file_info.get("url")
        parsed = urllib.parse.urlparse(url or "")
        name = file_info.get("name") or urllib.parse.unquote(pathlib.PurePosixPath(parsed.path).name)
        size = file_info.get("size")
        if parsed.scheme != "https" or parsed.hostname != "github.com":
            raise ValueError(f"{mod_id}: registry file URL is not an approved HTTPS GitHub URL.")
        if not isinstance(name, str) or not name.endswith(".hnmod") or pathlib.PurePosixPath(name).name != name:
            raise ValueError(f"{mod_id}: invalid .hnmod filename in registry.")
        if size is not None and (not isinstance(size, int) or size <= 0 or size > MAX_FILE_BYTES):
            raise ValueError(f"{mod_id}: invalid or oversized registry file.")
        selected.append({
            "id": mod_id,
            "version": version.get("version"),
            "game": game,
            "published": published,
            "file": name,
            "url": url,
            "size": size,
            "sha256": digest,
        })
    return selected, skipped


def validate_bundle(data: bytes, expected: dict) -> None:
    """Validate a downloaded .hnmod with the shared path policy and bounded CRC streaming."""
    mod_id = expected["id"]
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f"{mod_id}: archive exceeds the {MAX_FILE_BYTES}-byte safety limit.")
    if expected.get("size") is not None and len(data) != expected["size"]:
        raise ValueError(
            f"{mod_id}: file size mismatch; expected {expected['size']}, got {len(data)}."
        )
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected["sha256"]:
        raise ValueError(
            f"{mod_id}: SHA-256 mismatch; expected {expected['sha256']}, got {actual}."
        )

    stream = io.BytesIO(data)
    try:
        preflight_zip_stream(stream, f"{mod_id}.hnmod")
        stream.seek(0)
        with zipfile.ZipFile(stream) as archive:
            _, names = validate_zip_archive(archive)
            infos = archive.infolist()
            if len(infos) != len(names):
                raise ValueError(f"{mod_id}: ZIP entries changed during validation.")
            members = dict(zip(names, infos))

            def is_directory(info: zipfile.ZipInfo) -> bool:
                return info.is_dir() or info.filename.replace(chr(92), "/").endswith("/")

            manifests = [
                name for name, info in members.items()
                if not is_directory(info)
                and pathlib.PurePosixPath(name).name == "horizon-native.json"
            ]
            mixins = [
                name for name, info in members.items()
                if not is_directory(info)
                and pathlib.PurePosixPath(name).name == "horizon-mixin.toml"
            ]
            if len(manifests) != 1 or len(mixins) != 1:
                raise ValueError(
                    f"{mod_id}: expected one native manifest and one mixin manifest."
                )

            manifest_name, mixin_name = manifests[0], mixins[0]
            captured = {
                manifest_name: bytearray(),
                mixin_name: bytearray(),
            }
            metadata_limits = {
                manifest_name: MAX_MANIFEST_BYTES,
                mixin_name: MAX_MIXIN_BYTES,
            }
            total_declared = 0
            for name, info in members.items():
                if is_directory(info):
                    continue
                if info.file_size < 0 or info.file_size > MAX_BUNDLE_MEMBER_BYTES:
                    raise ValueError(
                        f"{mod_id}: ZIP member exceeds the uncompressed size limit: {name}"
                    )
                total_declared += info.file_size
                if total_declared > MAX_BUNDLE_TOTAL_BYTES:
                    raise ValueError(
                        f"{mod_id}: bundle exceeds the total uncompressed size limit "
                        f"({MAX_BUNDLE_TOTAL_BYTES} bytes)."
                    )
                if name in metadata_limits and info.file_size > metadata_limits[name]:
                    raise ValueError(
                        f"{mod_id}: metadata file exceeds its size limit: {name}"
                    )

            total_actual = 0
            for name, info in members.items():
                if is_directory(info):
                    continue
                member_bytes = 0
                with archive.open(info) as member:
                    while True:
                        chunk = member.read(CHUNK_BYTES)
                        if not chunk:
                            break
                        member_bytes += len(chunk)
                        total_actual += len(chunk)
                        if member_bytes > MAX_BUNDLE_MEMBER_BYTES:
                            raise ValueError(
                                f"{mod_id}: ZIP member expanded beyond the size limit: {name}"
                            )
                        if total_actual > MAX_BUNDLE_TOTAL_BYTES:
                            raise ValueError(
                                f"{mod_id}: bundle expanded beyond the total size limit "
                                f"({MAX_BUNDLE_TOTAL_BYTES} bytes)."
                            )
                        if name in captured:
                            captured[name].extend(chunk)
                if member_bytes != info.file_size:
                    raise ValueError(
                        f"{mod_id}: ZIP member size does not match its declaration: {name}"
                    )

            try:
                manifest = json.loads(bytes(captured[manifest_name]).decode("utf-8"))
                tomllib.loads(bytes(captured[mixin_name]).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError, tomllib.TOMLDecodeError) as error:
                raise ValueError(f"{mod_id}: invalid native manifest or mixin config: {error}") from error
            if not isinstance(manifest, dict):
                raise ValueError(f"{mod_id}: native manifest must be a JSON object.")
            if (
                manifest.get("id") != expected["id"]
                or manifest.get("version") != expected["version"]
                or manifest.get("game") != expected["game"]
            ):
                raise ValueError(f"{mod_id}: archive manifest does not match registry metadata.")
    except zipfile.BadZipFile as error:
        raise ValueError(f"{mod_id}: invalid ZIP archive: {error}") from error


def download_registry_mods(output: pathlib.Path, report_path: pathlib.Path,
                           ids: tuple[str, ...], game: str, horizon_commit: str,
                           index_url: str = INDEX_URL, health_url: str = HEALTH_URL,
                           health_release_api: str = HEALTH_RELEASE_API) -> dict:
    index = fetch_json(index_url)
    health = fetch_json(health_url)
    release = fetch_json(health_release_api)
    updated_at = release.get("updated_at", "")
    selected, skipped = choose_versions(index, health, updated_at, ids, game, horizon_commit)

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="horizon-registry-mods-", dir=output.parent) as temp:
        staging = pathlib.Path(temp)
        downloaded = []
        for item in selected:
            data = fetch_bytes(item["url"], max_bytes=MAX_FILE_BYTES)
            validate_bundle(data, item)
            (staging / item["file"]).write_bytes(data)
            downloaded.append({
                "id": item["id"],
                "version": item["version"],
                "game": item["game"],
                "file": item["file"],
                "size": len(data),
                "sha256": item["sha256"],
                "url": item["url"],
            })
        if not downloaded:
            raise ValueError("No compatible registry native mods remain; refusing to publish a core-only build.")
        if output.exists():
            shutil.rmtree(output)
        staging.rename(output)

    result = {
        "schema": 1,
        "source": "Horizon official mod registry",
        "horizon_commit": horizon_commit,
        "health_report_updated_at": updated_at,
        "game": game,
        "selected": downloaded,
        "skipped": skipped,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "horizon_commit": horizon_commit,
        "selected": [{"id": item["id"], "version": item["version"]} for item in downloaded],
        "skipped": skipped,
        "report": str(report_path),
    }, indent=2))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ids", default=",".join(DEFAULT_IDS))
    parser.add_argument("--game", default="26.3")
    parser.add_argument("--horizon-commit", required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--report", type=pathlib.Path, required=True)
    args = parser.parse_args()
    ids = tuple(part.strip() for part in args.ids.split(",") if part.strip())
    if not ids or len(set(ids)) != len(ids):
        raise SystemExit("--ids must contain unique, non-empty registry project IDs.")
    try:
        download_registry_mods(args.output, args.report, ids, args.game, args.horizon_commit)
    except Exception as error:
        raise SystemExit(f"Registry mod preparation failed closed: {error}") from error


if __name__ == "__main__":
    main()
