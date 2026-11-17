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
from typing import Any
from urllib.parse import urlparse
import urllib.request

DEFAULT_NUM_PREDICT = 4096
MAX_NUM_PREDICT = 32768

from models.lead import (
    EXTRACTION_SCHEMA,
    EXTRACTION_TEMPLATE,
    ORGANIZATION_CATEGORIES,
    normalize_contacts,
    normalize_opening_hours,
    normalize_social_links,
)

SOURCE_CHUNK_CHARS = 5500
SOURCE_CHUNK_OVERLAP_CHARS = 800

_RAW_LOGGERS: dict[str, logging.Logger] = {}
_RAW_LOGGERS_LOCK = threading.Lock


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


def is_local_url(url: str) -> bool:
    """Return whether an Ollama endpoint points at this machine."""
    try:
        hostname = (urlparse(url).hostname or "").strip("[]").casefold()
    except ValueError:
        return False
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return True
    if hostname == "::1":
        return True
    return hostname.startswith("127.")


def is_ollama_cloud_url(url: str) -> bool:
    """Identify Ollama's hosted API endpoint, where users enter model names."""
    try:
        parsed = urlparse(url.strip())
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme.casefold() == "https"
        and (parsed.hostname or "").casefold() == "ollama.com"
        and parsed.path in ("", "/")
        and port in (None, 443)
    )


def is_openai_url(url: str) -> bool:
    """Identify OpenAI's API endpoint."""
    try:
        parsed = urlparse(url.strip())
        hostname = (parsed.hostname or "").casefold()
        return hostname == "api.openai.com" and parsed.scheme == "https"
    except ValueError:
        return False


def is_openai_compatible_url(url: str) -> bool:
    """Identify any OpenAI-compatible API endpoint (OpenAI, NVIDIA NIM, etc.)."""
    try:
        parsed = urlparse(url.strip())
        hostname = (parsed.hostname or "").casefold()
        scheme = parsed.scheme.casefold()
        return scheme == "https" and hostname not in (
            "localhost",
            "ollama.com",
        ) and not hostname.startswith("127.")
    except ValueError:
        return False


