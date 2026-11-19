"""Portable archives of user-visible Lead Extractor data."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from services.storage import data_base, list_data_folders, write_json

ARCHIVE_FORMAT = "lead-extractor-data-v2"
_LEGACY_ARCHIVE_FORMAT = "lead-extractor-data-v1"
_VISIBLE_ROOT_ITEMS = ("companies", "crm", "review", "categories.json")
_LEGACY_SOURCE_ROOT_ITEMS = ("ingest/archived", "ingest/incoming")
_OWNED_ROOT_ITEMS = ("companies", "crm", "review", "ingest", "extracted", "logs", "backups", "categories.json", "folders.json")


def create_data_archive(
    settings: dict[str, Any],
    destination: Path,
    progress_callback: Callable[[int, int, int, int, str], None] | None = None,
) -> Path:
    """Write displayed workspace data while omitting generated extraction caches."""
    base = data_base(settings)
    folders = list_data_folders(settings)
    folder_ids = {folder["id"] for folder in folders}
    active_folder_id = str(settings.get("active_folder_id") or "")
    manifest = {
        "format": ARCHIVE_FORMAT,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "active_folder_id": active_folder_id if active_folder_id in folder_ids else "",
        "custom_statuses": _clean_custom_statuses(settings.get("custom_statuses", [])),
        "folders": folders,
    }
    files: list[tuple[Path, str, int]] = []
    for folder in folders:
        root = base if not folder["id"] else base / "workspaces" / folder["id"]
        prefix = "data/default" if not folder["id"] else f"data/workspaces/{folder['id']}"
        for relative in _VISIBLE_ROOT_ITEMS:
            source = root / relative
            if source.is_file() and not source.is_symlink():
                files.append((source, f"{prefix}/{relative}", source.stat().st_size))
            elif source.is_dir() and not source.is_symlink():
                for path in sorted(source.rglob("*")):
                    if path.is_file() and not path.is_symlink() and not path.name.endswith(".tmp"):
                        files.append((path, f"{prefix}/{path.relative_to(root).as_posix()}", path.stat().st_size))

    total_files = len(files)
    total_bytes = sum(size for _, _, size in files)
    completed_bytes = 0
    if progress_callback:
        progress_callback(0, total_files, 0, total_bytes, "")
    # Favor CPU time for large source archives; level 1 remains ZIP-compressed
    # while avoiding a long, silent wait on collections of HTML pages.
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
        archive.writestr("archive_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        for index, (source, archive_name, size) in enumerate(files, start=1):
            _write_file(archive, source, archive_name)
            completed_bytes += size
            if progress_callback:
                progress_callback(index, total_files, completed_bytes, total_bytes, source.name)
    return destination


def _write_file(archive: zipfile.ZipFile, source: Path, archive_name: str) -> None:
    archive.write(source, archive_name)


def _clean_custom_statuses(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        status = " ".join(str(item).split())
        if status and status.casefold() not in seen and len(status) <= 80:
            result.append(status)
            seen.add(status.casefold())
    return result


def _validate_relative_data_path(
    path: PurePosixPath,
    known_folder_ids: set[str],
    *,
    allow_legacy_sources: bool = False,
) -> tuple[str, Path] | None:
    parts = path.parts
    if len(parts) < 3 or parts[0] != "data":
        return None
    if parts[1] == "default":
        folder_key, relative_parts = "default", parts[2:]
    elif parts[1] == "workspaces" and len(parts) >= 4 and parts[2] in known_folder_ids:
        folder_key, relative_parts = parts[2], parts[3:]
    else:
        return None
    relative = Path(*relative_parts)
    posix_relative = PurePosixPath(*relative_parts)
    if posix_relative.is_absolute() or any(part in {"", ".", ".."} for part in posix_relative.parts):
        return None
    relative_text = posix_relative.as_posix()
    allowed_roots = (*_VISIBLE_ROOT_ITEMS, *(_LEGACY_SOURCE_ROOT_ITEMS if allow_legacy_sources else ()))
    allowed = relative_text == "categories.json" or any(
        relative_text.startswith(item + "/")
        for item in allowed_roots if item != "categories.json"
    )
    return (folder_key, relative) if allowed else None


def _load_and_stage_archive(archive_path: Path, staging: Path) -> tuple[list[dict[str, str]], str]:
    with zipfile.ZipFile(archive_path, "r") as archive:
        try:
            manifest = json.loads(archive.read("archive_manifest.json").decode("utf-8"))
        except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("This is not a Lead Extractor data archive") from exc
        if not isinstance(manifest, dict) or manifest.get("format") not in {
            ARCHIVE_FORMAT, _LEGACY_ARCHIVE_FORMAT,
        }:
            raise ValueError("Unsupported Lead Extractor archive format")
        legacy_archive = manifest.get("format") == _LEGACY_ARCHIVE_FORMAT
        custom_statuses = _clean_custom_statuses(manifest.get("custom_statuses", []))
        raw_folders = manifest.get("folders")
        if not isinstance(raw_folders, list):
            raise ValueError("Archive workspace list is missing")
        folders: list[dict[str, str]] = []
        seen_ids: set[str] = set()
        seen_names: set[str] = set()
        for item in raw_folders:
            if not isinstance(item, dict):
                raise ValueError("Archive workspace entry is invalid")
            folder_id, name = str(item.get("id") or ""), " ".join(str(item.get("name") or "").split())
            if not name or len(name) > 80 or name.casefold() in seen_names:
                raise ValueError("Archive workspace names are invalid or duplicated")
            if folder_id and (not re.fullmatch(r"[a-f0-9]{32}", folder_id) or folder_id in seen_ids):
                raise ValueError("Archive workspace id is invalid or duplicated")
            if not folder_id and not name:
                raise ValueError("The default workspace entry is invalid")
            seen_ids.add(folder_id)
            seen_names.add(name.casefold())
            folders.append({"id": folder_id, "name": name})
        if not any(not folder["id"] for folder in folders):
            raise ValueError("Archive does not include the Default workspace")
        active_folder_id = str(manifest.get("active_folder_id") or "")
        if active_folder_id not in seen_ids:
            raise ValueError("Archive active workspace does not exist")
        folder_ids = {folder["id"] for folder in folders if folder["id"]}

        total_size = 0
        seen_entries: set[str] = set()
        for entry in archive.infolist():
            if entry.filename in seen_entries:
                raise ValueError("Archive contains duplicate file paths")
            seen_entries.add(entry.filename)
            if "\\" in entry.filename:
                raise ValueError("Archive contains an invalid path")
            archive_path_part = PurePosixPath(entry.filename)
            if archive_path_part.is_absolute() or any(part in {"", ".", ".."} for part in archive_path_part.parts):
                raise ValueError("Archive contains an unsafe path")
            if entry.filename == "archive_manifest.json":
                if entry.is_dir():
                    raise ValueError("Archive manifest is invalid")
                continue
            target_info = _validate_relative_data_path(
                archive_path_part, folder_ids, allow_legacy_sources=legacy_archive,
            )
            if target_info is None:
                raise ValueError(f"Archive contains unsupported data: {entry.filename}")
            if entry.is_dir():
                continue
            if stat.S_ISLNK(entry.external_attr >> 16):
                raise ValueError("Archive contains a symbolic link")
            folder_key, relative = target_info
            relative_text = relative.as_posix()
            if legacy_archive and any(
                relative_text.startswith(item + "/") for item in _LEGACY_SOURCE_ROOT_ITEMS
            ):
                # Older archives contained retry source files. Keep accepting
                # them, but do not restore these originals into the new data model.
                continue
            total_size += entry.file_size
            if total_size > 10 * 1024 * 1024 * 1024:
                raise ValueError("Archive is larger than the supported 10 GB limit")
            folder_root = staging / ("default" if folder_key == "default" else f"workspaces/{folder_key}")
            destination = folder_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(entry, "r") as source, destination.open("wb") as output:
                shutil.copyfileobj(source, output)
    return folders, active_folder_id, custom_statuses


def restore_data_archive(settings: dict[str, Any], archive_path: Path) -> dict[str, Any]:
    """Validate then replace app data transactionally, keeping rollback files until commit."""
    base = data_base(settings)
    base.mkdir(parents=True, exist_ok=True)
    staging_parent = base.parent
    stage = Path(tempfile.mkdtemp(prefix="lead-extractor-restore-", dir=staging_parent))
    staged_data = stage / "new"
    staged_data.mkdir()
    rollback = stage / "old"
    rollback.mkdir()
    installed: list[str] = []
    moved_old: list[str] = []
    try:
        folders, active_folder_id, custom_statuses = _load_and_stage_archive(archive_path, staged_data)
        folders_manifest = [folder for folder in folders if folder["id"]]
        write_json(staged_data / "folders.json", folders_manifest)
        (staged_data / "workspaces").mkdir(parents=True, exist_ok=True)
        (staged_data / "default").mkdir(parents=True, exist_ok=True)

        workspace_root = base / "workspaces"
        if workspace_root.is_symlink():
            raise ValueError("Workspace data directory cannot be a symbolic link")
        for item in (*_OWNED_ROOT_ITEMS, "workspaces"):
            existing = base / item
            if not existing.exists():
                continue
            saved = rollback / item
            saved.parent.mkdir(parents=True, exist_ok=True)
            os.replace(existing, saved)
            moved_old.append(item)
        default_root = staged_data / "default"
        new_items = [path for path in default_root.iterdir()] + [
            staged_data / "workspaces",
            staged_data / "folders.json",
        ]
        for source in new_items:
            name = source.name
            os.replace(source, base / name)
            installed.append(name)
    except Exception:
        for name in reversed(installed):
            current = base / name
            if current.is_dir() and not current.is_symlink():
                shutil.rmtree(current)
            else:
                current.unlink(missing_ok=True)
        for name in reversed(moved_old):
            saved = rollback / name
            if saved.exists():
                os.replace(saved, base / name)
        raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    default_folder = next(folder for folder in folders if not folder["id"])
    return {
        "active_folder_id": active_folder_id,
        "default_folder_name": default_folder["name"],
        "custom_statuses": custom_statuses,
    }
