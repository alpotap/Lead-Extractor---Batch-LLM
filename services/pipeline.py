"""Incoming-file processing and periodic local scanner."""

from __future__ import annotations

import logging
import logging.handlers
import json
import re
import shutil
import threading
import time
import codecs
from html.parser import HTMLParser
from datetime import datetime
import filecmp
from pathlib import Path
from typing import Any
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from models.lead import (
    EXTRACTION_TEMPLATE,
    ORGANIZATION_CATEGORIES,
    normalize_contacts,
    normalize_opening_hours,
    normalize_social_links,
)
from services.ollama import extract_lead, is_ollama_cloud_url, is_openai_compatible_url, list_models
from services.storage import data_root, load_category_taxonomy, merge_company, write_json

SUPPORTED_EXTENSIONS = {".txt", ".html", ".htm", ".md"}
_scanner_lock = threading.Lock()
_scanner_started = False
_watch_interval_seconds = 0.5
_scanner_pause_requested = threading.Event()
_scanner_paused = threading.Event()
_scanner_wake_event = threading.Event()
_stable_observations: dict[str, tuple[int, int, int]] = {}
_reserved_sources_lock = threading.Lock()
_reserved_sources: set[str] = set()

# Guards the extraction run itself so the periodic scanner and manual
# triggers never process the same files at the same time.
_processing_lock = threading.Lock()
# Tracks the current/last extraction run so the UI can show live progress.
_job_lock = threading.Lock()
_processing_job: dict[str, Any] | None = None


def is_scanner_running() -> bool:
    """Return whether the background ingestion scanner has been started."""
    return _scanner_started


def pause_background_scanner(timeout_seconds: float = 30.0) -> bool:
    """Pause source watchers and hold the extraction lock during data replacement."""
    if not _scanner_started:
        return False
    _scanner_paused.clear()
    _scanner_pause_requested.set()
    _scanner_wake_event.set()
    if not _scanner_paused.wait(timeout_seconds):
        _scanner_pause_requested.clear()
        _scanner_wake_event.set()
        raise TimeoutError("The source watcher did not pause in time")
    if not _processing_lock.acquire(timeout=timeout_seconds):
        _scanner_pause_requested.clear()
        _scanner_paused.clear()
        _scanner_wake_event.set()
        raise TimeoutError("The extraction worker did not stop in time")
    return True


def resume_background_scanner(paused: bool) -> None:
    """Release the extraction lock and resume watchers after data replacement."""
    if not paused:
        return
    _processing_lock.release()
    _scanner_pause_requested.clear()
    _scanner_paused.clear()
    _scanner_wake_event.set()


def get_processing_job() -> dict[str, Any] | None:
    """Return a snapshot of the current/last extraction run for progress reporting."""
    with _job_lock:
        return dict(_processing_job) if _processing_job else None


def reserve_processing_sources(names: set[str]) -> None:
    with _reserved_sources_lock:
        _reserved_sources.update(names)


def release_processing_sources(names: set[str]) -> None:
    with _reserved_sources_lock:
        _reserved_sources.difference_update(names)


def _start_processing_job(total_files: int) -> None:
    global _processing_job
    with _job_lock:
        _processing_job = {
            "id": datetime.now().strftime("%Y%m%d%H%M%S%f"),
            "total": total_files,
            "processed": 0,
            "current": "",
            "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "finished": False,
            "result": None,
        }


def _update_processing_job(current_file: str, processed: int, total_files: int) -> None:
    with _job_lock:
        job = _processing_job
        if job is not None:
            job["current"] = current_file
            job["processed"] = processed
            job["total"] = total_files


def _finish_processing_job(counts: dict[str, int]) -> None:
    with _job_lock:
        job = _processing_job
        if job is not None:
            job["finished"] = True
            job["current"] = ""
            job["result"] = dict(counts)


