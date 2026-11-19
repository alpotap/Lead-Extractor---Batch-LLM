"""Tests for the /api/companies endpoint, including edge cases with malformed data."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import json


def _write_company(root: Path, company_id: str, record: dict[str, Any]) -> None:
    (root / "companies").mkdir(parents=True, exist_ok=True)
    (root / "companies" / f"{company_id}.json").write_text(
        json.dumps(record, ensure_ascii=False), encoding="utf-8"
    )


def test_api_companies_handles_phones_as_dicts(
    client: Any, temp_data_root: Path
) -> None:
    """GET /api/companies must not crash when phones are stored as dicts."""
    _write_company(
        temp_data_root,
        "test-phones-dicts",
        {
            "company_id": "test-phones-dicts",
            "company_name": "Phones Dict Test",
            "organization_category": "",
            "subcategory": "",
            "website": "",
            "emails": [{"value": "info@example.com"}],
            "phones": [{"value": "+1234567890"}, {"value": "+0987654321"}],
            "addresses": [],
            "locations": [],
            "contacts": [],
            "social_links": [],
            "opening_hours": [],
            "notes": [],
            "sources": [],
            "first_seen": "2026-01-01T00:00:00+00:00",
            "last_seen": "2026-01-01T00:00:00+00:00",
            "schema_version": 1,
        },
    )
    res = client.get("/api/companies")
    assert res.status_code == 200, f"Got {res.status_code}: {res.text[:200]}"
    data = res.json()
    names = [c["company_name"] for c in data["companies"]]
    assert "Phones Dict Test" in names


def test_api_companies_handles_mixed_phones(
    client: Any, temp_data_root: Path
) -> None:
    """GET /api/companies must handle mixed string/dict phone entries."""
    _write_company(
        temp_data_root,
        "test-mixed-phones",
        {
            "company_id": "test-mixed-phones",
            "company_name": "Mixed Phones Test",
            "organization_category": "",
            "subcategory": "",
            "website": "",
            "emails": ["info@example.com", {"value": "sales@example.com"}],
            "phones": ["+1111111111", {"value": "+2222222222"}],
            "addresses": [],
            "locations": [],
            "contacts": [],
            "social_links": [],
            "opening_hours": [],
            "notes": [],
            "sources": [],
            "first_seen": "2026-01-01T00:00:00+00:00",
            "last_seen": "2026-01-01T00:00:00+00:00",
            "schema_version": 1,
        },
    )
    res = client.get("/api/companies")
    assert res.status_code == 200, f"Got {res.status_code}: {res.text[:200]}"
    data = res.json()
    names = [c["company_name"] for c in data["companies"]]
    assert "Mixed Phones Test" in names
