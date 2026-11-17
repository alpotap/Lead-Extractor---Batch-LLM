"""FastAPI web server and single-page application for Lead Extractor."""

from __future__ import annotations

import json
import logging
import math
import re
import shutil
import socket
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any, AsyncGenerator
from urllib.parse import urlparse

import uvicorn
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
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
from services.pipeline import configure_logging, process_incoming, start_background_scanner
from services.storage import (
    company_files,
    data_root,
    load_category_taxonomy,
    load_deleted_default_categories,
    load_settings,
    read_json,
    rebuild_companies,
    safe_stem,
    save_category_taxonomy,
    save_ollama_api_key,
    save_settings,
    write_json,
)
from services.web_capture import capture_rendered_html

app = FastAPI(title="Lead Extractor", version="0.3.0")
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


@app.on_event("startup")
def on_startup() -> None:
    settings = _get_settings()
    root = _get_root()
    start_background_scanner(settings)


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
        "ollama_url": ollama_url,
        "selected_model": selected_model,
        "is_local_url": is_local,
        "is_cloud_url": is_cloud,
        "available_models": models,
        "lan_ip": lan if not lan.startswith("127.") else "",
        "lan_port": 8502,
        "total_companies": len(companies),
        "review_count": len(review_files),
        "data_path": str(root),
    }


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
                    emails_txt = " ".join(full_rec.get("emails", []) if isinstance(full_rec.get("emails", []), list) else [])
                    phones_txt = " ".join(full_rec.get("phones", []) if isinstance(full_rec.get("phones", []), list) else [])
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

    return {
        "company": company,
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
        "opening_hours", "notes", "archived",
    ]:
        if field in payload:
            existing[field] = payload[field]

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
    outcome = process_incoming(settings, only_names={source_name})
    _invalidate_caches()
    return {"status": "success", "outcome": outcome}


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


async def _url_fetch_progress(urls: list[str]) -> AsyncGenerator[dict[str, Any], None]:
    """Yield progress updates as each URL is processed."""
    from services.web_capture import capture_rendered_html
    results: list[dict[str, Any]] = []
    root = data_root(load_settings())
    for i, page_url in enumerate(urls, start=1):
        yield {"type": "progress", "index": i, "total": len(urls), "url": page_url}
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
    yield {"type": "complete", "results": results, "total": len(urls), "saved": sum(1 for r in results if r["status"] == "saved"), "failed": sum(1 for r in results if r["status"] == "failed")}


@app.post("/api/sources/url")
async def api_fetch_url(request: Request) -> StreamingResponse:
    root = _get_root()
    payload = await request.json()
    urls_input = str(payload.get("urls", "")).strip()
    if not urls_input:
        raise HTTPException(status_code=400, detail="URL is required")

    url_list = list(dict.fromkeys(u.strip() for u in re.split(r"[,\n\r]+", urls_input) if u.strip()))
    return StreamingResponse(_url_fetch_progress(url_list), media_type="application/json")


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

    if not incoming_source.exists():
        shutil.copy2(archived_source, incoming_source)

    settings = _get_settings()
    outcome = process_incoming(settings, only_names={file_name})
    _invalidate_caches()
    return {"status": "success", "outcome": outcome}


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
        "selected_model": selected_model,
        "ollama_api_key": api_key,
        "is_local_url": is_local,
        "is_cloud_url": is_cloud,
        "available_models": models,
        "models_error": error_msg,
        "lan_ip": lan if not lan.startswith("127.") else "",
        "lan_port": 8502,
        "page_size": int(settings.get("page_size", 50)),
        "resolved_root": str(root),
    }


@app.post("/api/settings/test-ollama")
async def api_test_ollama(request: Request) -> dict[str, Any]:
    payload = await request.json()
    url = str(payload.get("ollama_url", "")).strip().rstrip("/")
    api_key = str(payload.get("ollama_api_key", "")).strip()
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
        settings["data_path"] = str(payload["data_path"]).strip()
    if "ollama_url" in payload:
        settings["ollama_url"] = str(payload["ollama_url"]).rstrip("/")
    if "selected_model" in payload:
        settings["selected_model"] = str(payload["selected_model"]).strip()
    if "ollama_api_key" in payload:
        key = str(payload["ollama_api_key"]).strip()
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
    uvicorn.run("server:app", host="0.0.0.0", port=8502, reload=False)


if __name__ == "__main__":
    main()
