"""Tests for the /api/sources/url endpoint.

Regression test for: fetching from URL returned "failed to fetch" because the
endpoint returned a StreamingResponse yielding dicts instead of a single JSON
document. The endpoint must return proper JSON with results/total/saved/failed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch
import asyncio

import services.web_capture as web_capture


def test_api_sources_url_returns_json_results(client: Any) -> None:
    """POST /api/sources/url must return a JSON object with results array."""
    fake_html = "<html><body><h1>Test Company</h1></body></html>"

    with patch.object(web_capture, "capture_rendered_html", return_value=fake_html):
        response = client.post(
            "/api/sources/url",
            json={"urls": "https://example.com"},
        )

    assert response.status_code == 200, response.text
    data = response.json()
    assert "results" in data
    assert data["total"] == 1
    assert data["saved"] == 1
    assert data["failed"] == 0
    assert data["results"][0]["url"] == "https://example.com"
    assert data["results"][0]["status"] == "saved"
    assert "file" in data["results"][0]


def test_api_sources_url_handles_fetch_failure(client: Any) -> None:
    """POST /api/sources/url must report failed URLs in results, not crash."""
    with patch.object(
        web_capture,
        "capture_rendered_html",
        side_effect=RuntimeError("browser crashed"),
    ):
        response = client.post(
            "/api/sources/url",
            json={"urls": "https://bad.example.com"},
        )

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["total"] == 1
    assert data["saved"] == 0
    assert data["failed"] == 1
    assert data["results"][0]["status"] == "failed"
    assert "browser crashed" in data["results"][0]["error"]


def test_api_sources_url_empty_returns_empty(client: Any) -> None:
    """POST /api/sources/url with no URLs returns a 400 error."""
    response = client.post("/api/sources/url", json={"urls": ""})
    assert response.status_code == 400


def test_url_fetch_job_exposes_per_url_progress(client: Any) -> None:
    async def fake_capture(url: str, **kwargs: Any) -> str:
        await asyncio.sleep(0.01)
        if "bad" in url:
            raise RuntimeError("browser crashed")
        return "<html>ok</html>"

    with patch.object(web_capture, "capture_rendered_html", side_effect=fake_capture):
        started = client.post(
            "/api/sources/url/jobs", json={"urls": "https://good.example\nhttps://bad.example"}
        )
        assert started.status_code == 200, started.text
        job_id = started.json()["job_id"]
        # Poll briefly because the worker intentionally continues after the start response.
        for _ in range(50):
            progress = client.get(f"/api/sources/url/jobs/{job_id}").json()
            if progress["finished"]:
                break
            import time
            time.sleep(0.02)

    assert progress["completed"] == 2
    assert progress["saved"] == 1
    assert progress["failed"] == 1
    assert {row["status"] for row in progress["results"]} == {"saved", "failed"}
