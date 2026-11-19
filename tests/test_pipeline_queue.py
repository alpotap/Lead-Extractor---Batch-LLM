from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest.mock import patch

import services.pipeline as pipeline
from models.lead import EXTRACTION_TEMPLATE


def test_parser_prepares_new_html_without_acquiring_llm_lock(tmp_path: Path, monkeypatch: Any) -> None:
    root = tmp_path / "data"
    incoming = root / "ingest" / "incoming"
    incoming.mkdir(parents=True)
    source = incoming / "new.html"
    source.write_text("<html><body><h1>Example</h1><a href='https://example.com'>Site</a></body></html>", encoding="utf-8")
    (root / "logs").mkdir(parents=True)
    monkeypatch.setattr(pipeline, "_stable_observations", {})

    assert pipeline._processing_lock.acquire(blocking=False)
    try:
        logger = pipeline.configure_logging(root)
        pipeline._prepare_incoming(root, logger)
        pipeline._prepare_incoming(root, logger)
        parsed = root / "ingest" / "parsed" / "new.html.parsed.txt"
        assert parsed.exists()
        assert "Example" in parsed.read_text(encoding="utf-8")
        assert "https://example.com" in parsed.read_text(encoding="utf-8")
    finally:
        pipeline._processing_lock.release()


def test_empty_llm_queue_does_not_probe_model_endpoint(tmp_path: Path) -> None:
    settings: dict[str, Any] = {
        "data_path": str(tmp_path),
        "ollama_url": "http://localhost:11434",
        "selected_model": "test-model",
    }
    with patch.object(pipeline, "list_models") as list_models:
        result = pipeline.process_incoming(settings)
    assert result["processed"] == 0
    list_models.assert_not_called()


def test_persisted_parsed_queue_item_is_consumed_after_restart(tmp_path: Path) -> None:
    root = tmp_path / "data"
    incoming = root / "ingest" / "incoming"
    incoming.mkdir(parents=True)
    source = incoming / "queued.html"
    source.write_text("<h1>Original HTML</h1>", encoding="utf-8")
    parsed_path = pipeline._prepare_source(source, root)
    parsed_path.write_text("Durable parsed queue payload", encoding="utf-8")
    settings: dict[str, Any] = {
        "data_path": str(root),
        "ollama_url": "https://api.example.com/v1",
        "selected_model": "test-model",
        "minimum_extraction_confidence": 0.35,
        "raw_llm_log_max_bytes": 10_000,
        "raw_llm_log_backups": 1,
        "duplicate_weights": {},
    }
    raw = deepcopy(EXTRACTION_TEMPLATE)
    raw.update({"company_name": "Queue Co", "website": "https://queue.example", "emails": ["a@queue.example"]})

    with patch.object(pipeline, "extract_lead", return_value=raw) as extract, patch.object(
        pipeline, "merge_company", return_value=("queue-co", 0, False, [])
    ):
        result = pipeline.process_incoming(settings)

    assert result["new"] == 1
    assert extract.call_args.args[2] == "Durable parsed queue payload"
    assert not source.exists()
    assert (root / "ingest" / "archived" / source.name).exists()


def test_extraction_without_contact_info_goes_to_review_not_companies(tmp_path: Path) -> None:
    root = tmp_path / "data"
    incoming = root / "ingest" / "incoming"
    incoming.mkdir(parents=True)
    source = incoming / "no-contact.txt"
    source.write_text("Company overview", encoding="utf-8")
    parsed = pipeline._parsed_path(source, root / "ingest" / "parsed")
    parsed.parent.mkdir(parents=True)
    parsed.write_text("A company with a website but no human contact details", encoding="utf-8")
    settings: dict[str, Any] = {
        "data_path": str(root),
        "ollama_url": "https://api.example.com/v1",
        "selected_model": "test-model",
        "minimum_extraction_confidence": 0.35,
        "raw_llm_log_max_bytes": 10_000,
        "raw_llm_log_backups": 1,
        "duplicate_weights": {},
    }
    raw = deepcopy(EXTRACTION_TEMPLATE)
    raw.update({
        "company_name": "No Contact Co",
        "website": "https://no-contact.example",
        "notes": ["Interesting organization"],
    })

    with patch.object(pipeline, "extract_lead", return_value=raw), patch.object(
        pipeline, "merge_company"
    ) as merge:
        result = pipeline.process_incoming(settings)

    assert result["review"] == 1
    assert result["new"] == 0
    merge.assert_not_called()
    review_path = root / "review" / "no-contact.review.json"
    assert review_path.exists()
    import json
    assert json.loads(review_path.read_text(encoding="utf-8"))["reason"] == "missing_contact_information"
