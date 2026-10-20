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

from models.lead import (
    EXTRACTION_TEMPLATE,
    ORGANIZATION_CATEGORIES,
    normalize_contacts,
    normalize_opening_hours,
    normalize_social_links,
)
from services.ollama import extract_lead, list_models
from services.storage import data_root, load_category_taxonomy, merge_company, write_json

SUPPORTED_EXTENSIONS = {".txt", ".html", ".md"}
_scanner_lock = threading.Lock()
_scanner_started = False


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
    parsed_path.write_text(text + "\n", encoding="utf-8")
    return text, parsed_path


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
    result["organization_category"] = canonical or "unknown"
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
    if record.get("organization_category") != "unknown":
        score += 0.15
    if record.get("website"):
        score += 0.2
    if any(record.get(key) for key in ("emails", "phones", "addresses", "contacts")):
        score += 0.2
    if record.get("notes") or record.get("locations"):
        score += 0.05
    return score


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


def process_incoming(settings: dict[str, Any], only_names: set[str] | None = None) -> dict[str, int]:
    root = data_root(settings)
    logger = configure_logging(root)
    counts = {"processed": 0, "review": 0, "errors": 0, "merged": 0, "new": 0}
    try:
        available_models = list_models(settings["ollama_url"])
        model_check_error = ""
    except Exception as exc:
        available_models = []
        model_check_error = str(exc)
    incoming = root / "ingest" / "incoming"
    for source in sorted(incoming.iterdir()):
        if not source.is_file() or source.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        if only_names is not None and source.name not in only_names:
            continue
        extracted_path = root / "extracted" / f"{source.stem}.extracted.json"
        review_path = root / "review" / f"{source.stem}.review.json"
        selected_model = settings.get("selected_model", "")
        try:
            prepared_path: Path | None = None
            if source.suffix.lower() == ".html":
                text, prepared_path = prepare_html(source, root / "ingest" / "parsed")
                logger.info(
                    "Prepared HTML source=%s parsed_file=%s source_bytes=%d prepared_chars=%d",
                    source.name, prepared_path.name, source.stat().st_size, len(text),
                )
            else:
                # Plain text and Markdown keep their existing byte-to-text path.
                text = source.read_text(encoding="utf-8", errors="replace")
            if model_check_error:
                raise ValueError(f"Could not verify local Ollama models: {model_check_error}")
            if not selected_model or selected_model not in available_models:
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
            )
            record = _normalize_extraction(raw, category_taxonomy)
            confidence = _confidence(record)
            record["_source_file"] = source.name
            record["_confidence"] = confidence
            record["_model"] = selected_model
            record["_processed_at"] = datetime.now().astimezone().isoformat(timespec="milliseconds")
            if prepared_path is not None:
                record["_prepared_file"] = str(prepared_path.relative_to(root))
            if not record["company_name"] or confidence < float(settings.get("minimum_extraction_confidence", 0.35)):
                write_json(review_path, {
                    "reason": "missing_company_name" if not record["company_name"] else "low_confidence",
                    "source_file": source.name,
                    "created_at": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                    "confidence": confidence,
                    "extraction": record,
                })
                counts["review"] += 1
                logger.warning("Extraction sent to review: %s (confidence %.2f)", source.name, confidence)
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
    return counts


def start_background_scanner(settings: dict[str, Any]) -> None:
    """Start one lightweight daemon scanner per Streamlit server process."""
    global _scanner_started
    with _scanner_lock:
        if _scanner_started:
            return
        _scanner_started = True

    def scan_loop() -> None:
        while True:
            try:
                # Read settings each cycle so sidebar changes take effect without a restart.
                from services.storage import load_settings

                current_settings = load_settings()
                process_incoming(current_settings)
            except Exception:
                configure_logging(data_root(settings)).exception("Background scan failed")
                current_settings = settings
            interval = max(1, int(current_settings.get("scan_interval_minutes", 1))) * 60
            threading.Event().wait(interval)

    threading.Thread(target=scan_loop, name="lead-incoming-scanner", daemon=True).start()