def _request(
    url: str, payload: dict[str, Any] | None = None, timeout: int = 30, api_key: str = "",
) -> Any:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    if api_key and not is_local_url(url):
        headers["Authorization"] = f"Bearer {api_key.strip()}"
    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
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
    api_key: str = "",
) -> dict[str, Any]:
    """Call either OpenAI-compatible or Ollama generate endpoint and return parsed JSON."""
    openai = is_openai_compatible_url(base_url)

    if openai:
        base = base_url.rstrip("/")
        # base_url already includes /v1 (e.g. https://api.openai.com/v1 or
        # https://integrate.api.nvidia.com/v1); do not double-append /v1.
        chat_url = base + "/chat/completions"
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
    else:
        chat_url = base_url.rstrip("/") + "/api/generate"
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {
                "temperature": 0,
                "num_predict": DEFAULT_NUM_PREDICT,
                "num_ctx": max(8192, DEFAULT_NUM_PREDICT + 4096),
            },
        }

    while True:
        request_time = datetime.now().astimezone().isoformat(timespec="seconds")
        try:
            result = _request(chat_url, payload, timeout=300, api_key=api_key)
        except Exception as exc:
            error_body = ""
            if hasattr(exc, "read"):
                try:
                    error_body = exc.read().decode("utf-8", errors="replace")
                except Exception:
                    error_body = ""
            _raw_log_event(raw_log_path, {
                "timestamp": request_time, "event": "extraction_error", "task": task,
                "source_file": source_file, "model": model, "segment": segment,
                "segment_count": segment_count, "prompt": prompt,
                "request": {"url": chat_url, "payload": {k: v for k, v in payload.items() if k != "messages"}},
                "error": f"{type(exc).__name__}: {exc}", "error_body": error_body,
            }, raw_log_max_bytes, raw_log_backups)
            raise
        _raw_log_event(raw_log_path, {
            "timestamp": request_time, "event": "extraction_response", "task": task,
            "source_file": source_file, "model": model, "segment": segment,
            "segment_count": segment_count, "prompt": prompt,
            "request": {"url": chat_url, "payload": {k: v for k, v in payload.items() if k != "messages"}},
            "response_text": result.get("choices", [{}])[0].get("message", {}).get("content", "") if openai else result.get("response", ""),
            "response_metadata": {
                key: value for k, v in result.items()
                if k not in ("id", "object", "created", "model", "choices")
                and not (isinstance(v, (str, int, float, bool, type(None))) or isinstance(v, list))
            } if not openai else {},
        }, raw_log_max_bytes, raw_log_backups)

        if openai:
            choice = result.get("choices", [{}])[0]
            delta = choice.get("delta", {}) or {}
            content = choice.get("message", {}).get("content", "")
            done_reason = delta.get("role") or choice.get("finish_reason")
            if done_reason == "stop":
                break
            response = content.strip()
        else:
            response = str(result.get("response", "")).strip()
            done_reason = str(result.get("done_reason") or "").strip().lower()
            if done_reason != "length":
                break
            current_limit = int(payload["options"]["num_predict"])
            if current_limit >= MAX_NUM_PREDICT:
                raise ValueError(
                    f"Ollama truncated {task} segment {segment}/{segment_count} at the maximum "
                    f"output limit ({current_limit} tokens); the response was rejected before any extraction could be saved"
                )
            next_limit = min(current_limit * 2, MAX_NUM_PREDICT)
            payload["options"]["num_predict"] = next_limit
            payload["options"]["num_ctx"] = max(8192, next_limit + 4096)

    if openai:
        response = result.get("choices", [{}])[0].get("message", {}).get("content", "")
    if not response:
        raise ValueError(
            f"API returned an empty response for {task} segment {segment}/{segment_count} "
            f"(finish_reason={done_reason or 'no completion reason'}); the response was rejected"
        )
    # Accept JSON fences or raw JSON.
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
                raise ValueError(f"API returned invalid JSON for {task} segment {segment}/{segment_count} ({reason}): {exc}") from exc
        else:
            reason = done_reason or "no completion reason"
            raise ValueError(f"API returned invalid JSON for {task} segment {segment}/{segment_count} ({reason}): {exc}") from exc
    if not isinstance(extracted, dict):
        raise ValueError(f"API response for {task} segment {segment}/{segment_count} was not a JSON object")
    return extracted

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
    api_key: str = "",
    company_hint: str = "",
) -> dict[str, Any]:
    """Extract lead information from source text using the configured AI API.

    When ``company_hint`` is provided (for example a company name entered
    manually while retrying a review item), it is used as the authoritative
    company name so the extraction runs with the value the user entered.
    """
    schema = json.dumps(EXTRACTION_SCHEMA, ensure_ascii=False, indent=2)
    # Keep prompts bounded, but carry nearby text across boundaries so a name,
    # heading, or contact detail is less likely to be isolated in one segment.
    chunks: list[str] = []
    start = 0
    while start < len(source_text):
        end = min(start + SOURCE_CHUNK_CHARS, len(source_text))
        if end < len(source_text):
            # Prefer a line boundary in the latter half of the window when one
            # is available; fall back to the hard limit for long single lines.
            line_break = source_text.rfind("\n", start + SOURCE_CHUNK_CHARS // 2, end)
            if line_break > start:
                end = line_break
        chunks.append(source_text[start:end].strip())
        if end >= len(source_text):
            break
        next_start = max(start + 1, end - SOURCE_CHUNK_OVERLAP_CHARS)
        # If possible, move forward to a nearby line boundary while keeping a
        # useful portion of the previous segment in the next prompt.
        line_break = source_text.find("\n", next_start, min(end, next_start + 300))
        if line_break >= 0:
            next_start = line_break + 1
        start = next_start
    if not chunks:
        chunks = [""]
    combined = {
        key: ([] if isinstance(value, list) else value)
        for key, value in EXTRACTION_TEMPLATE.items()
    }
    taxonomy = category_taxonomy if category_taxonomy is not None else {}
    if company_hint.strip():
        combined["company_name"] = company_hint.strip()
    taxonomy_prompt = "\n".join(
        f"- {category} (display name: {category.replace('_', ' ').title()}): {', '.join(subcategories) if subcategories else '(no subcategories listed)'}"
        for category, subcategories in taxonomy.items()
    )
    for chunk_number, chunk in enumerate(chunks, start=1):
        prompt = f"""You extract publishing-related organization leads from supplied source text.
Return exactly one JSON object and no markdown or commentary. Follow this schema exactly:
{schema}
Choose organization_category and subcategory from this user taxonomy when the source supports it. Use the category name exactly as written; use empty strings for both fields when no listed category fits or when no categories are listed.
USER CATEGORY TAXONOMY:
{taxonomy_prompt}
Support English and Hebrew. Do not guess missing facts. Use empty strings and arrays when unknown.
Include only information present in the source. Keep notes to at most three short bullets. Return compact JSON.
CONTACTS: Find every named person associated with the organization, including people in team, staff, editorial, leadership, and contact sections. Make one contacts entry per person even when the page gives only a name. Attach that person's title/role, email, and phone to the same entry when the page makes the association clear. Never merge different people or invent names.
SOCIAL LINKS: For each social account, pair its platform name (such as Facebook or Instagram) with the actual profile URL found in the source. Do not put a platform name in the URL field. If only the platform is shown, keep the name and leave url empty.
OPENING HOURS: Copy any published hours into one opening_hours row per day or day range. Preserve closed days and exact times. Leave the array empty when no hours are stated.
This is source segment {chunk_number} of {len(chunks)}. Adjacent segments may repeat a little context around their boundary. Extract facts present in this segment.

SOURCE TEXT:
{chunk}
"""
        extracted = _generate_json(
            base_url, model, prompt, source_file=source_file, task="full_extraction",
            segment=chunk_number, segment_count=len(chunks), raw_log_path=raw_log_path,
            raw_log_max_bytes=raw_log_max_bytes, raw_log_backups=raw_log_backups,
            api_key=api_key,
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
                if value and value.casefold() != "unknown" and not combined[key]:
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
                api_key=api_key,
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


def list_models(base_url: str, api_key: str = '') -> list[str]:
    # Match the familiar Ollama CLI workflow when its executable is installed.
    if is_local_url(base_url) and shutil.which('ollama'):
        import subprocess
        result = subprocess.run(['ollama', 'list'], capture_output=True, text=True, timeout=10, check=True)
        models = []
        for line in result.stdout.splitlines()[1:]:
            columns = line.split()
            if columns:
                models.append(columns[0])
        return sorted(models)

    # OpenAI-compatible: list models from /models (base_url already includes /v1)
    if is_openai_compatible_url(base_url):
        base = base_url.rstrip("/")
        response = _request(base + "/models", timeout=5, api_key=api_key)
        return sorted(model["id"] for model in response.get("data", []) if model.get("id"))

    # The local API remains a useful fallback when Ollama is available on PATH
    # only through a service wrapper or a nonstandard installation.
    response = _request(base_url.rstrip('/') + '/api/tags', timeout=5, api_key=api_key)
    return sorted(model['name'] for model in response.get('models', []) if model.get('name'))
