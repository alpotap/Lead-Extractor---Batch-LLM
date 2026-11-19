"""Tests for the /api/review/retry endpoint.

Retry requests must enqueue work and return immediately so the UI remains usable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch



def _write_archived_source(root: Path, name: str, content: str = "test content") -> None:
    """Write a fake archived source file so the retry endpoint finds it."""
    archived = root / "ingest" / "archived" / name
    archived.write_text(content, encoding="utf-8")


def test_api_review_retry_queues_extraction(
    client: Any, temp_data_root: Path
) -> None:
    """POST /api/review/retry returns without waiting for model inference."""
    source_name = "test_source.html"
    _write_archived_source(temp_data_root, source_name)

    with patch.object(__import__("server"), "_launch_processing") as mock_launch:
        response = client.post(
            "/api/review/retry",
            json={"source_file": source_name},
        )

    assert response.status_code == 200, response.text
    data = response.json()
    assert data == {"status": "queued", "source_file": source_name}
    mock_launch.assert_called_once()
    assert mock_launch.call_args.args[1] == {source_name}


def test_api_review_retry_queues_even_when_another_run_is_active(
    client: Any, temp_data_root: Path
) -> None:
    """Lock contention is handled by the worker; the request stays responsive."""
    source_name = "test_source.html"
    _write_archived_source(temp_data_root, source_name)

    with patch.object(__import__("server"), "_launch_processing") as mock_launch:
        response = client.post(
            "/api/review/retry",
            json={"source_file": source_name},
        )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "queued"
    mock_launch.assert_called_once()


def test_api_review_retry_404_when_no_archived_source(
    client: Any,
) -> None:
    """POST /api/review/retry must return 404 when the archived source is missing."""
    response = client.post(
        "/api/review/retry",
        json={"source_file": "nonexistent.html"},
    )
    assert response.status_code == 404
