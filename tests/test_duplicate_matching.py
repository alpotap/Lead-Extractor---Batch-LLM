from services.storage import merge_company, write_json


WEIGHTS = {
    "email": 100,
    "phone": 100,
    "website_domain": 95,
    "address": 80,
    "contact": 70,
    "company_name": 30,
    "city": 20,
}


def _company(company_id, name, **fields):
    return {
        "company_id": company_id,
        "company_name": name,
        "organization_category": "",
        "subcategory": "",
        "website": "",
        "emails": [],
        "phones": [],
        "addresses": [],
        "locations": [],
        "contacts": [],
        "social_links": [],
        "opening_hours": [],
        "notes": [],
        "sources": [],
        **fields,
    }


def test_exact_name_only_records_merge_when_other_identifiers_are_missing(tmp_path):
    root = tmp_path
    (root / "companies").mkdir()
    (root / "crm").mkdir()
    existing = _company("breada-original", "Breada Bakery Haifa", sources=["homepage.html"])
    write_json(root / "companies" / "breada-original.json", existing)

    company_id, score, merged, candidates = merge_company(
        root,
        _company("", "BREADA, Bakery Haifa!"),
        "contact.html",
        WEIGHTS,
    )

    assert company_id == "breada-original"
    assert score >= 70
    assert merged is True
    assert len(candidates) == 1
    updated = (root / "companies" / "breada-original.json").read_text(encoding="utf-8")
    assert "contact.html" in updated
    assert len(list((root / "companies").glob("*.json"))) == 1


def test_exact_name_does_not_override_conflicting_locations(tmp_path):
    root = tmp_path
    (root / "companies").mkdir()
    (root / "crm").mkdir()
    existing = _company(
        "breada-haifa", "Breada Bakery", locations=["Haifa"], sources=["haifa.html"]
    )
    write_json(root / "companies" / "breada-haifa.json", existing)

    company_id, score, merged, _ = merge_company(
        root,
        _company("", "Breada Bakery", locations=["Tel Aviv"]),
        "tel-aviv.html",
        WEIGHTS,
    )

    assert company_id != "breada-haifa"
    assert score == 30
    assert merged is False
    assert len(list((root / "companies").glob("*.json"))) == 2
