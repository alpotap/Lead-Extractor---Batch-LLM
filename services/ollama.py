"""Minimal Ollama HTTP API client using Python's standard library."""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
import shutil
import subprocess
import threading
from datetime import datetime
from pathlib import Path
import urllib.request
from typing import Any

from models.lead import (
    EXTRACTION_SCHEMA,
    EXTRACTION_TEMPLATE,
    ORGANIZATION_CATEGORIES,
    normalize_contacts,
    normalize_opening_hours,
    normalize_social_links,
)

_RAW_LOGGERS: dict[str, logging.Logger] = {}
_RAW_LOGGERS_LOCK = threading.Lock()


def _raw_log_event(
    path: Path | None,
    event: dict[str, Any],
    max_bytes: int,
    backup_count: int,
) -> None:
    """Write one complete JSONL record through a rotating local log handler."""
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    key = str(path.resolve())
    with _RAW_LOGGERS_LOCK:
        logger = _RAW_LOGGERS.get(key)
        if logger is None:
            logger = logging.getLogger(f"lead_extractor.raw_llm.{len(_RAW_LOGGERS)}")
            logger.setLevel(logging.INFO)
            logger.propagate = False
            handler = logging.handlers.RotatingFileHandler(
                path, maxBytes=max(1024, max_bytes), backupCount=max(1, backup_count), encoding="utf-8"
            )
            handler.setFormatter(logging.Formatter("%(message)s"))
            logger.addHandler(handler)
            _RAW_LOGGERS[key] = logger
    logger.info(json.dumps(event, ensure_ascii=False, separators=(",", ":")))


def _request(url: str, payload: dict[str, Any] | None = None, timeout: int = 30) -> Any:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"} if data is not None else {},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _generate_json(
    base_url: str,
    model: str,
    prompt: str,
    *,
    source_file: str,
    task: str,
    segment: int,
    segment_count: int,
    raw_log_path: Path | None,
    raw_log_max_bytes: int,
    raw_log_backups: int,
) -> dict[str, Any]:
    request_payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0, "num_predict": 1024},
    }
    request_time = datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        result = _request(base_url.rstrip("/") + "/api/generate", request_payload, timeout=300)
    except Exception as exc:
        error_body = ""
        if hasattr(exc, "read"):
            try:
                error_body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                error_body = ""
        _raw_log_event(raw_log_path, {
            "timestamp": request_time, "event": "ollama_error", "task": task,
            "source_file": source_file, "model": model, "segment": segment,
            "segment_count": segment_count, "prompt": prompt,
            "request": {key: value for key, value in request_payload.items() if key != "prompt"},
            "error": f"{type(exc).__name__}: {exc}", "error_body": error_body,
        }, raw_log_max_bytes, raw_log_backups)
        raise
    _raw_log_event(raw_log_path, {
        "timestamp": request_time, "event": "ollama_response", "task": task,
        "source_file": source_file, "model": model, "segment": segment,
        "segment_count": segment_count, "prompt": prompt,
        "request": {key: value for key, value in request_payload.items() if key != "prompt"},
        "response_text": result.get("response", ""),
        "response_metadata": {key: value for key, value in result.items() if key != "response"},
    }, raw_log_max_bytes, raw_log_backups)
    response = str(result.get("response", "")).strip()
    done_reason = str(result.get("done_reason") or "").strip().lower()
    if done_reason == "length":
        raise ValueError(
            f"Ollama truncated {task} segment {segment}/{segment_count} at its output limit; "
            "the response was rejected before any extraction could be saved"
        )
    if not response:
        raise ValueError(
            f"Ollama returned an empty response for {task} segment {segment}/{segment_count} "
            f"({done_reason or 'no completion reason'}); the response was rejected"
        )
    # Some cloud models wrap valid JSON in Markdown fences despite format=json.
    # Accept that common variation, plus a short preamble around one JSON object.
    unfenced = re.sub(r"^\s*```(?:json)?\s*", "", response, flags=re.IGNORECASE)
    unfenced = re.sub(r"\s*```\s*$", "", unfenced)
    try:
        extracted = json.loads(unfenced)
    except json.JSONDecodeError as exc:
        object_match = re.search(r"\{.*\}", unfenced, flags=re.DOTALL)
        if object_match:
            try:
                extracted = json.loads(object_match.group(0))
            except json.JSONDecodeError:
                reason = done_reason or "no completion reason"
                raise ValueError(f"Ollama returned invalid JSON for {task} segment {segment}/{segment_count} ({reason}): {exc}") from exc
        else:
            reason = done_reason or "no completion reason"
            raise ValueError(f"Ollama returned invalid JSON for {task} segment {segment}/{segment_count} ({reason}): {exc}") from exc
    if not isinstance(extracted, dict):
        raise ValueError(f"Ollama response for {task} segment {segment}/{segment_count} was not a JSON object")
    return extracted


def list_models(base_url: str) -> list[str]:
    # Match the familiar Ollama CLI workflow when its executable is installed.
    if shutil.which("ollama"):
        result = subprocess.run(["ollama", "list"], capture_output=True, text=True, timeout=10, check=True)
        models = []
        for line in result.stdout.splitlines()[1:]:
            columns = line.split()
            if columns:
                models.append(columns[0])
        return sorted(models)
    # The local API remains a useful fallback when Ollama is available on PATH
    # only through a service wrapper or a nonstandard installation.
    response = _request(base_url.rstrip("/") + "/api/tags", timeout=5)
    return sorted(model["name"] for model in response.get("models", []) if model.get("name"))


