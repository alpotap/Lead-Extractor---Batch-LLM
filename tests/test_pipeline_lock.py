"""Tests for pipeline.py processing lock behavior.

Regression test for: retry silently doing nothing when the background scanner
holds the processing lock. process_incoming must return skipped=True when the
lock is held, so callers can detect the contention instead of doing nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import services.pipeline as pipeline


def test_processing_lock_is_instance_not_class() -> None:
    """_processing_lock must be a lock instance, not the Lock class."""
    import threading

    assert pipeline._processing_lock is not threading.Lock
    assert isinstance(pipeline._processing_lock, type(threading.Lock()))


def test_process_incoming_returns_skipped_when_lock_held(
    tmp_path: Path,
) -> None:
    """When _processing_lock is already held, process_incoming returns skipped=True."""
    # Manually acquire the lock to simulate another run in progress.
    acquired = pipeline._processing_lock.acquire(blocking=False)
    assert acquired is True

    try:
        settings: dict[str, Any] = {
            "data_path": str(tmp_path),
            "ollama_url": "http://localhost:11434",
            "selected_model": "test-model",
            "ollama_api_key": "",
            "minimum_extraction_confidence": 0.35,
            "raw_llm_log_max_bytes": 10_000_000,
            "raw_llm_log_backups": 5,
            "duplicate_weights": {
                "email": 100, "phone": 100, "website_domain": 95,
                "address": 80, "contact": 70, "company_name": 30, "city": 20,
            },
            "custom_statuses": [],
        }
        outcome = pipeline.process_incoming(settings)
        assert outcome.get("skipped") is True
    finally:
        pipeline._processing_lock.release()
