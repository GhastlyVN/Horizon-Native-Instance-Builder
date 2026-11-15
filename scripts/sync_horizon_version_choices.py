#!/usr/bin/env python3
"""Keep the build workflow's version dropdown synchronized with the checked-in catalog."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

TAG = re.compile(r"^nightly(?:-\d{4}-\d{2}-\d{2})?$")


def sync_choices(catalog_path: Path, workflow_path: Path) -> int:
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    versions = catalog.get("versions")
    if not isinstance(versions, list) or not versions:
        raise ValueError("Horizon version catalog is empty or malformed.")
    tags = ["nightly"]
    for item in versions:
        tag = item.get("tag") if isinstance(item, dict) else None
        if not isinstance(tag, str) or not TAG.fullmatch(tag):
            raise ValueError(f"Invalid Nightly tag in catalog: {tag!r}")
        if tag not in tags:
            tags.append(tag)
        if len(tags) == 25:
            break
    source = workflow_path.read_text(encoding="utf-8")
    block = re.compile(r"(?ms)^      horizon_ref:\n.*?(?=^      include_native_mods:)")
    options = "\n".join(f'          - "{tag}"' for tag in tags)
    replacement = (
        '      horizon_ref:\n'
        '        description: "Choose a version from catalog/horizon-versions.json"\n'
        '        required: true\n'
        '        default: "nightly"\n'
        '        type: choice\n'
        '        options:\n'
        f'{options}\n'
    )
    updated, count = block.subn(replacement, source, count=1)
    if count != 1:
        raise ValueError("Could not locate the horizon_ref input block in build.yml.")
    workflow_path.write_text(updated, encoding="utf-8")
    return len(tags)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--workflow", type=Path, required=True)
    args = parser.parse_args()
    print(f"Synchronized {sync_choices(args.catalog, args.workflow)} Horizon version dropdown options.")


if __name__ == "__main__":
    main()
