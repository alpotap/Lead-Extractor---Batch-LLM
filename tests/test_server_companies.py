"""Tests for the /api/companies endpoint."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def test_api_companies_returns_json(client: Any) -> None:
    """GET /api/companies must return valid JSON, not an HTML error page."""
    res = client.get("/api/companies")
    assert res.status_code == 200, f"Got {res.status_code}: {res.text[:200]}"
    data = res.json()
    assert "companies" in data
    assert "total" in data
    assert isinstance(data["companies"], list)


def test_api_companies_search(client: Any) -> None:
    """GET /api/companies with a query parameter must return valid JSON."""
    res = client.get("/api/companies", params={"query": "nonexistent"})
    assert res.status_code == 200, f"Got {res.status_code}: {res.text[:200]}"
    data = res.json()
    assert "companies" in data


def test_api_companies_with_filters(client: Any) -> None:
    """GET /api/companies with filters must return valid JSON."""
    res = client.get("/api/companies", params={
        "category": "Unknown",
        "status": "New",
        "has_website": "none",
        "page": 1,
        "page_size": 50,
    })
    assert res.status_code == 200, f"Got {res.status_code}: {res.text[:200]}"
    data = res.json()
    assert "companies" in data


def test_company_opening_hours_can_be_saved_and_read_back(client: Any) -> None:
    created = client.post("/api/companies", json={"company_name": "Hours Test Bakery"})
    assert created.status_code == 200, created.text
    company_id = created.json()["company_id"]

    updated = client.put(
        f"/api/companies/{company_id}",
        json={"opening_hours": [
            {"day": "Monday", "hours": "Open 24 hours"},
            {"day": "Tuesday", "hours": "Closed"},
        ]},
    )
    assert updated.status_code == 200, updated.text

    details = client.get(f"/api/companies/{company_id}")
    assert details.status_code == 200, details.text
    assert details.json()["company"]["opening_hours"] == [
        {"day": "Monday", "hours": "Open 24 hours"},
        {"day": "Tuesday", "hours": "Closed"},
    ]


def test_company_detail_marks_missing_original_sources_unavailable(client: Any) -> None:
    import server

    created = client.post("/api/companies", json={"company_name": "Source Metadata Co"})
    assert created.status_code == 200, created.text
    company_id = created.json()["company_id"]
    root = server._get_root()
    company_path = root / "companies" / f"{server.safe_stem(company_id)}.json"
    company = server.read_json(company_path, {})
    company["sources"] = ["web_source.html"]
    server.write_json(company_path, company)

    missing = client.get(f"/api/companies/{company_id}")
    assert missing.json()["available_sources"] == []

    archived = root / "ingest" / "archived" / "web_source.html"
    archived.parent.mkdir(parents=True, exist_ok=True)
    archived.write_text("source", encoding="utf-8")
    available = client.get(f"/api/companies/{company_id}")
    assert available.json()["available_sources"] == ["web_source.html"]
