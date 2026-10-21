"""File-backed storage. All business data lives outside the source repository."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from rapidfuzz import fuzz

from models.lead import ORGANIZATION_CATEGORIES, empty_crm, normalize_contacts, normalize_opening_hours, normalize_social_links

APP_ROOT = Path(__file__).resolve().parents[1]
SETTINGS_PATH = APP_ROOT / "config" / "settings.json"
SECRETS_PATH = APP_ROOT / "config" / "secrets.json"
TAXONOMY_FILENAME = "categories.json"

DEFAULT_SETTINGS: dict[str, Any] = {
    "data_path": "~/LeadData",
    "ollama_url": "http://localhost:11434",
    "selected_model": "",
    "scan_interval_minutes": 1,
    "minimum_extraction_confidence": 0.35,
    "raw_llm_log_max_bytes": 10000000,
    "raw_llm_log_backups": 5,
    "duplicate_weights": {
        "email": 100, "phone": 100, "website_domain": 95,
        "address": 80, "contact": 70, "company_name": 30, "city": 20,
    },
    "custom_statuses": [],
}


def load_settings() -> dict[str, Any]:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not SETTINGS_PATH.exists():
        save_settings(DEFAULT_SETTINGS)
    try:
        loaded = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        loaded = {}
    settings = {**DEFAULT_SETTINGS, **loaded}
    secrets = read_json(SECRETS_PATH, {})
    settings["ollama_api_key"] = str(secrets.get("ollama_api_key", "")) if isinstance(secrets, dict) else ""
    settings["duplicate_weights"] = {
        **DEFAULT_SETTINGS["duplicate_weights"],
        **(loaded.get("duplicate_weights") or {}),
    }
    settings["data_path"] = str(Path(os.path.expandvars(settings["data_path"])).expanduser())
    return settings


def save_settings(settings: dict[str, Any]) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    portable = dict(settings)
    portable.pop("ollama_api_key", None)
    configured_path = Path(os.path.expandvars(str(portable.get("data_path", "")))).expanduser()
    try:
        relative_to_home = configured_path.resolve().relative_to(Path.home().resolve())
        portable["data_path"] = "~/" + relative_to_home.as_posix()
    except (ValueError, OSError):
        portable["data_path"] = str(configured_path)
    temporary = SETTINGS_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(portable, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(SETTINGS_PATH)


def save_ollama_api_key(api_key: str) -> None:
    """Keep remote Ollama credentials outside the tracked settings file."""
    if api_key.strip():
        write_json(SECRETS_PATH, {"ollama_api_key": api_key.strip()})
    else:
        SECRETS_PATH.unlink(missing_ok=True)


def data_root(settings: dict[str, Any]) -> Path:
    root = Path(os.path.expandvars(str(settings["data_path"]))).expanduser().resolve()
    for folder in (
        "ingest/incoming", "ingest/archived", "ingest/parsed", "extracted", "companies", "crm",
        "logs", "backups", "review",
    ):
        (root / folder).mkdir(parents=True, exist_ok=True)
    return root


def load_category_taxonomy(root: Path) -> dict[str, list[str]]:
    """Load only user-created category/subcategory pairs; new installs start empty."""
    saved = read_json(root / TAXONOMY_FILENAME, {})
    categories = saved.get("categories", {}) if isinstance(saved, dict) and "categories" in saved else saved
    deleted_defaults = saved.get("deleted_defaults", []) if isinstance(saved, dict) and "categories" in saved else []
    deleted_defaults = {str(item).casefold() for item in deleted_defaults if isinstance(item, str)} if isinstance(deleted_defaults, list) else set()
    taxonomy = {name: [] for name in ORGANIZATION_CATEGORIES if name.casefold() not in deleted_defaults}
    if not isinstance(categories, dict):
        return taxonomy
    for raw_name, raw_subcategories in categories.items():
        name = str(raw_name).strip()
        if not name or name.casefold() == "unknown":
            continue
        canonical = next((
            entry for entry in taxonomy
            if entry.casefold() == name.casefold()
            or entry.replace("_", " ").casefold() == name.replace("_", " ").casefold()
        ), name.replace("_", " "))
        values = raw_subcategories if isinstance(raw_subcategories, list) else []
        merged = taxonomy.setdefault(canonical, [])
        for value in values:
            subcategory = str(value).strip()
            if subcategory and subcategory.casefold() not in {item.casefold() for item in merged}:
                merged.append(subcategory)
    return taxonomy


def load_deleted_default_categories(root: Path) -> list[str]:
    saved = read_json(root / TAXONOMY_FILENAME, {})
    if isinstance(saved, dict) and isinstance(saved.get("deleted_defaults"), list):
        return [str(item) for item in saved["deleted_defaults"] if str(item) in ORGANIZATION_CATEGORIES]
    return []


def save_category_taxonomy(
    root: Path, taxonomy: dict[str, list[str]], deleted_defaults: list[str] | None = None,
) -> None:
    if deleted_defaults is None:
        deleted_defaults = load_deleted_default_categories(root)
    # Adding a built-in category back through the manager makes it available again.
    present = {name.casefold() for name in taxonomy}
    deleted_defaults = [
        name for name in deleted_defaults
        if name.casefold() not in present and name in ORGANIZATION_CATEGORIES
    ]
    write_json(root / TAXONOMY_FILENAME, {
        "version": 1,
        "categories": taxonomy,
        "deleted_defaults": deleted_defaults,
    })


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp_path.replace(path)


def safe_stem(value: str) -> str:
    return re.sub(r"[^\w-]+", "_", value, flags=re.UNICODE).strip("_") or "company"


def company_files(root: Path) -> list[Path]:
    return sorted((root / "companies").glob("*.json"))


def load_companies(root: Path) -> list[dict[str, Any]]:
    companies = []
    for path in company_files(root):
        record = read_json(path)
        if isinstance(record, dict) and record.get("company_id"):
            companies.append(record)
    return companies


def _values(record: dict[str, Any], key: str) -> set[str]:
    raw = record.get(key, [])
    values = raw if isinstance(raw, list) else [raw]
    return {str(item).strip().casefold() for item in values if str(item).strip()}


def _domain(website: str) -> str:
    value = website.strip().casefold()
    value = re.sub(r"^https?://", "", value)
    return value.split("/", 1)[0].removeprefix("www.").split(":", 1)[0]


def _contact_names(record: dict[str, Any]) -> set[str]:
    return {
        contact["name"].casefold()
        for contact in normalize_contacts(record.get("contacts", []))
        if contact.get("name")
    }


def duplicate_score_details(incoming: dict[str, Any], existing: dict[str, Any], weights: dict[str, int]) -> dict[str, Any]:
    score = 0
    signals: dict[str, dict[str, Any]] = {}
    for field, weight in (("emails", "email"), ("phones", "phone"), ("addresses", "address")):
        matches = sorted(_values(incoming, field) & _values(existing, field))
        points = int(weights.get(weight, 0)) if matches else 0
        score += points
        signals[weight] = {"matched_values": matches, "points": points}
    incoming_domain = _domain(str(incoming.get("website", "")))
    existing_domain = _domain(str(existing.get("website", "")))
    website_match = bool(incoming_domain and incoming_domain == existing_domain)
    website_points = int(weights.get("website_domain", 0)) if website_match else 0
    score += website_points
    signals["website_domain"] = {"incoming": incoming_domain, "existing": existing_domain, "points": website_points}
    contact_matches = sorted(_contact_names(incoming) & _contact_names(existing))
    contact_points = int(weights.get("contact", 0)) if contact_matches else 0
    score += contact_points
    signals["contact"] = {"matched_names": contact_matches, "points": contact_points}
    name_a = str(incoming.get("company_name", ""))
    name_b = str(existing.get("company_name", ""))
    name_similarity = fuzz.token_set_ratio(name_a, name_b) if name_a and name_b else 0
    name_points = int(weights.get("company_name", 0)) if name_similarity >= 88 else 0
    score += name_points
    signals["company_name"] = {"similarity": name_similarity, "threshold": 88, "points": name_points}
    city_a = _values(incoming, "locations")
    city_b = _values(existing, "locations")
    city_matches = sorted(city_a & city_b)
    city_points = int(weights.get("city", 0)) if city_matches else 0
    score += city_points
    signals["city"] = {"matched_values": city_matches, "points": city_points}
    return {"score": score, "signals": signals}


def duplicate_score(incoming: dict[str, Any], existing: dict[str, Any], weights: dict[str, int]) -> int:
    return int(duplicate_score_details(incoming, existing, weights)["score"])


def _merge_unique(old: list[Any], new: list[Any]) -> list[Any]:
    result = list(old)
    seen = {json.dumps(item, sort_keys=True, ensure_ascii=False).casefold() for item in result}
    for item in new:
        if item in (None, "", [], {}):
            continue
        marker = json.dumps(item, sort_keys=True, ensure_ascii=False).casefold()
        if marker not in seen:
            result.append(item)
            seen.add(marker)
    return result


def _restore_orphaned_crm(root: Path, company: dict[str, Any]) -> None:
    """Attach CRM history from prior rebuild-generated IDs to this company."""
    company_id = str(company.get("company_id") or "")
    name_prefix = f"{safe_stem(str(company.get('company_name') or ''))}-"
    target = root / "crm" / f"{safe_stem(company_id)}.json"
    current = read_json(target, empty_crm(company_id))
    if not isinstance(current, dict):
        current = empty_crm(company_id)

    history: list[tuple[float, dict[str, Any]]] = []
    for path in (root / "crm").glob("*.json"):
        suffix = path.stem[len(name_prefix):] if path.stem.startswith(name_prefix) else ""
        if suffix.isdigit() and path != target:
            record = read_json(path, {})
            if isinstance(record, dict):
                history.append((path.stat().st_mtime, record))
    if not history:
        return
    history.sort(key=lambda item: item[0], reverse=True)

    changed = False
    current.setdefault("company_id", company_id)
    for field, default in (("status", "New"), ("rating", ""), ("last_contacted", ""), ("next_followup", "")):
        if current.get(field) in (None, "", default):
            previous = next((record.get(field) for _, record in history if record.get(field) not in (None, "", default)), None)
            if previous is not None:
                current[field] = previous
                changed = True

    notes: list[Any] = current.get("notes", []) if isinstance(current.get("notes"), list) else []
    for _, record in reversed(history):
        previous_notes = record.get("notes", [])
        if isinstance(previous_notes, list):
            notes = _merge_unique(notes, previous_notes)
    if notes != current.get("notes", []):
        current["notes"] = notes
        changed = True

    if changed:
        backup = root / "backups" / f"crm-before-recovery-{safe_stem(company_id)}.json"
        if target.exists() and not backup.exists():
            shutil.copy2(target, backup)
        write_json(target, current)


def merge_company(root: Path, extracted: dict[str, Any], source_file: str, weights: dict[str, int]) -> tuple[str, int, bool, list[dict[str, Any]]]:
    extracted = dict(extracted)
    extracted["contacts"] = normalize_contacts(extracted.get("contacts", []))
    extracted["social_links"] = normalize_social_links(extracted.get("social_links", []))
    extracted["opening_hours"] = normalize_opening_hours(extracted.get("opening_hours", []))
    companies = load_companies(root)
    candidate_audit = []
    source_match = None
    for candidate in companies:
        sources = candidate.get("sources", [])
        sources = sources if isinstance(sources, list) else [sources]
        same_source = source_file in {str(source) for source in sources}
        if same_source and source_match is None:
            source_match = candidate
        details = duplicate_score_details(extracted, candidate, weights)
        score = int(details["score"])
        decision = "definite" if score >= 100 else "likely" if score >= 70 else "candidate" if score >= 40 else "new"
        candidate_audit.append({
            "company_id": candidate.get("company_id", ""),
            "company_name": candidate.get("company_name", ""),
            "score": score,
            "decision": decision,
            "same_source": same_source,
            "signals": details["signals"],
        })
    candidate_audit.sort(key=lambda item: (item["same_source"], item["score"]), reverse=True)
    best = None
    if source_match is not None:
        best = (source_match, duplicate_score(extracted, source_match, weights))
    elif candidate_audit:
        best_info = candidate_audit[0]
        best = (next(c for c in companies if str(c.get("company_id")) == str(best_info["company_id"])), best_info["score"])

    now = datetime.now().astimezone().isoformat(timespec="seconds")
    definite = bool(best and (best[0] is source_match or best[1] >= 100))
    likely = bool(best and best[1] >= 70)
    if definite or likely:
        record, score = best
        record["company_name"] = record.get("company_name") or extracted.get("company_name", "")
        current_category = str(record.get("organization_category") or "")
        extracted_category = str(extracted.get("organization_category") or "")
        if current_category.casefold() in {"", "unknown"} and extracted_category.casefold() not in {"", "unknown"}:
            current_category = extracted_category
            record["organization_category"] = extracted_category
        extracted_subcategory = str(extracted.get("subcategory") or "")
        if not record.get("subcategory") and extracted_subcategory and extracted_category == current_category:
            record["subcategory"] = extracted_subcategory
        record["website"] = record.get("website") or extracted.get("website", "")
        for key in ("emails", "phones", "addresses", "locations"):
            record[key] = _merge_unique(record.get(key, []), extracted.get(key, []))
        record["contacts"] = normalize_contacts(
            normalize_contacts(record.get("contacts", [])) + extracted["contacts"]
        )
        record["social_links"] = normalize_social_links(
            normalize_social_links(record.get("social_links", [])) + extracted["social_links"]
        )
        record["opening_hours"] = normalize_opening_hours(
            normalize_opening_hours(record.get("opening_hours", [])) + extracted["opening_hours"]
        )
        record["notes"] = _merge_unique(record.get("notes", []), extracted.get("notes", []))
        record["sources"] = _merge_unique(record.get("sources", []), [source_file])
        record["last_seen"] = now
        write_json(root / "companies" / f"{safe_stem(record['company_id'])}.json", record)
        return str(record["company_id"]), score, True, candidate_audit

    company_id = f"{safe_stem(extracted['company_name'])}-{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
    record = {
        "company_id": company_id,
        "company_name": extracted["company_name"],
        "organization_category": extracted.get("organization_category", ""),
        "subcategory": extracted.get("subcategory", ""),
        "website": extracted.get("website", ""),
        "emails": extracted.get("emails", []),
        "phones": extracted.get("phones", []),
        "addresses": extracted.get("addresses", []),
        "locations": extracted.get("locations", []),
        "contacts": extracted.get("contacts", []),
        "social_links": extracted.get("social_links", []),
        "opening_hours": extracted.get("opening_hours", []),
        "notes": extracted.get("notes", []),
        "sources": [source_file],
        "first_seen": now,
        "last_seen": now,
        "schema_version": 1,
    }
    write_json(root / "companies" / f"{safe_stem(company_id)}.json", record)
    crm_path = root / "crm" / f"{safe_stem(company_id)}.json"
    if not crm_path.exists():
        write_json(crm_path, empty_crm(company_id))
    return company_id, best[1] if best else 0, False, candidate_audit


def rebuild_companies(
    root: Path,
    weights: dict[str, int],
    progress_callback: Callable[[float, str], None] | None = None,
) -> tuple[int, int]:
    """Rebuild company records from saved JSON without risking a partial deletion."""
    # Import lazily to avoid the storage <-> pipeline import cycle.
    from services.pipeline import configure_logging

    logger = configure_logging(root)
    extracted_records: list[tuple[Path, dict[str, Any], str]] = []
    reviewed = 0
    extracted_dir = root / "extracted"
    extraction_paths = sorted(extracted_dir.glob("*.extracted.json"))
    total = len(extraction_paths)
    for index, path in enumerate(extraction_paths, start=1):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            reviewed += 1
            logger.warning("Rebuild skipped invalid extraction JSON path=%s error=%s", path, exc)
            continue
        if not isinstance(item, dict) or not item.get("company_name"):
            reviewed += 1
            logger.warning("Rebuild skipped extraction with no company_name path=%s", path)
            continue
        source_name = str(item.get("_source_file", path.stem.replace(".extracted", "")))
        item = dict(item)
        item.pop("_source_file", None)
        item.pop("_confidence", None)
        item.pop("_model", None)
        item.pop("_processed_at", None)
        for key in ("emails", "phones", "addresses", "locations", "contacts", "social_links", "opening_hours", "notes"):
            value = item.get(key, [])
            item[key] = value if isinstance(value, list) else ([] if value is None else [value])
        item["contacts"] = normalize_contacts(item["contacts"])
        item["social_links"] = normalize_social_links(item["social_links"])
        item["opening_hours"] = normalize_opening_hours(item["opening_hours"])
        extracted_records.append((path, item, source_name))
        if progress_callback:
            progress_callback(0.2 * index / max(total, 1), f"Reading saved extractions ({index}/{total})")

    # Build under a temporary data root first. If a record causes a serialization
    # or merge error, the currently visible company records are left intact.
    existing_companies = load_companies(root)
    extraction_sources = {source_name for _, _, source_name in extracted_records}
    stage_root = Path(tempfile.mkdtemp(prefix="rebuild-", dir=root / "backups"))
    try:
        (stage_root / "companies").mkdir()
        (stage_root / "crm").mkdir()
        # Seed only companies that are represented by saved extractions. This
        # keeps their IDs stable so CRM records remain attached across rebuilds.
        for company in existing_companies:
            sources = company.get("sources", [])
            sources = sources if isinstance(sources, list) else [sources]
            if extraction_sources.intersection(str(source) for source in sources):
                write_json(stage_root / "companies" / f"{safe_stem(str(company['company_id']))}.json", company)
        if progress_callback:
            progress_callback(0.2, f"Prepared {len(extracted_records)} extraction(s); rebuilding company records")
        for index, (source_path, item, source_name) in enumerate(extracted_records, start=1):
            try:
                merge_company(stage_root, item, source_name, weights)
                if progress_callback:
                    progress_callback(0.2 + 0.75 * index / max(len(extracted_records), 1), f"Merging companies ({index}/{len(extracted_records)})")
            except Exception:
                logger.exception("Rebuild failed for extraction path=%s; existing company records were preserved", source_path)
                raise

        current_companies = root / "companies"
        backup_companies = stage_root / "previous-companies"
        if current_companies.exists():
            current_companies.replace(backup_companies)
        try:
            (stage_root / "companies").replace(current_companies)
        except Exception:
            if backup_companies.exists() and not current_companies.exists():
                backup_companies.replace(current_companies)
            raise

        for company in load_companies(root):
            crm_path = root / "crm" / f"{safe_stem(company['company_id'])}.json"
            if not crm_path.exists():
                write_json(crm_path, empty_crm(str(company["company_id"])))
            _restore_orphaned_crm(root, company)
        count = len(extracted_records)
        logger.info("Rebuild completed extractions=%d skipped=%d", count, reviewed)
        # The staged output and old company snapshot are only needed until the
        # commit succeeds; don't accumulate duplicate files on every rebuild.
        try:
            shutil.rmtree(stage_root)
        except OSError:
            logger.warning("Rebuild succeeded but temporary snapshot could not be removed path=%s", stage_root, exc_info=True)
    except Exception:
        logger.exception("Rebuild failed; inspect staging and rollback data path=%s", stage_root)
        raise
    return count, reviewed