class _VisibleHTMLText(HTMLParser):
    """Extract readable page text while retaining link targets for inspection."""

    _SKIP_TAGS = {"script", "style", "noscript", "svg", "template", "iframe"}
    _BLOCK_TAGS = {
        "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt",
        "figcaption", "figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6",
        "header", "hr", "li", "main", "nav", "ol", "p", "section", "table", "td",
        "th", "tr", "ul",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0
        self._links: list[str | None] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attrs_map = dict(attrs)
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag in self._BLOCK_TAGS:
            self.parts.append("\n")
        if tag == "img" and attrs_map.get("alt"):
            self.parts.extend([" ", attrs_map["alt"] or "", " "])
        if tag == "a":
            href = (attrs_map.get("href") or "").strip()
            self._links.append(href if href and not href.lower().startswith(("javascript:", "data:")) else None)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag == "a" and self._links:
            href = self._links.pop()
            if href:
                self.parts.extend([" [link: ", href, "] "])
        if tag in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth and data.strip():
            self.parts.extend([" ", data.strip(), " "])

    def text(self) -> str:
        text = re.sub(r"[\t\f\v ]+", " ", "".join(self.parts))
        text = re.sub(r" *\n *", "\n", text)
        return re.sub(r"\n{3,}", "\n\n", text).strip()


def _decode_html(raw: bytes) -> str:
    """Honor common HTML charset declarations, with a safe UTF-8 fallback."""
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return raw.decode("utf-16", errors="replace")
    if raw.startswith(codecs.BOM_UTF8):
        return raw.decode("utf-8-sig", errors="replace")
    prefix = raw[:8192].decode("ascii", errors="ignore")
    match = re.search(r"<meta[^>]+charset\s*=\s*[\"']?\s*([\w.-]+)", prefix, re.IGNORECASE)
    if not match:
        match = re.search(
            r"<meta[^>]+content\s*=\s*[\"'][^\"']*charset=([\w.-]+)", prefix, re.IGNORECASE
        )
    if match:
        try:
            return raw.decode(codecs.lookup(match.group(1)).name, errors="replace")
        except LookupError:
            pass
    return raw.decode("utf-8", errors="replace")


def prepare_html(source: Path, parsed_dir: Path) -> tuple[str, Path]:
    """Write the exact readable text used for extraction to an auditable TXT file."""
    raw = source.read_bytes()
    parser = _VisibleHTMLText()
    parser.feed(_decode_html(raw))
    parser.close()
    text = parser.text()
    if not text:
        raise ValueError("HTML preparation produced no readable text; original source was retained")
    parsed_path = parsed_dir / f"{source.name}.parsed.txt"
    parsed_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = parsed_path.with_suffix(parsed_path.suffix + ".tmp")
    temporary.write_text(text + "\n", encoding="utf-8")
    temporary.replace(parsed_path)
    return text, parsed_path


def _parsed_path(source: Path, parsed_dir: Path) -> Path:
    return parsed_dir / f"{source.name}.parsed.txt"


def _prepare_source(source: Path, root: Path) -> Path:
    """Prepare a stable incoming source once and return its parsed queue item."""
    parsed_dir = root / "ingest" / "parsed"
    parsed_path = _parsed_path(source, parsed_dir)
    if parsed_path.exists() and parsed_path.stat().st_mtime_ns >= source.stat().st_mtime_ns:
        return parsed_path
    if source.suffix.lower() in {".html", ".htm"}:
        _, parsed_path = prepare_html(source, parsed_dir)
    else:
        text = source.read_text(encoding="utf-8", errors="replace")
        if not text.strip():
            raise ValueError("Source contains no readable text")
        parsed_dir.mkdir(parents=True, exist_ok=True)
        temporary = parsed_path.with_suffix(parsed_path.suffix + ".tmp")
        temporary.write_text(text.rstrip() + "\n", encoding="utf-8")
        temporary.replace(parsed_path)
    return parsed_path


def _prepare_incoming(root: Path, logger: logging.Logger) -> None:
    """Parse newly arrived stable files without waiting for the LLM worker."""
    incoming = root / "ingest" / "incoming"
    current_names: set[str] = set()
    for source in incoming.iterdir():
        if not source.is_file() or source.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        current_names.add(source.name)
        try:
            stat = source.stat()
            previous = _stable_observations.get(source.name)
            observations = previous[2] + 1 if previous and previous[:2] == (stat.st_size, stat.st_mtime_ns) else 1
            _stable_observations[source.name] = (stat.st_size, stat.st_mtime_ns, observations)
            if observations < 2:
                continue
            parsed_candidate = _parsed_path(source, root / "ingest" / "parsed")
            needs_prepare = not parsed_candidate.exists() or parsed_candidate.stat().st_mtime_ns < stat.st_mtime_ns
            if not needs_prepare:
                continue
            started = time.perf_counter()
            parsed_path = _prepare_source(source, root)
            logger.info(
                "Source prepared source=%s parsed_file=%s source_bytes=%d parsed_chars=%d elapsed_seconds=%.3f",
                source.name, parsed_path.name, stat.st_size, parsed_path.stat().st_size,
                time.perf_counter() - started,
            )
        except Exception:
            # Keep malformed sources visible to the existing review workflow.
            review_path = root / "review" / f"{source.stem}.review.json"
            write_json(review_path, {
                "reason": "processing_error",
                "error": "Source preparation failed; see application.log for details",
                "source_file": source.name,
                "created_at": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            })
            logger.exception("Failed to prepare source=%s", source.name)
            try:
                _archive(source, root / "ingest" / "archived")
            except OSError:
                logger.exception("Could not archive unparseable source=%s", source.name)
            _stable_observations.pop(source.name, None)
    for old_name in set(_stable_observations) - current_names:
        _stable_observations.pop(old_name, None)


class _IncomingEventHandler(FileSystemEventHandler):
    def __init__(self, wake_event: threading.Event) -> None:
        self.wake_event = wake_event

    def on_any_event(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self.wake_event.set()


class ISOFormatter(logging.Formatter):
    """Render local timestamps with milliseconds and an explicit UTC offset."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        return datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="milliseconds")


def configure_logging(root: Path) -> logging.Logger:
    logger = logging.getLogger("lead_extractor")
    target = (root / "logs" / "application.log").resolve()
    matching_handler = next((h for h in logger.handlers if getattr(h, "baseFilename", None) == str(target)), None)
    if matching_handler is None:
        for old_handler in list(logger.handlers):
            if isinstance(old_handler, logging.handlers.RotatingFileHandler):
                logger.removeHandler(old_handler)
                old_handler.close()
        handler = logging.handlers.RotatingFileHandler(
            target, maxBytes=1_000_000, backupCount=4, encoding="utf-8"
        )
        logger.addHandler(handler)
    for handler in logger.handlers:
        handler.setFormatter(ISOFormatter("%(asctime)s %(levelname)s %(message)s"))
    logger.setLevel(logging.INFO)
    return logger


def close_data_loggers() -> None:
    """Release app log handles so restore can replace workspace directories on Windows."""
    logger = logging.getLogger("lead_extractor")
    for handler in list(logger.handlers):
        if isinstance(handler, logging.handlers.RotatingFileHandler):
            logger.removeHandler(handler)
            handler.flush()
            handler.close()
    from services.ollama import close_raw_loggers
    close_raw_loggers()


def _normalize_extraction(raw: dict[str, Any], category_taxonomy: dict[str, list[str]] | None = None) -> dict[str, Any]:
    result = dict(EXTRACTION_TEMPLATE)
    result.update({key: raw.get(key, default) for key, default in EXTRACTION_TEMPLATE.items()})
    for key in ("company_name", "organization_category", "website", "source_language"):
        result[key] = str(result.get(key) or "").strip()
    if not re.search(r"[A-Za-z0-9\u0590-\u05FF]", result["company_name"]):
        result["company_name"] = ""
    for key in ("emails", "phones", "addresses", "locations", "contacts", "social_links", "opening_hours", "notes"):
        value = result.get(key)
        if not isinstance(value, list):
            raise ValueError(f"Field {key} must be a JSON array")
    result["contacts"] = normalize_contacts(result["contacts"])
    result["social_links"] = normalize_social_links(result["social_links"])
    result["opening_hours"] = normalize_opening_hours(result["opening_hours"])
    category_options = set(category_taxonomy) if category_taxonomy is not None else set(ORGANIZATION_CATEGORIES)
    category_value = result["organization_category"].strip()
    normalized_category = category_value.replace("_", " ").casefold()
    canonical = next((
        item for item in category_options
        if item.casefold() == category_value.casefold()
        or item.replace("_", " ").casefold() == normalized_category
    ), None)
    result["organization_category"] = canonical or ""
    result["subcategory"] = str(result.get("subcategory") or "").strip()
    valid_subcategories = (category_taxonomy or {}).get(result["organization_category"], [])
    if result["subcategory"]:
        canonical_subcategory = next((item for item in valid_subcategories if item.casefold() == result["subcategory"].casefold()), None)
        result["subcategory"] = canonical_subcategory or ""
    return result


def _confidence(record: dict[str, Any]) -> float:
    score = 0.0
    if record.get("company_name"):
        score += 0.4
    if record.get("organization_category") and str(record.get("organization_category")).casefold() != "unknown":
        score += 0.15
    if record.get("website"):
        score += 0.2
    if any(record.get(key) for key in ("emails", "phones", "addresses", "contacts")):
        score += 0.2
    if record.get("notes") or record.get("locations"):
        score += 0.05
    return score


def _has_contact_information(record: dict[str, Any]) -> bool:
    """Return true when extraction found at least one person/contact identifier."""
    if record.get("emails") or record.get("phones"):
        return True
    return any(
        str(contact.get(field) or "").strip()
        for contact in record.get("contacts", [])
        if isinstance(contact, dict)
        for field in ("name", "email", "phone")
    ) or any(
        contact.get(field)
        for contact in record.get("contacts", [])
        if isinstance(contact, dict)
        for field in ("emails", "phones")
    )


def _archive(source: Path, archive_dir: Path) -> Path:
    destination = archive_dir / source.name
    if destination.exists():
        # A retry copies an archived original back to incoming. If it is the
        # same file, discard only that temporary copy and retain the archive.
        if source.is_file() and destination.is_file() and filecmp.cmp(source, destination, shallow=False):
            source.unlink()
            return destination
        destination = archive_dir / f"{source.stem}_{datetime.now().strftime('%Y%m%d%H%M%S%f')}{source.suffix}"
    shutil.move(str(source), str(destination))
    return destination


def process_incoming(
    settings: dict[str, Any],
    only_names: set[str] | None = None,
    manual_fields: dict[str, Any] | None = None,
    include_reserved: bool = False,
) -> dict[str, int]:
    root = data_root(settings)
    logger = configure_logging(root)
    counts = {"processed": 0, "review": 0, "errors": 0, "merged": 0, "new": 0}
    if not _processing_lock.acquire(blocking=False):
        # Another run (scanner or manual trigger) is already extracting.
        logger.info("Extraction already in progress; skipping this processing run")
        counts["skipped"] = True
        return counts
    try:
        return _process_incoming_locked(settings, only_names, manual_fields, counts, logger, include_reserved)
    finally:
        _processing_lock.release()


def _process_incoming_locked(
    settings: dict[str, Any],
    only_names: set[str] | None,
    manual_fields: dict[str, Any] | None,
    counts: dict[str, int],
    logger: logging.Logger,
    include_reserved: bool = False,
) -> dict[str, int]:
    root = data_root(settings)
    cloud_ollama = is_ollama_cloud_url(settings["ollama_url"])
    manual_fields = manual_fields or {}
    manual_company_name = str(manual_fields.get("company_name") or "").strip()
    openai_compat = is_openai_compatible_url(settings["ollama_url"])
    incoming = root / "ingest" / "incoming"
    with _reserved_sources_lock:
        reserved_sources = set() if include_reserved else set(_reserved_sources)
    pending_sources = [
        source for source in sorted(incoming.iterdir())
        if source.is_file()
        and source.suffix.lower() in SUPPORTED_EXTENSIONS
        and (only_names is None or source.name in only_names)
        and source.name not in reserved_sources
        and _parsed_path(source, root / "ingest" / "parsed").exists()
    ]
    total_files = len(pending_sources)
    if not total_files:
        return counts
    if cloud_ollama or openai_compat:
        available_models = []
        model_check_error = ""
    else:
        try:
            available_models = list_models(settings["ollama_url"], str(settings.get("ollama_api_key", "")))
            model_check_error = ""
        except Exception as exc:
            available_models = []
            model_check_error = str(exc)
    if total_files:
        _start_processing_job(total_files)
    try:
        for index, source in enumerate(pending_sources, start=1):
            _update_processing_job(source.name, index - 1, total_files)
            extracted_path = root / "extracted" / f"{source.stem}.extracted.json"
            review_path = root / "review" / f"{source.stem}.review.json"
            selected_model = settings.get("selected_model", "")
            try:
                prepared_path = _parsed_path(source, root / "ingest" / "parsed")
                text = prepared_path.read_text(encoding="utf-8", errors="replace")
                if model_check_error and not openai_compat:
                    raise ValueError(f"Could not verify local Ollama models: {model_check_error}")
                if not selected_model:
                    raise ValueError("Enter and save an Ollama model before processing")
                if not cloud_ollama and not openai_compat and selected_model not in available_models:
                    raise ValueError("Select and save an installed Ollama model before processing")
                start_time = time.perf_counter()
                logger.info("Extraction started source=%s model=%s source_bytes=%d", source.name, selected_model, source.stat().st_size)
                category_taxonomy = load_category_taxonomy(root)
                raw = extract_lead(
                    settings["ollama_url"],
                    selected_model,
                    text,
                    source_file=source.name,
                    raw_log_path=root / "logs" / "llm_raw.jsonl",
                    raw_log_max_bytes=int(settings.get("raw_llm_log_max_bytes", 10_000_000)),
                    raw_log_backups=int(settings.get("raw_llm_log_backups", 5)),
                    category_taxonomy=category_taxonomy,
                    api_key=str(settings.get("ollama_api_key", "")),
                    company_hint=manual_company_name,
                )
                record = _normalize_extraction(raw, category_taxonomy)
                confidence = _confidence(record)
                has_contact_information = _has_contact_information(record)
                record["_source_file"] = source.name
                record["_confidence"] = confidence
                record["_model"] = selected_model
                record["_processed_at"] = datetime.now().astimezone().isoformat(timespec="milliseconds")
                if prepared_path is not None:
                    record["_prepared_file"] = str(prepared_path.relative_to(root))
                if (
                    not record["company_name"]
                    or not has_contact_information
                    or confidence < float(settings.get("minimum_extraction_confidence", 0.35))
                ):
                    reason = (
                        "missing_company_name" if not record["company_name"]
                        else "missing_contact_information" if not has_contact_information
                        else "low_confidence"
                    )
                    write_json(review_path, {
                        "reason": reason,
                        "source_file": source.name,
                        "created_at": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                        "confidence": confidence,
                        "extraction": record,
                    })
                    counts["review"] += 1
                    logger.warning(
                        "Extraction sent to review: %s (reason=%s confidence=%.2f)",
                        source.name, reason, confidence,
                    )
                else:
                    write_json(extracted_path, record)
                    company_id, score, was_merged, candidates = merge_company(root, record, source.name, settings["duplicate_weights"])
                    review_path.unlink(missing_ok=True)
                    counts["merged" if was_merged else "new"] += 1
                    logger.info(
                        "Duplicate decision source=%s model=%s company=%s best_score=%d merged=%s elapsed_seconds=%.2f candidates=%s",
                        source.name, selected_model, company_id, score, was_merged, time.perf_counter() - start_time,
                        json.dumps(candidates, ensure_ascii=False, separators=(",", ":")),
                    )
                counts["processed"] += 1
                _archive(source, root / "ingest" / "archived")
            except Exception as exc:  # Keep failures visible and preserve the original source.
                counts["errors"] += 1
                counts["review"] += 1
                write_json(review_path, {
                    "reason": "processing_error",
                    "error": str(exc),
                    "source_file": source.name,
                    "created_at": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                })
                logger.exception("Failed to process source=%s model=%s", source.name, selected_model)
                # Extraction errors should not block later incoming files.
                try:
                    _archive(source, root / "ingest" / "archived")
                except OSError:
                    logger.exception("Could not archive %s", source.name)
    finally:
        if total_files:
            _finish_processing_job(counts)
    return counts


def start_background_scanner(settings: dict[str, Any]) -> None:
    """Start independent parser and LLM queue workers once per server process."""
    global _scanner_started
    with _scanner_lock:
        if _scanner_started:
            return
        _scanner_started = True

    def parser_loop() -> None:
        observer: Observer | None = None
        watched_directory: Path | None = None
        while True:
            if _scanner_pause_requested.is_set():
                if observer is not None:
                    observer.stop()
                    observer.join()
                    observer = None
                    watched_directory = None
                _scanner_paused.set()
                _scanner_wake_event.wait(timeout=0.25)
                _scanner_wake_event.clear()
                continue
            _scanner_paused.clear()
            try:
                from services.storage import load_settings
                current_settings = load_settings()
                root = data_root(current_settings)
                incoming = root / "ingest" / "incoming"
                if incoming != watched_directory:
                    if observer is not None:
                        observer.stop()
                        observer.join(timeout=2)
                    _stable_observations.clear()
                    observer = Observer()
                    observer.schedule(_IncomingEventHandler(_scanner_wake_event), str(incoming), recursive=False)
                    observer.start()
                    watched_directory = incoming
                    wake_event.set()  # Process existing files at startup or after a folder change.
                _prepare_incoming(root, configure_logging(root))
            except Exception:
                configure_logging(data_root(settings)).exception("Incoming preparation worker failed")
            _scanner_wake_event.wait(timeout=2.0)  # Filesystem events are immediate; this also recovers missed/network events.
            _scanner_wake_event.clear()

    def llm_queue_loop() -> None:
        from services.storage import load_settings

        while True:
            try:
                if not _scanner_pause_requested.is_set() and not _processing_lock.locked():
                    process_incoming(load_settings())
            except Exception:
                configure_logging(data_root(settings)).exception("LLM queue worker failed")
            threading.Event().wait(_watch_interval_seconds)

    threading.Thread(target=parser_loop, name="lead-source-parser", daemon=True).start()
    threading.Thread(target=llm_queue_loop, name="lead-llm-queue", daemon=True).start()
