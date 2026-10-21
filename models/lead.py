"""Shared lead data shapes and defaults."""

from typing import Any

ORGANIZATION_CATEGORIES: list[str] = []
DEFAULT_STATUSES = ["New", "Researching", "Contacted", "Interested", "Demo Scheduled", "Customer", "Not Interested"]
EXTRACTION_TEMPLATE: dict[str, Any] = {
    "company_name": "", "organization_category": "", "subcategory": "", "website": "", "emails": [],
    "phones": [], "addresses": [], "locations": [], "contacts": [], "social_links": [],
    "opening_hours": [], "notes": [], "source_language": "",
}
EXTRACTION_SCHEMA: dict[str, Any] = {
    **EXTRACTION_TEMPLATE,
    "contacts": [{"name": "", "roles": [], "emails": [], "phones": []}],
    "social_links": [{"name": "", "url": ""}],
    "opening_hours": [{"day": "", "hours": ""}],
}


def _string_list(value: Any) -> list[str]:
    values = value if isinstance(value, list) else ([] if value in (None, "") else [value])
    return [str(item).strip() for item in values if item is not None and str(item).strip()]


def normalize_contacts(value: Any) -> list[dict[str, Any]]:
    """Normalize old and new contact shapes and combine repeated people by name."""
    contacts = value if isinstance(value, list) else ([] if value in (None, "") else [value])
    result: list[dict[str, Any]] = []
    by_name: dict[str, list[dict[str, Any]]] = {}
    for contact in contacts:
        if isinstance(contact, str):
            contact = {"name": contact}
        if not isinstance(contact, dict):
            continue
        name = str(contact.get("name") or contact.get("person") or contact.get("full_name") or "").strip()
        roles = _string_list(contact.get("roles", contact.get("role", contact.get("title", []))))
        emails = _string_list(contact.get("emails", contact.get("email", [])))
        phones = _string_list(contact.get("phones", contact.get("phone", [])))
        if not (name or roles or emails or phones):
            continue
        normalized = {"name": name, "roles": roles, "emails": emails, "phones": phones}
        key = name.casefold()
        matches = by_name.get(key, []) if key else []
        existing = next((
            candidate for candidate in matches
            if not roles or not candidate["roles"] or set(roles).intersection(candidate["roles"])
        ), None)
        if existing is not None:
            for field, values in (("roles", roles), ("emails", emails), ("phones", phones)):
                existing[field] = list(dict.fromkeys(existing[field] + values))
        else:
            result.append(normalized)
            if key:
                by_name.setdefault(key, []).append(normalized)
    return result


def normalize_social_links(value: Any) -> list[dict[str, str]]:
    """Represent social accounts as a visible platform label plus its URL."""
    links = value if isinstance(value, list) else ([] if value in (None, "") else [value])
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for link in links:
        if isinstance(link, dict):
            name = str(link.get("name") or link.get("platform") or link.get("network") or "").strip()
            url = str(link.get("url") or link.get("href") or link.get("link") or "").strip()
        else:
            text = str(link).strip()
            if not text:
                continue
            url = text if text.startswith(("http://", "https://")) else ""
            host = url.split("/", 3)[2] if url else ""
            name = host.removeprefix("www.").split(".", 1)[0].title() if host else text
        if not name and url:
            host = url.split("/", 3)[2] if "/" in url else url
            name = host.removeprefix("www.").split(".", 1)[0].title()
        marker = (name.casefold(), url.casefold())
        if (name or url) and marker not in seen:
            result.append({"name": name, "url": url})
            seen.add(marker)
    return result


def normalize_opening_hours(value: Any) -> list[dict[str, str]]:
    """Normalize opening hours to one human-readable day/hours row per entry."""
    if isinstance(value, dict):
        entries = [{"day": day, "hours": hours} for day, hours in value.items()]
    elif isinstance(value, list):
        entries = value
    elif value in (None, ""):
        entries = []
    else:
        entries = [{"day": "", "hours": value}]
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for entry in entries:
        if isinstance(entry, dict):
            day = str(entry.get("day") or entry.get("days") or entry.get("day_of_week") or "").strip()
            hours = str(entry.get("hours") or entry.get("open") or entry.get("time") or ("Closed" if entry.get("closed") else "")).strip()
        else:
            day, hours = "", str(entry).strip()
        marker = (day.casefold(), hours.casefold())
        if (day or hours) and marker not in seen:
            result.append({"day": day, "hours": hours})
            seen.add(marker)
    return result


def empty_crm(company_id: str) -> dict[str, Any]:
    return {"company_id": company_id, "status": "New", "rating": "", "last_contacted": "", "next_followup": "", "notes": []}