def extract_lead(
    base_url: str,
    model: str,
    source_text: str,
    *,
    source_file: str = "",
    raw_log_path: Path | None = None,
    raw_log_max_bytes: int = 10_000_000,
    raw_log_backups: int = 5,
    category_taxonomy: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    schema = json.dumps(EXTRACTION_SCHEMA, ensure_ascii=False, indent=2)
    # Keep prompts under the small default context window used by many local
    # models. Long saved pages are handled in order and their fields combined.
    chunks = [source_text[i : i + 5500] for i in range(0, len(source_text), 5500)] or [""]
    combined = {
        key: ([] if isinstance(value, list) else value)
        for key, value in EXTRACTION_TEMPLATE.items()
    }
    taxonomy = category_taxonomy if category_taxonomy is not None else {category: [] for category in ORGANIZATION_CATEGORIES}
    taxonomy_prompt = "\n".join(
        f"- {category} (display name: {category.replace('_', ' ').title()}): {', '.join(subcategories) if subcategories else '(no subcategories listed)'}"
        for category, subcategories in taxonomy.items()
    )
    for chunk_number, chunk in enumerate(chunks, start=1):
        prompt = f"""You extract publishing-related organization leads from supplied source text.
Return exactly one JSON object and no markdown or commentary. Follow this schema exactly:
{schema}
Choose organization_category and subcategory from this user taxonomy when the source supports it. Use the category name exactly as written; use an empty subcategory when none fits. If no listed category fits, use unknown.
USER CATEGORY TAXONOMY:
{taxonomy_prompt}
Support English and Hebrew. Do not guess missing facts. Use empty strings and arrays when unknown.
Include only information present in the source. Keep notes to at most three short bullets. Return compact JSON.
CONTACTS: Find every named person associated with the organization, including people in team, staff, editorial, leadership, and contact sections. Make one contacts entry per person even when the page gives only a name. Attach that person's title/role, email, and phone to the same entry when the page makes the association clear. Never merge different people or invent names.
SOCIAL LINKS: For each social account, pair its platform name (such as Facebook or Instagram) with the actual profile URL found in the source. Do not put a platform name in the URL field. If only the platform is shown, keep the name and leave url empty.
OPENING HOURS: Copy any published hours into one opening_hours row per day or day range. Preserve closed days and exact times. Leave the array empty when no hours are stated.
This is source segment {chunk_number} of {len(chunks)}. Extract facts present in this segment only.

SOURCE TEXT:
{chunk}
"""
        extracted = _generate_json(
            base_url, model, prompt, source_file=source_file, task="full_extraction",
            segment=chunk_number, segment_count=len(chunks), raw_log_path=raw_log_path,
            raw_log_max_bytes=raw_log_max_bytes, raw_log_backups=raw_log_backups,
        )
        extracted["contacts"] = normalize_contacts(extracted.get("contacts", []))
        extracted["social_links"] = normalize_social_links(extracted.get("social_links", []))
        extracted["opening_hours"] = normalize_opening_hours(extracted.get("opening_hours", []))
        for key, default in EXTRACTION_TEMPLATE.items():
            value = extracted.get(key, default)
            if key in ("company_name", "subcategory", "website", "source_language"):
                if value and not combined[key]:
                    combined[key] = value
            elif key == "organization_category":
                if value != "unknown" and combined[key] == "unknown":
                    combined[key] = value
            elif key == "contacts" and isinstance(value, list):
                combined[key] = normalize_contacts(combined[key] + value)
            elif isinstance(value, list):
                for item in value:
                    if item not in combined[key]:
                        combined[key].append(item)

    # A targeted second pass recovers staff listings that the broad extraction
    # missed. It runs only for pages that look like team/contact pages and only
    # when fewer than five named people were found.
    team_terms = re.compile(
        r"\b(team|staff|people|leadership|editors?|contributors|our people|who we are)\b|"
        r"צוות|הנהלה|עורכים|עורכות|אנשי קשר|מי אנחנו|הצוות",
        re.IGNORECASE,
    )
    relevant_indexes = [index for index, chunk in enumerate(chunks) if team_terms.search(chunk)]
    if relevant_indexes and len(combined["contacts"]) < 5:
        selected_indexes = {
            neighbor
            for index in relevant_indexes
            for neighbor in (index - 1, index, index + 1)
            if 0 <= neighbor < len(chunks)
        }
        contact_source = "\n\n".join(chunks[index] for index in sorted(selected_indexes))[:11000]
        contact_prompt = f"""Extract only the named people associated with this organization from the source text.
Return exactly one JSON object with this shape and no markdown:
{{"contacts":[{{"name":"","roles":[],"emails":[],"phones":[]}}]}}
List every distinct named person in team, staff, editorial, leadership, and contact sections, even if only a name is shown. Keep each person's role, email, and phone in that person's own object when the association is clear. Do not invent or combine people. Return an empty contacts array if there are no named people.

SOURCE TEXT:
{contact_source}
"""
        try:
            contact_result = _generate_json(
                base_url, model, contact_prompt, source_file=source_file,
                task="contact_followup", segment=1, segment_count=1,
                raw_log_path=raw_log_path, raw_log_max_bytes=raw_log_max_bytes,
                raw_log_backups=raw_log_backups,
            )
            combined["contacts"] = normalize_contacts(
                combined["contacts"] + normalize_contacts(contact_result.get("contacts", []))
            )
        except Exception as exc:
            logging.getLogger("lead_extractor").warning(
                "Contact follow-up extraction failed source=%s; keeping primary result: %s",
                source_file, exc,
            )
    return combined
