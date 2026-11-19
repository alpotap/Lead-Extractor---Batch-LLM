"""FastAPI web server and single-page application for Lead Extractor."""

from __future__ import annotations

import json
import asyncio
import logging
import math
import re
import shutil
import socket
import threading
import time
import tempfile
import uuid
import zipfile
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.background import BackgroundTask
from fastapi.staticfiles import StaticFiles

from models.lead import (
    DEFAULT_STATUSES,
    EXTRACTION_TEMPLATE,
    ORGANIZATION_CATEGORIES,
    empty_crm,
    normalize_contacts,
    normalize_opening_hours,
    normalize_social_links,
)
from services.ollama import is_local_url, is_ollama_cloud_url, list_models
from services.backups import create_data_archive, restore_data_archive
from services.pipeline import (
    SUPPORTED_EXTENSIONS,
    close_data_loggers,
    configure_logging,
    get_processing_job,
    is_scanner_running,
    _prepare_source,
    process_incoming,
    pause_background_scanner,
    release_processing_sources,
    reserve_processing_sources,
    resume_background_scanner,
    start_background_scanner,
)
from services.storage import (
    company_files,
    create_data_folder,
    data_root,
    data_folder_summary,
    delete_data_folder,
    load_category_taxonomy,
    load_deleted_default_categories,
    load_settings,
    list_data_folders,
    read_json,
    rename_data_folder,
    rebuild_companies,
    safe_stem,
    save_category_taxonomy,
    save_ollama_api_key,
    save_settings,
    write_json,
)

_url_fetch_jobs: dict[str, dict[str, Any]] = {}
_URL_FETCH_CONCURRENCY = 5
_data_archive_jobs: dict[str, dict[str, Any]] = {}
_data_archive_jobs_lock = threading.Lock()
from services.web_capture import capture_rendered_html

APP_VERSION = (Path(__file__).resolve().parent / "VERSION").read_text(encoding="utf-8").strip()

