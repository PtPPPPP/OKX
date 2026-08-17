"""Build a source-only audit manifest without reading database contents."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

INCLUDED_SUFFIXES = {".py", ".toml", ".yaml", ".yml", ".json", ".md"}
SOURCE_DIRECTORIES = {"app", "backtest", "configs", "tests", "docs"}
EXCLUDED_DIRECTORIES = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "logs",
    "data",
    "graphify-out",
}
EXCLUDED_FILENAMES = {".env"}


def classify(relative_path: Path) -> str:
    return relative_path.parts[0] if len(relative_path.parts) > 1 else "project_root"


def is_in_scope(relative_path: Path) -> bool:
    if relative_path.name in EXCLUDED_FILENAMES:
        return False
    if any(part in EXCLUDED_DIRECTORIES for part in relative_path.parts):
        return False
    return relative_path.suffix.lower() in INCLUDED_SUFFIXES and (
        len(relative_path.parts) == 1 or relative_path.parts[0] in SOURCE_DIRECTORIES
    )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(root: Path) -> dict[str, object]:
    entries = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative_path = path.relative_to(root)
        if not is_in_scope(relative_path):
            continue
        stat = path.stat()
        entries.append(
            {
                "relative_path": relative_path.as_posix(),
                "file_size": stat.st_size,
                "sha256": sha256(path),
                "modified_time": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
                "category": classify(relative_path),
            }
        )
    return {
        "audit_version": "6C-1-pre-change",
        "generated_at": datetime.now(UTC).isoformat(),
        "scope": "source and documentation only; database contents and secrets excluded",
        "file_count": len(entries),
        "files": entries,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(build_manifest(args.root.resolve()), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