@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Start the background ingestion scanner when the FastAPI server starts."""
    settings = _get_settings()
    root = _get_root()
    start_background_scanner(settings)
    configure_logging(root).info("Background ingestion scanner started")
    yield
    configure_logging(root).info("Background ingestion scanner stopped")


app = FastAPI(title="Lead Extractor", version=APP_VERSION, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_cache_lock = threading.Lock()
_company_summaries_cache: list[dict[str, Any]] | None = None
_company_summaries_cache_time: float = 0.0
_crm_summaries_cache: dict[str, dict[str, Any]] = {}
_crm_summaries_cache_time: float = 0.0
CACHE_TTL = 30.0


def _invalidate_caches() -> None:
    global _company_summaries_cache, _crm_summaries_cache
    with _cache_lock:
        _company_summaries_cache = None
        _crm_summaries_cache = {}


def _get_settings() -> dict[str, Any]:
    return load_settings()


def _get_root() -> Path:
    return data_root(_get_settings())


def _launch_processing(
    settings: dict[str, Any],
    only_names: set[str] | None,
    manual_fields: dict[str, Any] | None,
) -> None:
    """Run the extraction pipeline in a daemon thread so HTTP requests return immediately.

    The UI stays responsive and can be closed or refreshed while extraction runs;
    progress is exposed through /api/processing-status.
    """

    reserved = set(only_names or ())
    if reserved:
        reserve_processing_sources(reserved)

    def runner() -> None:
        try:
            _process_incoming_with_lock_wait(settings, only_names, manual_fields)
            _invalidate_caches()
        finally:
            if reserved:
                release_processing_sources(reserved)

    threading.Thread(target=runner, name="lead-manual-processing", daemon=True).start()


def _process_incoming_with_lock_wait(
    settings: dict[str, Any],
    only_names: set[str] | None,
    manual_fields: dict[str, Any] | None,
) -> dict[str, Any]:
    """Run process_incoming, waiting for the processing lock if another run holds it.

    The queue worker or another manual trigger may hold the lock. Keep this job
    queued until the worker can own the pipeline lock, without blocking HTTP.
    """
    while True:
        outcome = process_incoming(
            settings, only_names=only_names, manual_fields=manual_fields, include_reserved=True
        )
        if not outcome.get("skipped"):
            return outcome
        time.sleep(0.25)


def _lan_ip() -> str:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("192.0.2.1", 80))
            ip = sock.getsockname()[0]
        finally:
            sock.close()
        return ip if not ip.startswith("127.") else "127.0.0.1"
    except Exception:
        return "127.0.0.1"


def _date_val(value: str) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            parts = [int(p) for p in value.split("/") if p.isdigit()]
            if len(parts) == 3:
                day, month, year = parts
                if year < 100:
                    year += 2000
                return date(year, month, day)
        except Exception:
            pass
        return None


def _format_date(value: Any) -> str:
    d = _date_val(str(value or ""))
    return d.strftime("%d/%m/%y") if d else ""


def _followup_notes(value: Any) -> list[dict[str, str]]:
    notes = []
    items = value if isinstance(value, list) else [value]
    for index, item in enumerate(items):
        if isinstance(item, dict):
            text = str(item.get("text", "")).strip()
            if text:
                notes.append({
                    "id": str(item.get("id") or f"note-{index}"),
                    "text": text,
                    "created_at": str(item.get("created_at") or ""),
                })
        elif str(item).strip():
            notes.append({"id": f"legacy-{index}", "text": str(item).strip(), "created_at": ""})
    return notes


def _flat_string_list(value: Any) -> list[str]:
    """Flatten a list that may contain strings or dicts into a list of strings.

    Company records sometimes store phones/emails as dicts like
    {"value": "+123..."} instead of plain strings. This normalizes both shapes.
    """
    items = value if isinstance(value, list) else []
    result: list[str] = []
    for item in items:
        if isinstance(item, dict):
            text = str(item.get("value", item.get("number", ""))).strip()
        else:
            text = str(item).strip()
        if text:
            result.append(text)
    return result


def _get_company_summaries(root: Path) -> list[dict[str, Any]]:
    global _company_summaries_cache, _company_summaries_cache_time
    now = datetime.now().timestamp()
    with _cache_lock:
        if _company_summaries_cache is not None and (now - _company_summaries_cache_time < CACHE_TTL):
            return _company_summaries_cache

    summaries = []
    for path in company_files(root):
        record = read_json(path, {})
        if not isinstance(record, dict) or not record.get("company_id"):
            continue
        emails = record.get("emails", [])
        emails = emails if isinstance(emails, list) else [emails]
        phones = record.get("phones", [])
        phones = phones if isinstance(phones, list) else [phones]
        contacts = normalize_contacts(record.get("contacts", []))
        email_set = {str(v).strip() for v in emails if str(v).strip()}
        phone_set = {str(v).strip() for v in phones if str(v).strip()}
        for c in contacts:
            for em in c.get("emails", []):
                if em:
                    email_set.add(str(em).strip())
            for ph in c.get("phones", []):
                if ph:
                    phone_set.add(str(ph).strip())

        raw_locs = record.get("locations", [])
        locations = [str(v) for v in (raw_locs if isinstance(raw_locs, list) else [raw_locs]) if v]

        summaries.append({
            "company_id": str(record.get("company_id", "")),
            "company_name": str(record.get("company_name") or ""),
            "organization_category": str(record.get("organization_category") or ""),
            "subcategory": str(record.get("subcategory") or ""),
            "website": str(record.get("website") or "").strip(),
            "first_seen": str(record.get("first_seen") or ""),
            "last_seen": str(record.get("last_seen") or ""),
            "archived": bool(record.get("archived", False)),
            "locations": locations,
            "email_count": len(email_set),
            "phone_count": len(phone_set),
            "social_count": len(normalize_social_links(record.get("social_links", []))),
        })

    with _cache_lock:
        _company_summaries_cache = summaries
        _company_summaries_cache_time = now
    return summaries


def _get_crm_summaries(root: Path, company_ids: list[str]) -> dict[str, dict[str, Any]]:
    global _crm_summaries_cache, _crm_summaries_cache_time
    now = datetime.now().timestamp()
    with _cache_lock:
        if _crm_summaries_cache and (now - _crm_summaries_cache_time < CACHE_TTL):
            missing = [cid for cid in company_ids if cid not in _crm_summaries_cache]
            if not missing:
                return _crm_summaries_cache

    summaries: dict[str, dict[str, Any]] = {}
    for company_id in company_ids:
        crm_path = root / "crm" / f"{safe_stem(company_id)}.json"
        record = read_json(crm_path, {})
        record = record if isinstance(record, dict) else {}
        notes = record.get("notes", [])
        summaries[company_id] = {
            "status": str(record.get("status") or "New"),
            "rating": str(record.get("rating") or ""),
            "note_count": len(_followup_notes(notes)),
            "last_contacted": str(record.get("last_contacted") or ""),
            "next_followup": str(record.get("next_followup") or ""),
        }

    with _cache_lock:
        _crm_summaries_cache.update(summaries)
        _crm_summaries_cache_time = now
    return _crm_summaries_cache


@app.get("/api/status")
def api_status() -> dict[str, Any]:
    settings = _get_settings()
    root = _get_root()
    ollama_url = str(settings.get("ollama_url", "http://localhost:11434"))
    api_key = str(settings.get("ollama_api_key", ""))
    selected_model = str(settings.get("selected_model", ""))
    is_local = is_local_url(ollama_url)
    is_cloud = is_ollama_cloud_url(ollama_url)

    models = []
    ollama_connected = False
    if is_cloud:
        ollama_connected = bool(api_key or selected_model)
    else:
        try:
            models = list_models(ollama_url, api_key)
            ollama_connected = True
        except Exception:
            ollama_connected = False

    companies = _get_company_summaries(root)
    review_files = list((root / "review").glob("*.review.json"))
    lan = _lan_ip()

    return {
        "ollama_connected": ollama_connected,
        "ai_service_connected": ollama_connected,
        "ollama_url": ollama_url,
        "selected_model": selected_model,
        "is_local_url": is_local,
        "is_cloud_url": is_cloud,
        "available_models": models,
        "lan_ip": lan if not lan.startswith("127.") else "",
        "lan_port": 8502,
        "total_companies": len(companies),
        "review_count": len(review_files),
        "scanner_running": is_scanner_running(),
        "data_path": str(root),
    }


@app.get("/api/processing-status")
def api_processing_status() -> dict[str, Any]:
    """Live processing progress and pending-file counts for the UI progress bar."""
    root = _get_root()
    incoming = root / "ingest" / "incoming"
    incoming_count = 0
    if incoming.exists():
        incoming_count = sum(
            1 for p in incoming.iterdir()
            if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
        )
    review_count = len(list((root / "review").glob("*.review.json")))
    parsed_count = sum(
        1 for p in (root / "ingest" / "parsed").glob("*.parsed.txt")
        if (root / "ingest" / "incoming" / p.name.removesuffix(".parsed.txt")).exists()
    )
    return {
        "incoming_count": incoming_count,
        "parsed_count": parsed_count,
        "review_count": review_count,
        "scanner_running": is_scanner_running(),
        "job": get_processing_job(),
    }


@app.get("/api/folders")
def api_list_folders() -> dict[str, Any]:
    settings = _get_settings()
    folders = list_data_folders(settings)
    return {
        "folders": folders,
        "active_folder_id": str(settings.get("active_folder_id") or ""),
        "active_folder_name": next(
            (item["name"] for item in folders
             if item["id"] == str(settings.get("active_folder_id") or "")),
            "Default",
        ),
    }


@app.post("/api/folders")
async def api_create_folder(request: Request) -> dict[str, Any]:
    payload = await request.json()
    settings = _get_settings()
    try:
        folder = create_data_folder(settings, str(payload.get("name") or ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    settings["active_folder_id"] = folder["id"]
    save_settings(settings)
    data_root(settings)
    _invalidate_caches()
    return {"status": "success", "folder": folder, "active_folder_id": folder["id"]}


@app.post("/api/folders/activate")
async def api_activate_folder(request: Request) -> dict[str, Any]:
    payload = await request.json()
    settings = _get_settings()
    folder_id = str(payload.get("id") or "")
    folders = list_data_folders(settings)
    folder = next((item for item in folders if item["id"] == folder_id), None)
    if folder is None:
        raise HTTPException(status_code=404, detail="Folder not found")
    settings["active_folder_id"] = folder_id
    save_settings(settings)
    data_root(settings)
    _invalidate_caches()
    return {"status": "success", "folder": folder}


@app.get("/api/folders/{folder_id}/summary")
def api_folder_summary(folder_id: str) -> dict[str, Any]:
    try:
        return data_folder_summary(_get_settings(), folder_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/folders/rename")
async def api_rename_folder(request: Request) -> dict[str, Any]:
    payload = await request.json()
    settings = _get_settings()
    folder_id = str(payload.get("id") or "")
    try:
        folder = rename_data_folder(
            settings, folder_id, str(payload.get("name") or "")
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not folder_id:
        settings["default_folder_name"] = folder["name"]
        save_settings(settings)
    _invalidate_caches()
    return {"status": "success", "folder": folder}


@app.post("/api/folders/delete")
async def api_delete_folder(request: Request) -> dict[str, Any]:
    payload = await request.json()
    settings = _get_settings()
    folder_id = str(payload.get("id") or "")
    try:
        summary = data_folder_summary(settings, folder_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if _processing_job := get_processing_job():
        if not _processing_job.get("finished"):
            raise HTTPException(status_code=409, detail="Wait for active processing to finish before deleting a folder")
    if summary["has_data"] and str(payload.get("confirmation") or "") != summary["name"]:
        raise HTTPException(status_code=409, detail="Type the folder name to confirm deleting its data")
    if not summary["has_data"] and payload.get("confirmed") is not True:
        raise HTTPException(status_code=409, detail="Confirm deleting this empty folder")

    scanner_paused = False
    data_loggers_closed = False
    try:
        scanner_paused = await asyncio.to_thread(pause_background_scanner, 30.0)
        close_data_loggers()
        data_loggers_closed = True
        try:
            folder = delete_data_folder(settings, folder_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if str(settings.get("active_folder_id") or "") == folder_id:
            settings["active_folder_id"] = ""
        save_settings(settings)
        data_root(settings)
        _invalidate_caches()
        return {"status": "success", "deleted": folder}
    except TimeoutError as exc:
        raise HTTPException(status_code=409, detail=f"Could not pause background processing for folder deletion: {exc}") from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not delete folder data: {exc}") from exc
    finally:
        try:
            if data_loggers_closed:
                configure_logging(data_root(_get_settings()))
        finally:
            if scanner_paused:
                resume_background_scanner(scanner_paused)


@app.get("/api/data/archive")
def api_download_data_archive() -> FileResponse:
    job = get_processing_job()
    if job and not job.get("finished"):
        raise HTTPException(status_code=409, detail="Wait for active processing to finish before creating an archive")
    settings = _get_settings()
    archive_path = Path(tempfile.gettempdir()) / f"lead-extractor-data-{uuid.uuid4().hex}.zip"
    create_data_archive(settings, archive_path)
    filename = f"lead-extractor-data-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
    return FileResponse(
        archive_path,
        media_type="application/zip",
        filename=filename,
        background=BackgroundTask(lambda: archive_path.unlink(missing_ok=True)),
    )


def _run_data_archive_job(job_id: str, settings: dict[str, Any], archive_path: Path) -> None:
    def update_progress(done: int, total: int, bytes_done: int, bytes_total: int, current: str) -> None:
        with _data_archive_jobs_lock:
            job = _data_archive_jobs.get(job_id)
            if job is not None:
                job.update(
                    files_done=done, total_files=total, bytes_done=bytes_done,
                    total_bytes=bytes_total, current_file=current,
                )

    try:
        create_data_archive(settings, archive_path, progress_callback=update_progress)
        with _data_archive_jobs_lock:
            job = _data_archive_jobs.get(job_id)
            if job is not None:
                job.update(status="ready", finished=True, current_file="")
    except Exception as exc:
        with _data_archive_jobs_lock:
            job = _data_archive_jobs.get(job_id)
            if job is not None:
                job.update(status="failed", finished=True, error=str(exc), current_file="")
        archive_path.unlink(missing_ok=True)


@app.post("/api/data/archive/jobs")
async def api_start_data_archive_job() -> dict[str, Any]:
    processing_job = get_processing_job()
    if processing_job and not processing_job.get("finished"):
        raise HTTPException(status_code=409, detail="Wait for active processing to finish before creating an archive")
    with _data_archive_jobs_lock:
        if any(job.get("status") == "preparing" for job in _data_archive_jobs.values()):
            raise HTTPException(status_code=409, detail="A data archive is already being prepared")
        job_id = uuid.uuid4().hex
        archive_path = Path(tempfile.gettempdir()) / f"lead-extractor-data-{job_id}.zip"
        _data_archive_jobs[job_id] = {
            "job_id": job_id, "status": "preparing", "finished": False,
            "files_done": 0, "total_files": 0, "bytes_done": 0, "total_bytes": 0,
            "current_file": "", "error": "",
        }
    asyncio.create_task(asyncio.to_thread(_run_data_archive_job, job_id, _get_settings(), archive_path))
    return {"job_id": job_id, "status": "preparing"}


@app.get("/api/data/archive/jobs/{job_id}")
def api_get_data_archive_job(job_id: str) -> dict[str, Any]:
    with _data_archive_jobs_lock:
        job = _data_archive_jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Archive job not found")
        return dict(job)


def _cleanup_data_archive_job(job_id: str, archive_path: Path) -> None:
    archive_path.unlink(missing_ok=True)
    with _data_archive_jobs_lock:
        _data_archive_jobs.pop(job_id, None)


@app.get("/api/data/archive/jobs/{job_id}/download")
def api_download_ready_data_archive(job_id: str) -> FileResponse:
    with _data_archive_jobs_lock:
        job = _data_archive_jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Archive job not found")
        if job.get("status") != "ready":
            raise HTTPException(status_code=409, detail="Archive is not ready")
    archive_path = Path(tempfile.gettempdir()) / f"lead-extractor-data-{job_id}.zip"
    if not archive_path.is_file():
        raise HTTPException(status_code=404, detail="Prepared archive file was not found")
    filename = f"lead-extractor-data-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
    return FileResponse(
        archive_path,
        media_type="application/zip",
        filename=filename,
        background=BackgroundTask(_cleanup_data_archive_job, job_id, archive_path),
    )


@app.post("/api/data/archive/restore")
async def api_restore_data_archive(request: Request) -> dict[str, Any]:
    if request.headers.get("x-restore-confirm", "") != "RESTORE":
        raise HTTPException(status_code=400, detail="Confirm the restore in Settings before uploading the archive")
    job = get_processing_job()
    if job and not job.get("finished"):
        raise HTTPException(status_code=409, detail="Wait for active processing to finish before restoring an archive")
    archive_path = Path(tempfile.gettempdir()) / f"lead-extractor-upload-{uuid.uuid4().hex}.zip"
    try:
        with archive_path.open("wb") as destination:
            async for chunk in request.stream():
                destination.write(chunk)
        settings = _get_settings()
        scanner_paused = False
        data_loggers_closed = False
        try:
            scanner_paused = await asyncio.to_thread(pause_background_scanner, 30.0)
            close_data_loggers()
            data_loggers_closed = True
            try:
                restored_state = restore_data_archive(settings, archive_path)
            except (ValueError, OSError, zipfile.BadZipFile) as exc:
                raise HTTPException(status_code=400, detail=f"Could not restore archive: {exc}") from exc
            settings["active_folder_id"] = restored_state["active_folder_id"]
            settings["default_folder_name"] = restored_state["default_folder_name"]
            settings["custom_statuses"] = restored_state["custom_statuses"]
            save_settings(settings)
            data_root(settings)
            _invalidate_caches()
            return {"status": "success", "active_folder_id": restored_state["active_folder_id"]}
        except TimeoutError as exc:
            raise HTTPException(status_code=409, detail=f"Could not pause background processing for restore: {exc}") from exc
        finally:
            try:
                if data_loggers_closed:
                    configure_logging(data_root(_get_settings()))
            finally:
                if scanner_paused:
                    resume_background_scanner(scanner_paused)
    finally:
        archive_path.unlink(missing_ok=True)


@app.get("/api/companies")
def api_companies(
    query: str = "",
    category: str = "",
    subcategory: str = "",
    status: str = "",
    location: str = "",
    has_website: str = "",
    has_phone: str = "",
    has_socials: str = "",
    archived: bool = False,
    sort_by: str = "Company",
    descending: bool = False,
    secondary_sort_by: str = "",
    secondary_descending: bool = False,
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    root = _get_root()
    summaries = _get_company_summaries(root)
    company_ids = [c["company_id"] for c in summaries]
    crm_by_id = _get_crm_summaries(root, company_ids)

    all_categories = sorted({
        c["organization_category"] for c in summaries if c["organization_category"] and c["organization_category"].casefold() != "unknown"
    }, key=str.casefold)
    all_subcategories = sorted({
        c["subcategory"] for c in summaries if c.get("subcategory")
    }, key=str.casefold)
    all_locations = sorted({
        loc for c in summaries for loc in c.get("locations", []) if loc
    }, key=str.casefold)
    all_statuses = sorted(list(DEFAULT_STATUSES), key=str.casefold)

    visible: list[dict[str, Any]] = []
    q = query.casefold().strip()

    for c in summaries:
        cid = c["company_id"]
        is_archived = c.get("archived", False)
        if is_archived and not archived:
            continue

        crm = crm_by_id.get(cid, {})
        c_status = crm.get("status", "New")
        c_cat = c["organization_category"]
        c_sub = c["subcategory"]

        if status and c_status != status:
            continue
        if category and c_cat.casefold() != category.casefold():
            continue
        if subcategory and c_sub.casefold() != subcategory.casefold():
            continue
        if location and not any(location.casefold() in loc.casefold() for loc in c.get("locations", [])):
            continue
        if has_website == "has" and not c.get("website"):
            continue
        if has_website == "none" and c.get("website"):
            continue
        if has_phone == "has" and c.get("phone_count", 0) == 0:
            continue
        if has_phone == "none" and c.get("phone_count", 0) > 0:
            continue
        if has_socials == "has" and c.get("social_count", 0) == 0:
            continue
        if has_socials == "none" and c.get("social_count", 0) > 0:
            continue

        if q:
            haystack = f"{c['company_name']} {c_cat} {c_sub} {' '.join(c.get('locations', []))}".casefold()
            if q not in haystack:
                full_rec = read_json(root / "companies" / f"{safe_stem(cid)}.json", {})
                if isinstance(full_rec, dict):
                    contacts_txt = " ".join(
                        f"{contact.get('name','')} {' '.join(contact.get('emails',[]))} {' '.join(contact.get('phones',[]))}"
                        for contact in normalize_contacts(full_rec.get("contacts", []))
                    )
                    emails_txt = " ".join(_flat_string_list(full_rec.get("emails", [])))
                    phones_txt = " ".join(_flat_string_list(full_rec.get("phones", [])))
                    haystack += f" {contacts_txt} {emails_txt} {phones_txt}".casefold()
            if q not in haystack:
                continue

        visible.append({
            **c,
            "status": c_status,
            "rating": crm.get("rating", ""),
            "note_count": crm.get("note_count", 0),
            "last_contacted": crm.get("last_contacted", ""),
            "next_followup": crm.get("next_followup", ""),
            "first_seen_formatted": _format_date(c["first_seen"]),
            "last_seen_formatted": _format_date(c["last_seen"]),
            "next_followup_formatted": _format_date(crm.get("next_followup")),
        })

    def _sort_key(record: dict[str, Any], column_name: str) -> Any:
        col = column_name.casefold()
        if "status" in col:
            return record.get("status", "").casefold()
        if "subcat" in col:
            return record.get("subcategory", "").casefold()
        if "cat" in col:
            return f"{record.get('organization_category', '')} {record.get('subcategory', '')}".casefold()
        if "email" in col:
            return int(record.get("email_count", 0))
        if "phone" in col:
            return int(record.get("phone_count", 0))
        if "social" in col:
            return int(record.get("social_count", 0))
        if "website" in col:
            return 1 if record.get("website") else 0
        if "note" in col:
            return int(record.get("note_count", 0))
        if "follow" in col or "next" in col:
            return _date_val(record.get("next_followup", "")) or date.min
        if "updated" in col or "last" in col:
            return _date_val(record.get("last_seen", "")) or date.min
        if "created" in col or "first" in col:
            return _date_val(record.get("first_seen", "")) or date.min
        return record.get("company_name", "").casefold()

    if secondary_sort_by:
        visible.sort(key=lambda r: _sort_key(r, secondary_sort_by), reverse=secondary_descending)
    visible.sort(key=lambda r: _sort_key(r, sort_by), reverse=descending)

    total = len(visible)
    if page_size <= 0:
        total_pages = 1
        page = 1
        items = visible
    else:
        total_pages = max(1, math.ceil(total / max(1, page_size)))
        page = max(1, min(page, total_pages))
        start_idx = (page - 1) * page_size
        items = visible[start_idx : start_idx + page_size]

    return {
        "companies": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "categories": all_categories,
        "subcategories": all_subcategories,
        "locations": all_locations,
        "statuses": all_statuses,
    }


@app.get("/api/companies/{company_id}")
def api_get_company(company_id: str) -> dict[str, Any]:
    root = _get_root()
    company_path = root / "companies" / f"{safe_stem(company_id)}.json"
    crm_path = root / "crm" / f"{safe_stem(company_id)}.json"

    company = read_json(company_path, {})
    if not isinstance(company, dict) or not company.get("company_id"):
        raise HTTPException(status_code=404, detail="Company not found")

    crm = read_json(crm_path, {})
    crm = crm if isinstance(crm, dict) else {}
    crm.setdefault("status", "New")
    crm.setdefault("rating", "")
    crm.setdefault("notes", [])
    sources = company.get("sources", [])
    sources = sources if isinstance(sources, list) else [sources]
    available_sources = [
        Path(str(source)).name for source in sources
        if Path(str(source)).name
        and (root / "ingest" / "archived" / Path(str(source)).name).is_file()
    ]

    return {
        "company": company,
        "available_sources": available_sources,
        "crm": {
            **crm,
            "notes": _followup_notes(crm.get("notes", [])),
        },
    }


@app.put("/api/companies/{company_id}")
async def api_update_company(company_id: str, request: Request) -> dict[str, Any]:
    root = _get_root()
    payload = await request.json()
    company_path = root / "companies" / f"{safe_stem(company_id)}.json"

    existing = read_json(company_path, {})
    if not isinstance(existing, dict) or not existing.get("company_id"):
        raise HTTPException(status_code=404, detail="Company not found")

    for field in [
        "company_name", "organization_category", "subcategory", "website",
        "emails", "phones", "addresses", "locations", "contacts", "social_links",
        "notes", "archived",
    ]:
        if field in payload:
            existing[field] = payload[field]
    if "opening_hours" in payload:
        existing["opening_hours"] = normalize_opening_hours(payload["opening_hours"])

    existing["last_seen"] = datetime.now().astimezone().isoformat(timespec="seconds")
    write_json(company_path, existing)
    _invalidate_caches()
    return {"status": "success", "company": existing}


@app.put("/api/companies/{company_id}/crm")
async def api_update_crm(company_id: str, request: Request) -> dict[str, Any]:
    root = _get_root()
    payload = await request.json()
    crm_path = root / "crm" / f"{safe_stem(company_id)}.json"

    crm = read_json(crm_path, {})
    crm = crm if isinstance(crm, dict) else empty_crm(company_id)

    for field in ["status", "rating", "last_contacted", "next_followup"]:
        if field in payload:
            crm[field] = str(payload[field] or "").strip()

    write_json(crm_path, crm)
    _invalidate_caches()
    return {"status": "success", "crm": crm}


@app.post("/api/companies/{company_id}/notes")
async def api_add_note(company_id: str, request: Request) -> dict[str, Any]:
    root = _get_root()
    payload = await request.json()
    text = str(payload.get("text", "")).strip()
    if not text:
        raise HTTPException(status_code=400, detail="Note text cannot be empty")

    crm_path = root / "crm" / f"{safe_stem(company_id)}.json"
    crm = read_json(crm_path, {})
    crm = crm if isinstance(crm, dict) else empty_crm(company_id)

    notes = _followup_notes(crm.get("notes", []))
    new_note = {
        "id": datetime.now().astimezone().strftime("%Y%m%d%H%M%S%f"),
        "text": text,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    notes.insert(0, new_note)
    crm["notes"] = notes
    write_json(crm_path, crm)
    _invalidate_caches()
    return {"status": "success", "note": new_note, "notes": notes}


@app.put("/api/companies/{company_id}/notes/{note_id}")
async def api_edit_note(company_id: str, note_id: str, request: Request) -> dict[str, Any]:
    root = _get_root()
    payload = await request.json()
    text = str(payload.get("text", "")).strip()
    if not text:
        raise HTTPException(status_code=400, detail="Note text cannot be empty")

    crm_path = root / "crm" / f"{safe_stem(company_id)}.json"
    crm = read_json(crm_path, {})
    crm = crm if isinstance(crm, dict) else empty_crm(company_id)

    notes = _followup_notes(crm.get("notes", []))
    for note in notes:
        if note["id"] == note_id:
            note["text"] = text
            break

    crm["notes"] = notes
    write_json(crm_path, crm)
    _invalidate_caches()
    return {"status": "success", "notes": notes}


@app.delete("/api/companies/{company_id}/notes/{note_id}")
def api_delete_note(company_id: str, note_id: str) -> dict[str, Any]:
    root = _get_root()
    crm_path = root / "crm" / f"{safe_stem(company_id)}.json"
    crm = read_json(crm_path, {})
    crm = crm if isinstance(crm, dict) else empty_crm(company_id)

    notes = [note for note in _followup_notes(crm.get("notes", [])) if note["id"] != note_id]
    crm["notes"] = notes
    write_json(crm_path, crm)
    _invalidate_caches()
    return {"status": "success", "notes": notes}


@app.post("/api/companies/{company_id}/archive")
def api_toggle_archive(company_id: str) -> dict[str, Any]:
    root = _get_root()
    company_path = root / "companies" / f"{safe_stem(company_id)}.json"
    company = read_json(company_path, {})
    if not isinstance(company, dict) or not company.get("company_id"):
        raise HTTPException(status_code=404, detail="Company not found")

    company["archived"] = not bool(company.get("archived", False))
    write_json(company_path, company)
    _invalidate_caches()
    return {"status": "success", "archived": company["archived"]}


@app.post("/api/companies/{company_id}/reextract")
async def api_reextract_company(company_id: str, request: Request) -> dict[str, Any]:
    root = _get_root()
    payload = await request.json()
    source_name = str(payload.get("source_name", "")).strip()
    if not source_name:
        raise HTTPException(status_code=400, detail="Source name required")

    archived_source = root / "ingest" / "archived" / source_name
    if not archived_source.exists():
        raise HTTPException(status_code=404, detail="Archived source not found")

    incoming_source = root / "ingest" / "incoming" / source_name
    if not incoming_source.exists():
        shutil.copy2(archived_source, incoming_source)

    settings = _get_settings()
    _launch_processing(settings, {source_name}, None)
    return {"status": "queued", "source_file": source_name}


@app.post("/api/companies")
async def api_create_lead_manually(request: Request) -> dict[str, Any]:
    root = _get_root()
    payload = await request.json()
    company_name = str(payload.get("company_name", "")).strip()
    if not company_name:
        raise HTTPException(status_code=400, detail="Company name is required")

    now = datetime.now().astimezone().isoformat(timespec="seconds")
    stamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    company_id = f"{safe_stem(company_name)}-{stamp}"

    record = dict(EXTRACTION_TEMPLATE)
    record.update({
        "company_id": company_id,
        "company_name": company_name,
        "organization_category": str(payload.get("organization_category", "")).strip(),
        "subcategory": str(payload.get("subcategory", "")).strip(),
        "website": str(payload.get("website", "")).strip(),
        "emails": [e.strip() for e in payload.get("emails", []) if e.strip()],
        "phones": [p.strip() for p in payload.get("phones", []) if p.strip()],
        "addresses": [a.strip() for a in payload.get("addresses", []) if a.strip()],
        "locations": [loc.strip() for loc in payload.get("locations", []) if loc.strip()],
        "notes": [n.strip() for n in payload.get("notes", []) if n.strip()],
        "contacts": payload.get("contacts", []),
        "social_links": payload.get("social_links", []),
        "opening_hours": payload.get("opening_hours", []),
        "sources": [],
        "first_seen": now,
        "last_seen": now,
        "schema_version": 1,
    })

    write_json(root / "companies" / f"{safe_stem(company_id)}.json", record)
    write_json(root / "crm" / f"{safe_stem(company_id)}.json", empty_crm(company_id))
    _invalidate_caches()
    return {"status": "success", "company_id": company_id, "company": record}


@app.post("/api/sources/url")
async def api_fetch_url(request: Request) -> JSONResponse:
    root = _get_root()
    payload = await request.json()
    urls_input = str(payload.get("urls", "")).strip()
    if not urls_input:
        raise HTTPException(status_code=400, detail="URL is required")

    url_list = list(dict.fromkeys(u.strip() for u in re.split(r"[,\n\r]+", urls_input) if u.strip()))

    from services.web_capture import capture_rendered_html
    results: list[dict[str, Any]] = []
    for page_url in url_list:
        try:
            html = await capture_rendered_html(page_url, settle_seconds=10)
            host = safe_stem(urlparse(page_url).hostname or "website")
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            target = root / "ingest" / "incoming" / f"web_{host}_{stamp}.html"
            target.write_text(html, encoding="utf-8")
            configure_logging(root).info("Captured URL source url=%s file=%s html_chars=%d", page_url, target.name, len(html))
            results.append({"url": page_url, "status": "saved", "file": target.name})
        except Exception as exc:
            configure_logging(root).exception("Could not fetch page url=%s", page_url)
            results.append({"url": page_url, "status": "failed", "error": str(exc)})

    return JSONResponse({
        "results": results,
        "total": len(results),
        "saved": sum(1 for r in results if r["status"] == "saved"),
        "failed": sum(1 for r in results if r["status"] == "failed"),
    })


async def _run_url_fetch_job(job_id: str, root: Path, urls: list[str]) -> None:
    from services.web_capture import capture_rendered_html

    job = _url_fetch_jobs[job_id]
    semaphore = asyncio.Semaphore(_URL_FETCH_CONCURRENCY)

    async def fetch_one(index: int, page_url: str) -> None:
        result = job["results"][index]
        async with semaphore:
            result["status"] = "fetching"
            try:
                html = await capture_rendered_html(page_url, settle_seconds=10)
                host = safe_stem(urlparse(page_url).hostname or "website")
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                target = root / "ingest" / "incoming" / f"web_{host}_{stamp}.html"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(html, encoding="utf-8")
                configure_logging(root).info(
                    "Captured URL source url=%s file=%s html_chars=%d", page_url, target.name, len(html)
                )
                result.update(status="saved", file=target.name)
            except Exception as exc:
                configure_logging(root).exception("Could not fetch page url=%s", page_url)
                result.update(status="failed", error=str(exc))
            finally:
                job["completed"] += 1
                job["saved"] = sum(item["status"] == "saved" for item in job["results"])
                job["failed"] = sum(item["status"] == "failed" for item in job["results"])

    try:
        await asyncio.gather(*(fetch_one(index, url) for index, url in enumerate(urls)))
    finally:
        job["finished"] = True


@app.post("/api/sources/url/jobs")
async def api_start_url_fetch_job(request: Request) -> dict[str, Any]:
    payload = await request.json()
    urls_input = str(payload.get("urls", "")).strip()
    if not urls_input:
        raise HTTPException(status_code=400, detail="URL is required")
    urls = list(dict.fromkeys(url.strip() for url in re.split(r"[,\n\r]+", urls_input) if url.strip()))
    job_id = uuid.uuid4().hex
    _url_fetch_jobs[job_id] = {
        "id": job_id, "total": len(urls), "completed": 0, "saved": 0, "failed": 0,
        "finished": False,
        "results": [{"url": url, "status": "queued"} for url in urls],
    }
    asyncio.create_task(_run_url_fetch_job(job_id, _get_root(), urls))
    return {key: value for key, value in _url_fetch_jobs[job_id].items() if key != "results"} | {"job_id": job_id}


@app.get("/api/sources/url/jobs/{job_id}")
def api_get_url_fetch_job(job_id: str) -> dict[str, Any]:
    job = _url_fetch_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="URL fetch job not found")
    return dict(job)


@app.post("/api/sources/text")
async def api_paste_text(request: Request) -> dict[str, Any]:
    root = _get_root()
    payload = await request.json()
    text = str(payload.get("text", "")).strip()
    if not text:
        raise HTTPException(status_code=400, detail="Source text cannot be empty")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    target = root / "ingest" / "incoming" / f"{stamp}.txt"
    target.write_text(text + "\n", encoding="utf-8")
    return {"status": "success", "file": target.name}


@app.get("/api/categories")
def api_get_categories() -> dict[str, Any]:
    root = _get_root()
    taxonomy = load_category_taxonomy(root)
    return {"taxonomy": taxonomy}


@app.post("/api/categories")
async def api_save_categories(request: Request) -> dict[str, Any]:
    root = _get_root()
    payload = await request.json()
    main_cat = str(payload.get("category", "")).strip()
    subcategories = payload.get("subcategories", [])

    taxonomy = load_category_taxonomy(root)
    canonical = next((name for name in taxonomy if name.casefold() == main_cat.casefold()), main_cat)
    children = taxonomy.setdefault(canonical, [])
    known = {item.casefold() for item in children}

    for sub in subcategories:
        s = str(sub).strip()
        if s and s.casefold() not in known:
            children.append(s)
            known.add(s.casefold())

    save_category_taxonomy(root, taxonomy)
    return {"status": "success", "taxonomy": taxonomy}


@app.delete("/api/categories")
async def api_delete_category(request: Request) -> dict[str, Any]:
    root = _get_root()
    payload = await request.json()
    category = str(payload.get("category", "")).strip()
    subcategory = str(payload.get("subcategory", "")).strip()

    taxonomy = load_category_taxonomy(root)
    if subcategory:
        if category in taxonomy:
            taxonomy[category] = [item for item in taxonomy[category] if item != subcategory]
    else:
        taxonomy.pop(category, None)
        deleted_defaults = load_deleted_default_categories(root)
        if category in ORGANIZATION_CATEGORIES and category not in deleted_defaults:
            deleted_defaults.append(category)
        save_category_taxonomy(root, taxonomy, deleted_defaults)
        return {"status": "success", "taxonomy": taxonomy}

    save_category_taxonomy(root, taxonomy)
    return {"status": "success", "taxonomy": taxonomy}


@app.get("/api/review")
def api_get_review() -> dict[str, Any]:
    root = _get_root()
    review_dir = root / "review"
    review_files = sorted(review_dir.glob("*.review.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    items = []
    for f in review_files:
        data = read_json(f, {})
        if isinstance(data, dict):
            source_name = str(data.get("source_file") or f.stem.removesuffix(".review"))
            archived_source = root / "ingest" / "archived" / Path(source_name).name
            items.append({
                "file": f.name,
                "source_file": source_name,
                "error": data.get("error", ""),
                "reason": data.get("reason", "Needs review"),
                "confidence": data.get("confidence"),
                "created_at": data.get("created_at", ""),
                "archived_exists": archived_source.exists(),
                "extraction": data.get("extraction"),
            })

    dismissed_dir = review_dir / "dismissed"
    dismissed_files = sorted(dismissed_dir.glob("*.review.json"), key=lambda p: p.stat().st_mtime, reverse=True) if dismissed_dir.exists() else []
    dismissed = [
        {"file": f.name, "source_file": read_json(f, {}).get("source_file", f.stem)}
        for f in dismissed_files
    ]
    return {"review": items, "dismissed": dismissed}


@app.post("/api/review/retry")
async def api_retry_review(request: Request) -> dict[str, Any]:
    root = _get_root()
    payload = await request.json()
    file_name = Path(str(payload.get("source_file", ""))).name
    archived_source = root / "ingest" / "archived" / file_name
    incoming_source = root / "ingest" / "incoming" / file_name

    if not archived_source.exists():
        raise HTTPException(status_code=404, detail="Archived source not found")

    # Reserve it before restore so the default-settings queue worker cannot race a retry override.
    reserve_processing_sources({file_name})
    try:
        shutil.copy2(archived_source, incoming_source)
        _prepare_source(incoming_source, root)
    except Exception as exc:
        release_processing_sources({file_name})
        raise HTTPException(status_code=422, detail=f"Could not prepare source: {exc}") from exc

    # Start from saved settings and apply only the per-retry overrides, without
    # mutating the persisted settings used by the background scanner.
    settings = dict(_get_settings())
    if payload.get("ollama_url"):
        settings["ollama_url"] = str(payload["ollama_url"]).rstrip("/")
    if payload.get("ollama_model") is not None:
        settings["selected_model"] = str(payload["ollama_model"]).strip()
    if payload.get("ollama_api_key") is not None:
        settings["ollama_api_key"] = str(payload["ollama_api_key"]).strip()
    if payload.get("minimum_confidence") is not None:
        try:
            settings["minimum_extraction_confidence"] = float(payload["minimum_confidence"])
        except (ValueError, TypeError):
            pass

    # Optional manual field overrides (e.g. a company name entered by hand when
    # the extraction missed it). These are passed into the extraction run.
    manual_fields: dict[str, Any] = {}
    company_name = str(payload.get("company_name") or "").strip()
    if company_name:
        manual_fields["company_name"] = company_name

    _launch_processing(settings, {file_name}, manual_fields or None)
    return {"status": "queued", "source_file": file_name}


@app.post("/api/review/retry-all")
async def api_retry_review_all(request: Request) -> dict[str, Any]:
    """Retry every item currently in the review queue in one sweep.

    Restores each review item's original source file to incoming, then runs the
    extraction pipeline over all of them together. Per-retry overrides apply to
    every item in the sweep.
    """
    root = _get_root()
    payload = await request.json()

    review_dir = root / "review"
    review_files = sorted(review_dir.glob("*.review.json"), key=lambda p: p.stat().st_mtime, reverse=True)

    # Start from saved settings and apply per-retry overrides.
    settings = dict(_get_settings())
    if payload.get("ollama_url"):
        settings["ollama_url"] = str(payload["ollama_url"]).rstrip("/")
    if payload.get("ollama_model") is not None:
        settings["selected_model"] = str(payload["ollama_model"]).strip()
    if payload.get("ollama_api_key") is not None:
        settings["ollama_api_key"] = str(payload["ollama_api_key"]).strip()
    if payload.get("minimum_confidence") is not None:
        try:
            settings["minimum_extraction_confidence"] = float(payload["minimum_confidence"])
        except (ValueError, TypeError):
            pass

    manual_fields: dict[str, Any] = {}
    company_name = str(payload.get("company_name") or "").strip()
    if company_name:
        manual_fields["company_name"] = company_name

    # Restore every review source back to incoming so it can be re-processed.
    source_files: list[str] = []
    for f in review_files:
        data = read_json(f, {})
        if not isinstance(data, dict):
            continue
        source_name = str(data.get("source_file") or f.stem.removesuffix(".review"))
        archived_source = root / "ingest" / "archived" / Path(source_name).name
        incoming_source = root / "ingest" / "incoming" / Path(source_name).name
        if archived_source.exists():
            source_basename = Path(source_name).name
            reserve_processing_sources({source_basename})
            try:
                shutil.copy2(archived_source, incoming_source)
                _prepare_source(incoming_source, root)
            except Exception:
                configure_logging(root).exception("Could not prepare retry source=%s", source_name)
                release_processing_sources({source_basename})
                continue
            source_files.append(source_basename)

    if not source_files:
        return {
            "status": "success",
            "outcome": {"processed": 0, "review": 0, "errors": 0, "merged": 0, "new": 0},
        }

    _launch_processing(settings, set(source_files), manual_fields or None)
    return {"status": "queued", "source_files": source_files}


@app.post("/api/review/dismiss")
async def api_dismiss_review(request: Request) -> dict[str, Any]:
    root = _get_root()
    payload = await request.json()
    review_file_name = str(payload.get("file", ""))
    review_file = root / "review" / review_file_name
    if not review_file.exists():
        raise HTTPException(status_code=404, detail="Review item not found")

    dismissed_dir = root / "review" / "dismissed"
    dismissed_dir.mkdir(parents=True, exist_ok=True)
    destination = dismissed_dir / review_file.name
    if destination.exists():
        destination = dismissed_dir / f"{review_file.stem}_{datetime.now().strftime('%Y%m%d%H%M%S%f')}{review_file.suffix}"
    shutil.move(str(review_file), str(destination))
    return {"status": "success"}


@app.get("/api/settings")
def api_get_settings() -> dict[str, Any]:
    settings = _get_settings()
    root = _get_root()
    ollama_url = str(settings.get("ollama_url", "http://localhost:11434"))
    api_key = str(settings.get("ollama_api_key", ""))
    selected_model = str(settings.get("selected_model", ""))
    is_local = is_local_url(ollama_url)
    is_cloud = is_ollama_cloud_url(ollama_url)

    models: list[str] = []
    error_msg = ""
    if not is_cloud:
        try:
            models = list_models(ollama_url, api_key)
        except Exception as exc:
            error_msg = str(exc)

    lan = _lan_ip()
    return {
        "data_path": str(settings.get("data_path", "~/LeadData")),
        "ollama_url": ollama_url,
        "api_url": ollama_url,
        "selected_model": selected_model,
        "ollama_api_key": api_key,
        "api_key": api_key,
        "is_local_url": is_local,
        "is_cloud_url": is_cloud,
        "available_models": models,
        "models_error": error_msg,
        "lan_ip": lan if not lan.startswith("127.") else "",
        "lan_port": 8502,
        "page_size": int(settings.get("page_size", 50)),
        "resolved_root": str(root),
        "folders": list_data_folders(settings),
        "active_folder_id": str(settings.get("active_folder_id") or ""),
        "version": APP_VERSION,
        "ai_model": selected_model,
        "scanner_running": is_scanner_running(),
        "processing_job": get_processing_job(),
    }


@app.post("/api/settings/test-connection")
@app.post("/api/settings/test-ollama")
async def api_test_ollama(request: Request) -> dict[str, Any]:
    payload = await request.json()
    url = str(payload.get("api_url", payload.get("ollama_url", ""))).strip().rstrip("/")
    api_key = str(payload.get("api_key", payload.get("ollama_api_key", ""))).strip()
    is_local = is_local_url(url)
    is_cloud = is_ollama_cloud_url(url)
    if is_cloud:
        return {
            "success": True,
            "is_local": is_local,
            "is_cloud": True,
            "models": [],
            "message": "Ollama Cloud endpoint detected. Enter your cloud model name (e.g. gemma4:31b-cloud)."
        }
    try:
        models = list_models(url, api_key)
        return {
            "success": True,
            "is_local": is_local,
            "is_cloud": False,
            "models": models,
            "message": f"Successfully connected! Found {len(models)} model(s)."
        }
    except Exception as exc:
        return {
            "success": False,
            "is_local": is_local,
            "is_cloud": False,
            "models": [],
            "error": str(exc),
            "message": f"Connection failed: {exc}"
        }


@app.post("/api/settings")
async def api_save_settings(request: Request) -> dict[str, Any]:
    payload = await request.json()
    settings = _get_settings()

    if "data_path" in payload:
        new_data_path = str(payload["data_path"]).strip()
        if new_data_path != str(settings.get("data_path", "")):
            settings["active_folder_id"] = ""
        settings["data_path"] = new_data_path
    if "api_url" in payload or "ollama_url" in payload:
        configured_url = str(payload.get("api_url", payload.get("ollama_url", ""))).rstrip("/")
        settings["api_url"] = configured_url
        settings["ollama_url"] = configured_url
    if "selected_model" in payload:
        settings["selected_model"] = str(payload["selected_model"]).strip()
    if "api_key" in payload or "ollama_api_key" in payload:
        key = str(payload.get("api_key", payload.get("ollama_api_key", ""))).strip()
        settings["api_key"] = key
        settings["ollama_api_key"] = key
        save_ollama_api_key(key)
    if "page_size" in payload:
        try:
            settings["page_size"] = int(payload["page_size"])
        except (ValueError, TypeError):
            pass

    save_settings(settings)
    _invalidate_caches()
    return {"status": "success", "settings": settings}


@app.post("/api/process-incoming")
async def api_process_incoming(request: Request) -> dict[str, Any]:
    """Manually trigger processing of all files in the ingestion incoming folder.

    Restores any review items whose sources are still in archived back to incoming,
    then runs the extraction pipeline over everything waiting there.
    """
    root = _get_root()
    settings = _get_settings()

    incoming = root / "ingest" / "incoming"
    archived = root / "ingest" / "archived"

    # Re-queue any review items whose sources are still in archived so they get retried.
    review_dir = root / "review"
    for review_file in review_dir.glob("*.review.json"):
        data = read_json(review_file, {})
        if not isinstance(data, dict):
            continue
        source_name = str(data.get("source_file") or review_file.stem.removesuffix(".review"))
        archived_source = archived / Path(source_name).name
        incoming_source = incoming / Path(source_name).name
        if archived_source.exists() and not incoming_source.exists():
            shutil.copy2(archived_source, incoming_source)

    _launch_processing(settings, None, None)
    return {"status": "queued"}


@app.post("/api/rebuild")
def api_rebuild_companies() -> dict[str, Any]:
    settings = _get_settings()
    root = _get_root()
    extracted_count, reviewed_count = rebuild_companies(root, settings["duplicate_weights"])
    _invalidate_caches()
    return {"status": "success", "extracted": extracted_count, "reviewed": reviewed_count}


INDEX_HTML_PATH = Path(__file__).resolve().parent / "static" / "index.html"

@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    if not INDEX_HTML_PATH.exists():
        return HTMLResponse("<h1>Lead Extractor is running.</h1><p>Static index.html not found.</p>")
    return HTMLResponse(INDEX_HTML_PATH.read_text(encoding="utf-8"))


def main() -> None:
    print("Open Lead Extractor in this browser at http://127.0.0.1:8502")
    uvicorn.run("server:app", host="0.0.0.0", port=8502, reload=False)


if __name__ == "__main__":
    main()
