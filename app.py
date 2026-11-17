"""Local-first Streamlit lead manager."""

from __future__ import annotations

import math
import json
import html
import re
import shutil
import socket
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import asyncio
import streamlit as st

from models.lead import (
    DEFAULT_STATUSES,
    ORGANIZATION_CATEGORIES,
    normalize_contacts,
    normalize_opening_hours,
    normalize_social_links,
    EXTRACTION_TEMPLATE,
)
from services.ollama import is_local_url, is_ollama_cloud_url, list_models
from services.pipeline import configure_logging, process_incoming, start_background_scanner
from services.storage import (
    company_files, data_root, load_category_taxonomy, load_deleted_default_categories, load_companies, load_settings, read_json,
    rebuild_companies, safe_stem, save_category_taxonomy, save_ollama_api_key, save_settings, write_json,
)
from models.lead import empty_crm
from services.web_capture import capture_rendered_html

st.set_page_config(page_title="Lead Extractor", page_icon="🔎", layout="wide", initial_sidebar_state="collapsed")
st.logo(str(Path(__file__).resolve().parent / "assets" / "lead_extractor_logo.svg"), size="large")
st.markdown("""
<style>
div[data-testid="stMainBlockContainer"] { padding-top: 2rem; }
div[data-testid="stRadio"] { position: fixed; top: .35rem; left: 19rem; z-index: 1000000; width: fit-content; max-width: calc(100vw - 19.5rem); background: transparent; padding: 0; }
div[data-testid="stRadioGroup"] { gap: .5rem; border-bottom: 1px solid #d8dde3; padding-bottom: .35rem; }
label[data-testid="stRadioOption"] { padding: .45rem .9rem; border-radius: .45rem .45rem 0 0; font-size: 1.08rem; color: #303840; }
label[data-testid="stRadioOption"] > div > div:first-child { display: none; }
label[data-testid="stRadioOption"][data-selected="true"] { background: #1b7780; color: white; }
h3#items-that-need-attention [data-testid="stHeaderActionElements"],
h3#add-a-source [data-testid="stHeaderActionElements"] { display: none !important; }
div[class*='st-key-company-back-'] button { min-height: 2rem; padding: .35rem .55rem; font-size: .9rem; }
div[class*='st-key-FormSubmitter-edit-company-'] button[data-testid="stBaseButton-primaryFormSubmit"] { background-color: #2563eb !important; border-color: #2563eb !important; color: #fff !important; }
div[class*='st-key-FormSubmitter-edit-company-'] button[data-testid="stBaseButton-primaryFormSubmit"]:hover:not(:disabled) { background-color: #1d4ed8 !important; border-color: #1d4ed8 !important; color: #fff !important; }
@media (prefers-color-scheme: dark) {
  div[data-testid="stRadioGroup"] { border-color: #39434e; }
  label[data-testid="stRadioOption"] { color: #e7ebef; }
  label[data-testid="stRadioOption"][data-selected="true"] { color: #fff; }
}
@media (max-width: 760px) {
  div[data-testid="stMainBlockContainer"] { padding: 1rem .75rem 1rem !important; }
  div[data-testid="stRadio"] { position: relative; top: auto; left: auto; width: 100%; max-width: 100%; margin: 0 0 .75rem; }
  div[data-testid="stRadioGroup"] { flex-wrap: wrap; gap: .2rem; }
  label[data-testid="stRadioOption"] { padding: .35rem .55rem; font-size: .9rem; }
  div[data-testid="stHorizontalBlock"] { gap: .45rem; flex-wrap: wrap !important; }
  div[data-testid="stColumn"] { min-width: 0 !important; flex: 1 1 46% !important; }
  div[class*="st-key-company-list-header"] { display: none; }
  div[class*="st-key-company-list-row-"] { border: 1px solid #aab2bd55; border-radius: .6rem; padding: .45rem .6rem; margin: .45rem 0; }
  div[class*="st-key-company-list-row-"] div[data-testid="stHorizontalBlock"] { display: grid !important; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: .35rem .8rem !important; }
  div[class*="st-key-company-list-row-"] div[data-testid="stColumn"] { width: auto !important; min-width: 0 !important; flex: unset !important; }
  .mobile-field-label { display: block !important; color: #7b8490; font-size: .72rem; line-height: 1.1; }
  div[data-testid="stDataFrame"] { overflow-x: auto; }
}
.mobile-field-label { display: none; }
</style>
""", unsafe_allow_html=True)
settings = load_settings()
root = data_root(settings)
start_background_scanner(settings)
active_section = st.radio(
    "Main navigation", ["Companies", "Add a source", "Create lead manually", "Categories", "Review queue"],
    horizontal=True, label_visibility="collapsed", key="main-navigation",
)


def _date_value(value: str) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        for date_format in ("%d/%m/%y", "%d/%m/%Y"):
            try:
                return datetime.strptime(value, date_format).date()
            except ValueError:
                pass
        return None


def _short_date(value: Any) -> str:
    parsed = _date_value(str(value or ""))
    return parsed.strftime("%d/%m/%y") if parsed else ""


def _crm_path(company_id: str) -> Path:
    return root / "crm" / f"{safe_stem(company_id)}.json"


@st.cache_data(ttl=60, show_spinner=False)
def _cached_crm_summaries(root_path: str, company_ids: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    summaries: dict[str, dict[str, Any]] = {}
    for company_id in company_ids:
        record = read_json(Path(root_path) / "crm" / f"{safe_stem(company_id)}.json", {})
        record = record if isinstance(record, dict) else {}
        notes = record.get("notes", [])
        summaries[company_id] = {
            "status": record.get("status") or "New",
            "note_count": len(_followup_notes(notes)),
            "next_followup": record.get("next_followup", ""),
        }
    return summaries


@st.cache_data(ttl=60, show_spinner=False)
def _cached_company_summaries(root_path: str) -> list[dict[str, Any]]:
    summaries = []
    for path in company_files(Path(root_path)):
        record = read_json(path, {})
        if not isinstance(record, dict) or not record.get("company_id"):
            continue
        emails = record.get("emails", [])
        emails = emails if isinstance(emails, list) else [emails]
        phones = record.get("phones", [])
        phones = phones if isinstance(phones, list) else [phones]
        contacts = normalize_contacts(record.get("contacts", []))
        email_count = len(set(str(value).strip() for value in emails if str(value).strip()))
        phone_count = len(set(str(value).strip() for value in phones if str(value).strip()))
        email_count += len({value for contact in contacts for value in contact.get("emails", [])} - set(str(value).strip() for value in emails if str(value).strip()))
        phone_count += len({value for contact in contacts for value in contact.get("phones", [])} - set(str(value).strip() for value in phones if str(value).strip()))
        summaries.append({
            "company_id": str(record.get("company_id", "")),
            "company_name": str(record.get("company_name") or ""),
            "organization_category": str(record.get("organization_category") or ""),
            "subcategory": str(record.get("subcategory") or ""),
            "first_seen": str(record.get("first_seen") or ""),
            "last_seen": str(record.get("last_seen") or ""),
            "archived": bool(record.get("archived", False)),
            "locations": [str(value) for value in (record.get("locations", []) if isinstance(record.get("locations", []), list) else [record.get("locations")]) if value],
            "email_count": email_count,
            "phone_count": phone_count,
            "has_website": bool(str(record.get("website") or "").strip()),
            "social_count": len(normalize_social_links(record.get("social_links", []))),
        })
    return summaries


def _crm(company_id: str) -> dict[str, Any]:
    record = read_json(_crm_path(company_id), {})
    return record if isinstance(record, dict) else {}


def _display(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(item) if not isinstance(item, dict) else str(item.get("name") or item) for item in value) or "—"
    return str(value) if value else "—"


def _as_lines(value: Any) -> str:
    if not isinstance(value, list):
        value = [value] if value else []
    return "\n".join(str(item) for item in value)


def _editor_text(value: Any) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    return str(value).strip()


def _editor_records(value: Any) -> list[dict[str, Any]]:
    if hasattr(value, "to_dict"):
        return value.to_dict(orient="records")
    return list(value) if isinstance(value, list) else []


def _split_semicolon(value: Any) -> list[str]:
    return [part.strip() for part in _editor_text(value).split(";") if part.strip()]


def _followup_notes(value: Any) -> list[dict[str, str]]:
    """Read both the legacy string list and dated follow-up note records."""
    if not isinstance(value, list):
        value = [value] if value else []
    notes: list[dict[str, str]] = []
    for index, item in enumerate(value):
        if isinstance(item, dict):
            text = str(item.get("text") or "").strip()
            if text:
                notes.append({"id": str(item.get("id") or f"legacy-{index}"), "text": text, "created_at": str(item.get("created_at") or "")})
        elif str(item).strip():
            notes.append({"id": f"legacy-{index}", "text": str(item).strip(), "created_at": ""})
    return notes


def _save_crm(company_id: str, crm: dict[str, Any]) -> None:
    write_json(_crm_path(company_id), crm)
    _cached_crm_summaries.clear()


def _open_company(company_id: str) -> None:
    st.session_state["selected_company_id"] = company_id


def _sort_companies_by(column: str) -> None:
    current = st.session_state.get("company_sort_column", "Company")
    descending = st.session_state.get("company_sort_descending", False)
    if current == column:
        st.session_state["company_sort_descending"] = not descending
    else:
        st.session_state["company_sort_column"] = column
        st.session_state["company_sort_descending"] = False


def _save_model_selection() -> None:
    current_settings = load_settings()
    current_settings["selected_model"] = st.session_state.get("model_choice", "")
    save_settings(current_settings)


def _dismiss_review(path: Path) -> None:
    dismissed_dir = root / "review" / "dismissed"
    dismissed_dir.mkdir(parents=True, exist_ok=True)
    destination = dismissed_dir / path.name
    if destination.exists():
        destination = dismissed_dir / f"{path.stem}_{datetime.now().strftime('%Y%m%d%H%M%S%f')}{path.suffix}"
    shutil.move(str(path), str(destination))


with st.sidebar:
    st.header("Setup")
    data_path = st.text_input("Lead data folder", value=str(settings["data_path"]), help="Your business records and logs are stored here.")
    ollama_url = st.text_input("Ollama URL", value=settings["ollama_url"])
    ollama_api_key = str(settings.get("ollama_api_key", ""))
    if not is_local_url(ollama_url):
        ollama_api_key = st.text_input(
            "Ollama API key", value=ollama_api_key, type="password", key="ollama-api-key-input",
            help="Stored locally outside settings.json.",
        )
        st.caption("Add API key for Cloud hosted Ollama, leave empty for self hosted.")
    cloud_ollama = is_ollama_cloud_url(ollama_url)
    if cloud_ollama:
        selected_model = st.text_input(
            "Ollama Cloud model", value=str(settings.get("selected_model", "")),
            placeholder="e.g. gemma4:31b-cloud", key="ollama-cloud-model-input",
            help="Enter the exact model name supported by your Ollama Cloud account.",
        )
    else:
        try:
            available_models = list_models(ollama_url, ollama_api_key)
            model_options = available_models or ([settings["selected_model"]] if settings["selected_model"] else [])
            if available_models:
                st.caption(f"{len(available_models)} Ollama model(s) available")
            else:
                st.warning("No Ollama models found. The lead list and manual editing still work.")
        except Exception as exc:
            model_options = [settings["selected_model"]] if settings["selected_model"] else []
            st.warning(f"Ollama is unavailable: {exc}")
        selected_index = model_options.index(settings["selected_model"]) if settings["selected_model"] in model_options else (0 if model_options else None)
        selected_model = st.selectbox(
            "Extraction model", options=model_options, index=selected_index,
            placeholder="No model installed", key="model_choice", on_change=_save_model_selection,
        )
    if st.button("Save setup", use_container_width=True):
        settings["data_path"] = data_path
        settings["ollama_url"] = ollama_url.rstrip("/")
        settings["selected_model"] = str(selected_model or "").strip()
        settings["ollama_api_key"] = ollama_api_key
        save_ollama_api_key(ollama_api_key)
        save_settings(settings)
        st.success("Setup saved.")
        st.rerun()

    st.divider()
    st.subheader("Rebuild company records")
    st.caption("Uses saved extraction JSON. It does not call an AI model.")
    if st.button("Rebuild from saved data", type="primary", use_container_width=True):
        with st.status("Rebuilding company records…", expanded=True) as rebuild_status:
            progress_bar = st.progress(0, text="Starting rebuild")

            def update_progress(value: float, message: str) -> None:
                progress_bar.progress(min(1.0, max(0.0, value)), text=message)

            try:
                extraction_count, reviewed = rebuild_companies(root, settings["duplicate_weights"], update_progress)
                _cached_company_summaries.clear()
                company_count = len(load_companies(root))
                review_files = list((root / "review").glob("*.review.json"))
                missing_extractions = 0
                for review_file in review_files:
                    review = read_json(review_file, {})
                    source_name = str(review.get("source_file") or review_file.stem.removesuffix(".review")) if isinstance(review, dict) else ""
                    if source_name and not (root / "extracted" / f"{Path(source_name).stem}.extracted.json").exists():
                        missing_extractions += 1
                progress_bar.progress(1.0, text="Finished")
                rebuild_status.update(label=f"Built {company_count} companies from {extraction_count} saved extraction(s)", state="complete", expanded=False)
                st.success(f"Done. Built {company_count} companies from {extraction_count} saved extraction(s); {reviewed} invalid file(s) skipped.")
                if missing_extractions:
                    st.info(f"{missing_extractions} source(s) in Review queue have no saved extraction, so rebuild could not include them. Retry those sources from Review queue to extract their companies.")
            except Exception as exc:
                rebuild_status.update(label="Rebuild failed", state="error", expanded=True)
                configure_logging(root).exception("Company rebuild failed")
                st.error(f"Rebuild failed: {exc}")
                st.caption("Full traceback and the extraction filename are in logs/application.log.")

    with st.expander("Lead data and background scanning"):
        st.caption(f"Your lead files stay in `{root}` · Background scan every {settings['scan_interval_minutes']} minutes")

    st.divider()
    with st.expander("Network access"):
        try:
            lan_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                lan_socket.connect(("192.0.2.1", 80))
                lan_ip = lan_socket.getsockname()[0]
            finally:
                lan_socket.close()
            if lan_ip.startswith("127."):
                raise OSError("No LAN address detected")
            st.caption("Share this address with people on your local network:")
            st.code(f"http://{lan_ip}:8501", language=None)
        except OSError:
            st.info("Could not detect a LAN IP address. Make sure the computer is connected to the network.")
        st.caption("Others can connect while this app is running and their device can reach this computer on the network.")

if active_section == "Create lead manually":
    st.subheader("Create lead manually")
    st.write("Add a company record directly, without fetching a page or running an extraction.")
    with st.form("create-manual-lead"):
        company_name = st.text_input("Company name *")
        website = st.text_input("Website")
        category_options = ["", *sorted(load_category_taxonomy(root), key=str.casefold)]
        category = st.selectbox("Category", category_options, format_func=lambda value: value.replace("_", " ").title() if value else "—")
        email_values = st.text_area("Emails · one per line", height=80)
        phone_values = st.text_area("Phones · one per line", height=80)
        address_values = st.text_area("Addresses · one per line", height=80)
        location_values = st.text_area("Locations · one per line", height=80)
        notes_values = st.text_area("Notes · one per line", height=100)
        create_manual_lead = st.form_submit_button("Create lead", type="primary", disabled=not company_name.strip())
    if create_manual_lead:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        company_id = f"{safe_stem(company_name.strip())}-{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
        record = dict(EXTRACTION_TEMPLATE)
        record.update({
            "company_id": company_id,
            "company_name": company_name.strip(),
            "organization_category": category,
            "website": website.strip(),
            "emails": [item.strip() for item in email_values.splitlines() if item.strip()],
            "phones": [item.strip() for item in phone_values.splitlines() if item.strip()],
            "addresses": [item.strip() for item in address_values.splitlines() if item.strip()],
            "locations": [item.strip() for item in location_values.splitlines() if item.strip()],
            "notes": [item.strip() for item in notes_values.splitlines() if item.strip()],
            "sources": [], "first_seen": now, "last_seen": now, "schema_version": 1,
        })
        write_json(root / "companies" / f"{safe_stem(company_id)}.json", record)
        write_json(root / "crm" / f"{safe_stem(company_id)}.json", empty_crm(company_id))
        _cached_company_summaries.clear()
        _cached_crm_summaries.clear()
        st.success(f"Created lead: {company_name.strip()}")
        st.session_state["selected_company_id"] = company_id
        st.session_state["main-navigation"] = "Companies"
        st.rerun()

if active_section == "Add a source":
    st.subheader("Add a source")
    url_tab, source_tab = st.tabs(["Fetch from URL", "Paste text"])
    with url_tab:
        st.write("Paste one or more page URLs, separated by commas or new lines. Each page is loaded in a browser, given 10 seconds for dynamic content, and saved for processing.")
        page_urls_text = st.text_area("Company page URLs", height=140, placeholder="https://example.co.uk/contact\nhttps://example.com/about")
        page_urls = list(dict.fromkeys(value.strip() for value in re.split(r"[,\n\r]+", page_urls_text) if value.strip()))
        st.caption(f"{len(page_urls)} unique URL(s). Large batches run sequentially; allow roughly 10 seconds per page, plus page load time.")
        if st.button("Fetch pages and save HTML", type="primary", disabled=not page_urls):
            progress = st.progress(0, text=f"Starting batch of {len(page_urls)} URL(s)…")
            status = st.empty()
            results: list[dict[str, str]] = []
            for index, page_url in enumerate(page_urls, start=1):
                progress.progress((index - 1) / len(page_urls), text=f"Fetching {index}/{len(page_urls)}: {page_url}")
                try:
                    captured_html = asyncio.run(capture_rendered_html(page_url, settle_seconds=10))
                    host = safe_stem(urlparse(page_url).hostname or "website")
                    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                    target = root / "ingest" / "incoming" / f"web_{host}_{stamp}.html"
                    target.write_text(captured_html, encoding="utf-8")
                    configure_logging(root).info("Captured rendered URL source url=%s file=%s html_chars=%d", page_url, target.name, len(captured_html))
                    results.append({"#": str(index), "Status": "✅ Saved", "URL": page_url, "Details": target.name})
                except Exception as exc:
                    configure_logging(root).exception("Could not fetch URL source url=%s", page_url)
                    results.append({"#": str(index), "Status": "❌ Failed", "URL": page_url, "Details": str(exc)})
                status.dataframe(results, hide_index=True, use_container_width=True)
            progress.progress(1.0, text=f"Finished: {sum(row['Status'] == '✅ Saved' for row in results)}/{len(page_urls)} saved")
            succeeded = sum(row["Status"] == "✅ Saved" for row in results)
            if succeeded == len(page_urls):
                st.success(f"All {succeeded} page(s) saved. They will be processed on the next scan.")
            else:
                st.warning(f"{succeeded} of {len(page_urls)} page(s) saved; see the results above for failures. Failed URLs can be retried by pasting them again.")
    with source_tab:
        st.write("Paste text from a company or contact page. It will be saved and picked up by the next scan.")
        pasted = st.text_area("Source text", height=220, placeholder="Paste source text…")
        if st.button("Save source", type="primary", disabled=not pasted.strip()):
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            target = root / "ingest" / "incoming" / f"{stamp}.txt"
            target.write_text(pasted.strip() + "\n", encoding="utf-8")
            st.success(f"Saved {target.name}. It will be processed on the next scan.")

if active_section == "Categories":
    taxonomy = load_category_taxonomy(root)
    st.caption("Add or remove categories and subcategories. Removing an option does not change existing company records; their saved values remain visible.")
    category_col, parent_col = st.columns(2)
    with category_col:
        new_main_category = st.text_input("New main category (optional)")
    with parent_col:
        existing_categories = sorted(taxonomy, key=str.casefold)
        target_parent = st.selectbox("Add subcategories to", ["— Choose a category —", *existing_categories], format_func=lambda value: value.replace("_", " ").title() if value != "— Choose a category —" else value)
    subcategory_lines = st.text_area("Subcategories · one per line", placeholder="Children’s books\nPoetry\nEducational publishing", height=100)
    if st.button("Save categories", type="primary", disabled=not new_main_category.strip() and target_parent == "— Choose a category —"):
        parent = new_main_category.strip().replace("_", " ") or target_parent
        parent_key = next((name for name in taxonomy if name.casefold() == parent.casefold()), parent)
        children = taxonomy.setdefault(parent_key, [])
        known_children = {item.casefold() for item in children}
        for item in subcategory_lines.splitlines():
            child = item.strip().replace("_", " ")
            if child and child.casefold() not in known_children:
                children.append(child)
                known_children.add(child.casefold())
        save_category_taxonomy(root, taxonomy)
        st.success(f"Saved category “{parent_key}” and its subcategories.")
        st.rerun()
    st.markdown("**Current categories**")
    for parent, children in sorted(taxonomy.items(), key=lambda item: item[0].casefold()):
        child_text = ", ".join(children) if children else "No subcategories yet"
        st.caption(f"{parent.replace('_', ' ').title()} · {child_text.replace('_', ' ')}")
    st.markdown("**Delete a category or subcategory**")
    delete_parent_col, delete_button_col = st.columns([3, 1])
    with delete_parent_col:
        delete_parent = st.selectbox(
            "Category to delete", ["— Choose a category —", *sorted(taxonomy, key=str.casefold)],
            format_func=lambda value: value.replace("_", " ").title() if value != "— Choose a category —" else value,
            key="delete-category-parent",
        )
    with delete_button_col:
        st.write("")
        st.write("")
        if st.button("Delete category", key="delete-category", disabled=delete_parent == "— Choose a category —"):
            taxonomy.pop(delete_parent, None)
            deleted_defaults = load_deleted_default_categories(root)
            if delete_parent in ORGANIZATION_CATEGORIES and delete_parent not in deleted_defaults:
                deleted_defaults.append(delete_parent)
            save_category_taxonomy(root, taxonomy, deleted_defaults)
            st.success(f"Removed “{delete_parent}” from the category choices. Existing company data was left unchanged.")
            st.rerun()
    deletable_children = taxonomy.get(delete_parent, [])
    if delete_parent != "— Choose a category —" and deletable_children:
        delete_child_col, delete_child_button_col = st.columns([3, 1])
        with delete_child_col:
            delete_child = st.selectbox("Subcategory to delete", deletable_children, key="delete-category-child")
        with delete_child_button_col:
            st.write("")
            st.write("")
            if st.button("Delete subcategory", key="delete-subcategory"):
                taxonomy[delete_parent] = [item for item in taxonomy[delete_parent] if item != delete_child]
                save_category_taxonomy(root, taxonomy)
                st.success(f"Removed “{delete_child}” from the choices. Existing company data was left unchanged.")
                st.rerun()

if active_section == "Review queue":
    review_dir = root / "review"
    review_files = sorted(review_dir.glob("*.review.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    st.subheader("Items that need attention")
    st.caption("Dismiss anything you no longer need to act on. Dismissed items are kept out of this list and can be viewed below.")
    if review_files:
        for review_file in review_files:
            review = read_json(review_file, {})
            if not isinstance(review, dict):
                review = {}
            source_name = str(review.get("source_file") or review_file.stem.removesuffix(".review"))
            created_at = str(review.get("created_at") or "")
            try:
                flagged_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                if flagged_at.tzinfo is not None:
                    flagged_at = flagged_at.astimezone()
            except ValueError:
                flagged_at = datetime.fromtimestamp(review_file.stat().st_mtime)
            date_value = flagged_at.date()
            age_days = (date.today() - date_value).days
            timestamp = flagged_at.strftime("%Y-%m-%d %H:%M:%S")
            label = f"{source_name} · {review.get('reason', 'needs review')} · {timestamp}"
            with st.expander(label):
                if age_days > 30:
                    st.warning(f"Older item · {age_days} days old. Check whether it still needs attention.")
                st.write(f"**Flagged:** {timestamp}")
                if review.get("error"):
                    st.error(str(review["error"]))
                if review.get("confidence") is not None:
                    st.write(f"Confidence: {review['confidence']:.2f}")
                if review.get("extraction"):
                    st.json(review["extraction"])

                action_col, dismiss_col = st.columns([2, 1])
                file_name = Path(source_name).name
                archived_source = root / "ingest" / "archived" / file_name
                incoming_source = root / "ingest" / "incoming" / file_name
                with action_col:
                    if st.button("Retry with selected model", key=f"retry-{review_file.stem}", disabled=not archived_source.exists()):
                        settings["data_path"] = str(root)
                        settings["ollama_url"] = ollama_url.rstrip("/")
                        settings["selected_model"] = str(selected_model or "").strip()
                        settings["ollama_api_key"] = ollama_api_key
                        save_ollama_api_key(ollama_api_key)
                        save_settings(settings)
                        if not incoming_source.exists():
                            shutil.copy2(archived_source, incoming_source)
                        with st.spinner("Retrying source…"):
                            outcome = process_incoming(settings, only_names={file_name})
                        st.success(f"Retry finished: {outcome['new']} new, {outcome['merged']} merged, {outcome['review']} still need attention.")
                        st.rerun()
                with dismiss_col:
                    if st.button("Dismiss", key=f"dismiss-{review_file.stem}"):
                        _dismiss_review(review_file)
                        st.rerun()
    else:
        st.success("Nothing needs attention.")

    dismissed_dir = review_dir / "dismissed"
    dismissed_files = sorted(dismissed_dir.glob("*.review.json"), key=lambda path: path.stat().st_mtime, reverse=True) if dismissed_dir.exists() else []
    with st.expander(f"Dismissed items ({len(dismissed_files)})"):
        for dismissed_file in dismissed_files:
            dismissed = read_json(dismissed_file, {})
            dismissed = dismissed if isinstance(dismissed, dict) else {}
            dismissed_date = _date_value(str(dismissed.get("created_at") or "")) or datetime.fromtimestamp(dismissed_file.stat().st_mtime).date()
            st.caption(f"{dismissed.get('source_file') or dismissed_file.stem} · {dismissed_date.isoformat()}")

if active_section == "Companies":
    selected_id = str(st.query_params.get("company_id") or st.session_state.get("selected_company_id") or "")
    selected_company = None
    if selected_id:
        with st.spinner("Loading company details…"):
            record = read_json(root / "companies" / f"{safe_stem(str(selected_id))}.json", {})
        selected_company = record if isinstance(record, dict) and record.get("company_id") else None

    if selected_company:
        company = selected_company
        company_id = str(company.get("company_id", ""))
        header_back, header_name, header_dates, header_copy, header_archive = st.columns([1.2, 2.8, 2.2, 1.4, 1.1], vertical_alignment="center")
        with header_back:
            if st.button("← Companies", key=f"company-back-{safe_stem(company_id)}", use_container_width=True):
                st.session_state.pop("selected_company_id", None)
                st.query_params.clear()
                st.rerun()
        with header_name:
            st.markdown(f"### {company.get('company_name') or 'Unnamed company'}")
        with header_dates:
            added_label = _short_date(company.get("first_seen")) or "unknown"
            updated_label = _short_date(company.get("last_seen")) or "unknown"
            st.markdown(
                f"<div style='display:flex;gap:.8rem;align-items:center;white-space:nowrap;color:var(--text-color);font-size:.85rem'>"
                f"<span>Added {added_label}</span><span>Updated {updated_label}</span></div>",
                unsafe_allow_html=True,
            )

        copy_payload = {"company": company, "follow_up": _crm(company_id)}
        copy_json = json.dumps(copy_payload, ensure_ascii=False, indent=2).replace("<", "\\u003c")
        with header_copy:
            st.components.v1.html(f"""
<style>html, body {{ margin: 0; padding: 0; overflow: hidden; }}</style>
<div style="display:flex;align-items:center;gap:.5rem;font:14px sans-serif">
<button id="copy-company-json" style="font: 13px sans-serif; padding: .4rem .55rem; border: 1px solid #8885; border-radius: .45rem; cursor: pointer; flex:1; white-space:nowrap; min-height:2rem">
  Copy as JSON
</button>
<span id="copy-result" style="font:12px sans-serif; white-space:nowrap"></span>
</div>
<script>
const jsonText = {json.dumps(copy_json)};
const button = document.getElementById('copy-company-json');
const result = document.getElementById('copy-result');
function applyTheme() {{
  try {{
    const parentDocument = window.parent.document;
    const app = parentDocument.querySelector('.stApp');
    const appStyle = window.parent.getComputedStyle(app);
    const surfaceColor = appStyle.backgroundColor;
    const rgb = surfaceColor.match(/[\\d.]+/g)?.map(Number) || [];
    const isLight = rgb.length >= 3 ? (rgb[0] + rgb[1] + rgb[2]) / 3 > 127 : !matchMedia('(prefers-color-scheme: dark)').matches;
    button.style.backgroundColor = isLight ? '#f0f2f6' : '#262730';
    button.style.color = isLight ? '#1f2937' : '#f3f4f6';
    button.style.borderColor = isLight ? '#aab2bd' : '#6b7280';
    result.style.color = isLight ? '#1f2937' : '#f3f4f6';
  }} catch (error) {{
    button.style.backgroundColor = matchMedia('(prefers-color-scheme: dark)').matches ? '#262730' : '#f0f2f6';
    button.style.color = matchMedia('(prefers-color-scheme: dark)').matches ? '#fafafa' : '#262730';
  }}
}}
applyTheme();
button.addEventListener('click', async () => {{
  try {{
    await window.parent.navigator.clipboard.writeText(jsonText);
    result.textContent = 'Copied';
  }} catch (error) {{
    try {{
      const parentDocument = window.parent.document;
      const area = parentDocument.createElement('textarea');
      area.value = jsonText;
      area.style.position = 'fixed'; area.style.opacity = '0';
      parentDocument.body.appendChild(area); area.select();
      const copied = parentDocument.execCommand('copy'); area.remove();
      result.textContent = copied ? 'Copied' : 'Copy was blocked by the browser';
    }} catch (fallbackError) {{
      result.textContent = 'Copy was blocked by the browser';
    }}
  }}
}});
</script>
""", height=38, scrolling=False)
        with header_archive:
            is_archived = bool(company.get("archived", False))
            if st.button("Restore" if is_archived else "Archive", key=f"archive-company-{safe_stem(company_id)}", help="Archived companies stay available for matching new sources."):
                company["archived"] = not is_archived
                write_json(root / "companies" / f"{safe_stem(company_id)}.json", company)
                _cached_company_summaries.clear()
                st.rerun()

        company_column, followup_column = st.columns([3, 2], gap="small")
        with company_column:
            st.markdown("#### Company details")
            taxonomy = load_category_taxonomy(root)
            categories = sorted(taxonomy, key=str.casefold)
            current_category = str(company.get("organization_category") or "")
            if current_category.casefold() == "unknown":
                current_category = ""
            if current_category and current_category not in categories:
                categories.append(current_category)
            category_options = ["", *categories]
            category_col, subcategory_col = st.columns(2)
            with category_col:
                category = st.selectbox(
                    "Category", category_options,
                    index=category_options.index(current_category),
                    format_func=lambda value: value.replace("_", " ").title() if value else "—",
                    key=f"company-category-{safe_stem(company_id)}",
                )
            with subcategory_col:
                subcategories = list(taxonomy.get(category, []))
                current_subcategory = str(company.get("subcategory") or "")
                if current_subcategory and current_subcategory not in subcategories:
                    subcategories.append(current_subcategory)
                subcategory = st.selectbox(
                    "Subcategory", ["", *subcategories],
                    index=(subcategories.index(current_subcategory) + 1) if current_subcategory in subcategories else 0,
                    format_func=lambda value: value or "—",
                    key=f"company-subcategory-{safe_stem(company_id)}",
                )
            with st.form(f"edit-company-{safe_stem(company_id)}"):
                name_col, website_col = st.columns([2, 2])
                with name_col:
                    name = st.text_input("Company name", value=str(company.get("company_name", "")))
                with website_col:
                    website = st.text_input("Website", value=str(company.get("website", "")))

                email_col, phone_col = st.columns(2)
                with email_col:
                    email_text = st.text_area("Emails · one per line", value=_as_lines(company.get("emails", [])), height=80)
                with phone_col:
                    phone_text = st.text_area("Phones · one per line", value=_as_lines(company.get("phones", [])), height=80)
                address_col, location_col = st.columns(2)
                with address_col:
                    address_text = st.text_area("Addresses · one per line", value=_as_lines(company.get("addresses", [])), height=80)
                with location_col:
                    location_text = st.text_area("Locations · one per line", value=_as_lines(company.get("locations", [])), height=80)

                existing_contacts = normalize_contacts(company.get("contacts", []))
                contact_rows = [{
                    "Name": contact.get("name", ""),
                    "Role or title": "; ".join(contact.get("roles", [])),
                    "Emails · separate with ;": "; ".join(contact.get("emails", [])),
                    "Phones · separate with ;": "; ".join(contact.get("phones", [])),
                } for contact in existing_contacts]
                if not contact_rows:
                    contact_rows = [{"Name": "", "Role or title": "", "Emails · separate with ;": "", "Phones · separate with ;": ""}]
                st.markdown("**People and contacts**")
                edited_contacts = st.data_editor(
                    contact_rows, key=f"contacts-{safe_stem(company_id)}", num_rows="dynamic",
                    use_container_width=True, hide_index=True,
                    column_config={
                        "Name": st.column_config.TextColumn("Name"),
                        "Role or title": st.column_config.TextColumn("Role or title"),
                        "Emails · separate with ;": st.column_config.TextColumn("Emails · separate with ;"),
                        "Phones · separate with ;": st.column_config.TextColumn("Phones · separate with ;"),
                    },
                )

                social_rows = [
                    {"Platform": link.get("name", ""), "URL": link.get("url", "")}
                    for link in normalize_social_links(company.get("social_links", []))
                ]
                if not social_rows:
                    social_rows = [{"Platform": "", "URL": ""}]
                hours_rows = normalize_opening_hours(company.get("opening_hours", []))
                if not hours_rows:
                    hours_rows = [{"day": "", "hours": ""}]
                socials_col, hours_col = st.columns(2)
                with socials_col:
                    st.markdown("**Social accounts**")
                    edited_socials = st.data_editor(
                        social_rows, key=f"socials-{safe_stem(company_id)}", num_rows="dynamic",
                        use_container_width=True, hide_index=True,
                        column_config={
                            "Platform": st.column_config.TextColumn("Platform"),
                            "URL": st.column_config.LinkColumn("Profile URL"),
                        },
                    )
                with hours_col:
                    st.markdown("**Opening hours**")
                    edited_hours = st.data_editor(
                        hours_rows, key=f"hours-{safe_stem(company_id)}", num_rows="dynamic",
                        use_container_width=True, hide_index=True,
                        column_config={
                            "day": st.column_config.TextColumn("Day or days"),
                            "hours": st.column_config.TextColumn("Hours"),
                        },
                    )
                notes_text = st.text_area("Company notes · one per line", value=_as_lines(company.get("notes", [])), height=100)
                save_company = st.form_submit_button("Save company changes", type="primary")

            if save_company:
                contacts = []
                for row in _editor_records(edited_contacts):
                    contact_name = _editor_text(row.get("Name"))
                    if not contact_name:
                        continue
                    contacts.append({
                        "name": contact_name,
                        "roles": _split_semicolon(row.get("Role or title")),
                        "emails": _split_semicolon(row.get("Emails · separate with ;")),
                        "phones": _split_semicolon(row.get("Phones · separate with ;")),
                    })
                socials = [
                    {"name": _editor_text(row.get("Platform")), "url": _editor_text(row.get("URL"))}
                    for row in _editor_records(edited_socials)
                    if _editor_text(row.get("Platform")) or _editor_text(row.get("URL"))
                ]
                hours = [
                    {"day": _editor_text(row.get("day")), "hours": _editor_text(row.get("hours"))}
                    for row in _editor_records(edited_hours)
                    if _editor_text(row.get("day")) or _editor_text(row.get("hours"))
                ]
                company.update({
                    "company_name": name.strip(),
                    "organization_category": category,
                    "subcategory": subcategory,
                    "website": website.strip(),
                    "emails": [line.strip() for line in email_text.splitlines() if line.strip()],
                    "phones": [line.strip() for line in phone_text.splitlines() if line.strip()],
                    "addresses": [line.strip() for line in address_text.splitlines() if line.strip()],
                    "locations": [line.strip() for line in location_text.splitlines() if line.strip()],
                    "social_links": normalize_social_links(socials),
                    "opening_hours": normalize_opening_hours(hours),
                    "notes": [line.strip() for line in notes_text.splitlines() if line.strip()],
                    "contacts": normalize_contacts(contacts),
                    "last_seen": datetime.now().astimezone().isoformat(timespec="seconds"),
                })
                write_json(root / "companies" / f"{safe_stem(company_id)}.json", company)
                _cached_company_summaries.clear()
                st.success("Company record saved.")
                st.rerun()

            source_files = company.get("sources", [])
            if not isinstance(source_files, list):
                source_files = [source_files] if source_files else []
            with st.expander(f"Original sources ({len(source_files)})"):
                source_names = [Path(str(source)).name for source in source_files if str(source).strip()]
                if source_names:
                    selected_source = st.selectbox("Archived source", source_names, key=f"source-{safe_stem(company_id)}")
                    archived_source = root / "ingest" / "archived" / selected_source
                    if st.button("Re-extract with selected model", key=f"reextract-{safe_stem(company_id)}", disabled=not archived_source.exists() or not selected_model):
                        settings["data_path"] = str(root)
                        settings["ollama_url"] = ollama_url.rstrip("/")
                        settings["selected_model"] = str(selected_model or "").strip()
                        settings["ollama_api_key"] = ollama_api_key
                        save_ollama_api_key(ollama_api_key)
                        save_settings(settings)
                        incoming_source = root / "ingest" / "incoming" / selected_source
                        if not incoming_source.exists():
                            shutil.copy2(archived_source, incoming_source)
                        with st.spinner("Re-extracting the archived source and merging any new details…"):
                            outcome = process_incoming(settings, only_names={selected_source})
                        st.info(f"Re-extraction finished: {outcome['new']} new, {outcome['merged']} merged, {outcome['review']} sent for review.")
                        st.rerun()
                else:
                    st.caption("No archived source names are attached to this company yet.")

        with followup_column:
            st.markdown("#### Follow-up")
            crm = _crm(company_id)
            choices = list(DEFAULT_STATUSES)
            current_status = str(crm.get("status") or "New")
            if current_status not in choices:
                choices.append(current_status)
            with st.form(f"crm-{safe_stem(company_id)}"):
                status_col, rating_col = st.columns([2, 1])
                with status_col:
                    status = st.selectbox("Status", choices, index=choices.index(current_status))
                with rating_col:
                    rating = st.text_input("Rating", value=str(crm.get("rating", "")))
                original_last_date = _date_value(str(crm.get("last_contacted", "")))
                original_next_date = _date_value(str(crm.get("next_followup", "")))
                last_date_col, next_date_col = st.columns(2)
                with last_date_col:
                    last_contacted_picker = st.date_input("Last contacted", value=original_last_date, format="DD/MM/YYYY", key=f"last-date-picker-{safe_stem(company_id)}")
                with next_date_col:
                    next_followup_picker = st.date_input("Next follow-up", value=original_next_date, format="DD/MM/YYYY", key=f"next-date-picker-{safe_stem(company_id)}")
                save_crm = st.form_submit_button("Save follow-up details")
            if save_crm:
                crm.update({
                    "company_id": company_id, "status": status, "rating": rating,
                    "last_contacted": last_contacted_picker.strftime("%d/%m/%y") if last_contacted_picker else "",
                    "next_followup": next_followup_picker.strftime("%d/%m/%y") if next_followup_picker else "",
                })
                _save_crm(company_id, crm)
                st.success("Follow-up details saved.")
                st.rerun()

            st.markdown("**Follow-up notes**")
            notes = _followup_notes(crm.get("notes", []))
            editing_note_id = st.session_state.get(f"editing-note-{safe_stem(company_id)}")
            note_input_col, note_add_col = st.columns([4, 1], vertical_alignment="bottom")
            with note_input_col:
                new_note = st.text_area("Add a note", key=f"new-note-{safe_stem(company_id)}", height=70, label_visibility="collapsed", placeholder="Write a follow-up note…")
            with note_add_col:
              add_note = st.button("Add", key=f"add-note-{safe_stem(company_id)}", type="secondary", disabled=not new_note.strip(), use_container_width=True)
            if add_note:
                notes.append({"id": datetime.now().astimezone().strftime("%Y%m%d%H%M%S%f"), "text": new_note.strip(), "created_at": datetime.now().astimezone().isoformat(timespec="seconds")})
                crm["notes"] = notes
                _save_crm(company_id, crm)
                st.rerun()

            notes.sort(key=lambda note: (note["created_at"] or "", note["id"]), reverse=True)
            for note in notes:
                note_id = note["id"]
                if editing_note_id == note_id:
                    edited_note = st.text_area("Edit note", value=note["text"], key=f"edit-note-text-{safe_stem(company_id)}-{note_id}", height=75)
                    save_note_col, cancel_note_col = st.columns([1, 1])
                    with save_note_col:
                        if st.button("Save", key=f"save-note-{safe_stem(company_id)}-{note_id}"):
                            for current_note in notes:
                                if current_note["id"] == note_id:
                                    current_note["text"] = edited_note.strip()
                            crm["notes"] = [item for item in notes if item["text"]]
                            _save_crm(company_id, crm)
                            st.session_state.pop(f"editing-note-{safe_stem(company_id)}", None)
                            st.rerun()
                    with cancel_note_col:
                        if st.button("Cancel", key=f"cancel-note-{safe_stem(company_id)}-{note_id}"):
                            st.session_state.pop(f"editing-note-{safe_stem(company_id)}", None)
                            st.rerun()
                    continue

                note_col, edit_col, delete_col = st.columns([10, 0.7, 0.7])
                with note_col:
                    note_date = _short_date(note["created_at"]) if note["created_at"] else "Date unknown"
                    st.caption(note_date)
                    st.write(note["text"])
                with edit_col:
                    if st.button("", key=f"edit-note-{safe_stem(company_id)}-{note_id}", icon=":material/edit:", help="Edit note"):
                        st.session_state[f"editing-note-{safe_stem(company_id)}"] = note_id
                        st.rerun()
                with delete_col:
                    if st.button("", key=f"delete-note-{safe_stem(company_id)}-{note_id}", icon=":material/delete:", help="Delete note"):
                        crm["notes"] = [item for item in notes if item["id"] != note_id]
                        _save_crm(company_id, crm)
                        st.rerun()

    else:
        with st.spinner("Loading companies…"):
            companies = _cached_company_summaries(str(root))
        show_advanced_search = bool(st.session_state.get("show_advanced_search", False))
        search_col, refresh_col, columns_col, advanced_col = st.columns([2.8, 0.35, 0.9, 1.7], vertical_alignment="bottom")
        with search_col:
            search = st.text_input("Search company, contact, email, or phone")
        with refresh_col:
            if st.button("", key="refresh-companies", icon=":material/refresh:", help="Refresh companies from disk", use_container_width=True):
                _cached_company_summaries.clear()
                _cached_crm_summaries.clear()
                st.rerun()
        list_columns = ("Company", "Status", "Category", "Subcategory", "Email", "Website", "Phone", "Socials", "Created", "Notes", "Next follow-up", "Updated")
        selected_columns = st.session_state.get("company_list_columns", list(list_columns))
        selected_columns = [label for label in list_columns if label in selected_columns]
        with columns_col:
            with st.popover("Columns", use_container_width=True):
                selected_columns = st.multiselect(
                    "Visible columns", list_columns, default=selected_columns,
                    key="company-list-column-picker",
                )
                st.session_state["company_list_columns"] = selected_columns
        with advanced_col:
            if st.button("▾ Advanced search" if not show_advanced_search else "▴ Advanced search", key="toggle-advanced-search", use_container_width=True):
                if show_advanced_search:
                    st.session_state["saved_search_category"] = st.session_state.get("advanced-search-category", [])
                    st.session_state["saved_search_location"] = st.session_state.get("advanced-search-location", [])
                    st.session_state["saved_search_added_after"] = st.session_state.get("advanced-search-added-after")
                    st.session_state["saved_search_added_before"] = st.session_state.get("advanced-search-added-before")
                st.session_state["show_advanced_search"] = not show_advanced_search
                st.rerun()

        category_options = sorted({
            "" if str(company.get("organization_category") or "").casefold() == "unknown"
            else str(company.get("organization_category") or "")
            for company in companies
        })
        location_options = sorted({
            location for company in companies
            for location in (company.get("locations", []) if isinstance(company.get("locations", []), list) else [company.get("locations")])
            if location
        })
        company_ids = tuple(str(company.get("company_id", "")) for company in companies)
        crm_by_id = _cached_crm_summaries(str(root), company_ids)

        status_options = sorted({str(crm_by_id.get(str(company.get("company_id", "")), {}).get("status") or "New") for company in companies}, key=str.casefold)
        category_column_options = sorted({
            "" if str(company.get("organization_category") or "").casefold() == "unknown" else str(company.get("organization_category") or "")
            for company in companies
        }, key=lambda value: value.casefold())
        subcategory_options = sorted({str(company.get("subcategory") or "") for company in companies}, key=str.casefold)
        column_filter_options = {
            "Status": status_options,
            "Category": category_column_options,
            "Subcategory": subcategory_options,
            "Website": ["Has entries", "No entries"],
            "Phone": ["Has entries", "No entries"],
            "Socials": ["Has entries", "No entries"],
        }
        column_filters = st.session_state.get("company_column_filters", {})
        column_filters = {
            label: [value for value in column_filters.get(label, options) if value in options]
            for label, options in column_filter_options.items()
        }

        filter_controls = st.columns(6)
        for filter_col, label in zip(filter_controls, column_filter_options):
            with filter_col:
                with st.popover(label, icon=":material/filter_alt:", key=f"filter-companies-{label}", use_container_width=True):
                    st.markdown(f"**Filter {label}**")
                    column_filters[label] = st.multiselect(
                        "Options", column_filter_options[label],
                        default=column_filters[label], key=f"company-filter-values-{label}",
                        format_func=lambda value: value.replace("_", " ").title() if value else "—",
                    )
        st.session_state["company_column_filters"] = column_filters

        primary_default = st.session_state.get("company_sort_column", "Company")
        if "company-sort-primary-select" not in st.session_state:
            st.session_state["company-sort-primary-select"] = primary_default
        sort_controls = st.columns([1.5, 1.5, 0.7, 1.5, 0.7])
        sort_column = sort_controls[0].selectbox("Sort by", list_columns, key="company-sort-primary-select")
        sort_descending = sort_controls[1].checkbox(
            "Descending", value=bool(st.session_state.get("company_sort_descending", False)),
            key="company-sort-primary-desc",
        )
        secondary_options = [None, *[label for label in list_columns if label != sort_column]]
        previous_secondary = st.session_state.get("company-sort-secondary-select", st.session_state.get("company_sort_secondary_column"))
        if previous_secondary == sort_column:
            previous_secondary = None
        if previous_secondary not in secondary_options:
            previous_secondary = None
        st.session_state["company-sort-secondary-select"] = previous_secondary
        sort_secondary_column = sort_controls[3].selectbox(
            "Then by", secondary_options, format_func=lambda value: value or "None",
            key="company-sort-secondary-select",
        )
        sort_secondary_descending = sort_controls[4].checkbox(
            "Descending", value=bool(st.session_state.get("company_sort_secondary_descending", False)),
            key="company-sort-secondary-desc",
            disabled=sort_secondary_column is None,
        )
        st.session_state["company_sort_column"] = sort_column
        st.session_state["company_sort_descending"] = sort_descending
        st.session_state["company_sort_secondary_column"] = sort_secondary_column
        st.session_state["company_sort_secondary_descending"] = sort_secondary_descending

        category_filter = st.session_state.get("saved_search_category", [])
        category_filter = [
            value for value in dict.fromkeys("" if str(value).casefold() == "unknown" else value for value in category_filter)
            if value in category_options
        ]
        location_filter = st.session_state.get("saved_search_location", [])
        added_after = st.session_state.get("saved_search_added_after")
        added_before = st.session_state.get("saved_search_added_before")
        show_archived = bool(st.session_state.get("show_archived_companies", False))
        if show_advanced_search:
            advanced_category, advanced_location, advanced_after, advanced_before, archived_col = st.columns([1.4, 1.5, 1, 1, 1.2])
            with advanced_category:
                category_filter = st.multiselect("Category", category_options, default=category_filter, key="advanced-search-category", format_func=lambda value: value.replace("_", " ").title() if value else "—")
            with advanced_location:
                location_filter = st.multiselect("Location", location_options, default=location_filter, key="advanced-search-location")
            with advanced_after:
                added_after = st.date_input("Added from", value=added_after, format="DD/MM/YYYY", key="advanced-search-added-after")
            with advanced_before:
                added_before = st.date_input("Added through", value=added_before, format="DD/MM/YYYY", key="advanced-search-added-before")
            with archived_col:
                show_archived = st.checkbox("Show archived", value=show_archived, key="show_archived_companies")
            st.session_state["saved_search_category"] = category_filter
            st.session_state["saved_search_location"] = location_filter
            st.session_state["saved_search_added_after"] = added_after
            st.session_state["saved_search_added_before"] = added_before

        notes_id = str(st.query_params.get("notes_id") or "")
        entry_id = str(st.query_params.get("entry_id") or "")
        entry_kind = str(st.query_params.get("entry_kind") or "")
        if notes_id or entry_id:
            detail_id = notes_id or entry_id
            detail_record = read_json(root / "companies" / f"{safe_stem(detail_id)}.json", {})
            if isinstance(detail_record, dict) and detail_record:
                with st.container(border=True):
                    detail_heading = "Company notes" if notes_id else f"{entry_kind.title()} details"
                    st.markdown(f"**{html.escape(str(detail_record.get('company_name') or 'Company'))} · {detail_heading}**")
                    if notes_id:
                        raw_notes = detail_record.get("notes", [])
                        if isinstance(raw_notes, list):
                            notes_text = "\n\n".join(str(note.get("text") or "") if isinstance(note, dict) else str(note) for note in raw_notes).strip()
                        else:
                            notes_text = str(raw_notes or "").strip()
                        st.write(notes_text or "No company notes available.")
                    else:
                        contacts = normalize_contacts(detail_record.get("contacts", []))
                        if entry_kind == "email":
                            raw_values = detail_record.get("emails", [])
                            raw_values = raw_values if isinstance(raw_values, list) else [raw_values]
                            values = list(dict.fromkeys([str(value).strip() for value in raw_values if str(value).strip()] + [value for contact in contacts for value in contact.get("emails", [])]))
                            for value in values:
                                st.code(value, language=None)
                        elif entry_kind == "phone":
                            raw_values = detail_record.get("phones", [])
                            raw_values = raw_values if isinstance(raw_values, list) else [raw_values]
                            values = list(dict.fromkeys([str(value).strip() for value in raw_values if str(value).strip()] + [value for contact in contacts for value in contact.get("phones", [])]))
                            for value in values:
                                st.code(value, language=None)
                        elif entry_kind == "website":
                            website = str(detail_record.get("website") or "").strip()
                            if website:
                                st.code(website, language=None)
                                safe_website = website if website.startswith(("http://", "https://")) else f"https://{website}"
                                st.markdown(f"<a href='{html.escape(safe_website, quote=True)}' target='_blank' rel='noopener noreferrer'>Open website ↗</a>", unsafe_allow_html=True)
                        elif entry_kind == "socials":
                            for index, social in enumerate(normalize_social_links(detail_record.get("social_links", []))):
                                social_url = str(social.get("url") or "").strip()
                                social_name = str(social.get("name") or "Social")
                                if social_url:
                                    safe_url = social_url if social_url.startswith(("http://", "https://")) else f"https://{social_url}"
                                    safe_url_js = json.dumps(safe_url, ensure_ascii=False).replace("<", "\\u003c")
                                    safe_url_html = html.escape(safe_url, quote=True)
                                    st.components.v1.html(f"""
<style>html,body{{margin:0;padding:0;font:14px sans-serif;overflow:hidden}}.entry{{display:flex;align-items:center;gap:.6rem;min-height:2.4rem}}.entry a{{color:#1b7780;overflow-wrap:anywhere;flex:1}}.entry button{{border:1px solid #aab2bd;border-radius:.4rem;background:#f0f2f6;color:#1f2937;padding:.35rem .65rem;cursor:pointer;white-space:nowrap}}</style>
<div class="entry"><a href="{safe_url_html}" target="_blank" rel="noopener noreferrer">{safe_url_html}</a><button id="copy-link-{index}">Copy</button><span id="copy-state-{index}"></span></div>
<script>document.getElementById('copy-link-{index}').addEventListener('click',async()=>{{try{{await window.parent.navigator.clipboard.writeText({safe_url_js});document.getElementById('copy-state-{index}').textContent='Copied';}}catch(error){{document.getElementById('copy-state-{index}').textContent='Copy failed';}}}});</script>
""", height=46, scrolling=False)
                                else:
                                    st.code(social_name, language=None)
                    if st.button("Close", key=f"close-list-detail-{detail_id}-{entry_kind}"):
                        st.query_params.clear()
                        st.rerun()

        visible: list[dict[str, Any]] = []
        query = search.casefold().strip()
        for company in companies:
            if company.get("archived", False) and not show_archived:
                continue
            haystack = " ".join([
                str(company.get("company_name", "")), str(company.get("organization_category", "")),
                str(company.get("subcategory", "")),
            ]).casefold()
            if query and query not in haystack:
                full_record = read_json(root / "companies" / f"{safe_stem(str(company.get('company_id', '')))}.json", {})
                full_record = full_record if isinstance(full_record, dict) else {}
                haystack = " ".join([
                    haystack, _display(full_record.get("contacts", [])),
                    _display(full_record.get("emails", [])), _display(full_record.get("phones", [])),
                ]).casefold()
            added = _date_value(str(company.get("first_seen", "")))
            category = str(company.get("organization_category") or "")
            if category.casefold() == "unknown":
                category = ""
            locations = company.get("locations", [])
            locations = locations if isinstance(locations, list) else [locations]
            company_subcategory = str(company.get("subcategory") or "")
            company_status = str(crm_by_id.get(str(company.get("company_id", "")), {}).get("status") or "New")
            has_phones = int(company.get("phone_count", 0)) > 0
            has_website = bool(company.get("has_website", False))
            has_socials = int(company.get("social_count", 0)) > 0
            if query and query not in haystack:
                continue
            if category_filter and category not in category_filter:
                continue
            if company_status not in column_filters["Status"]:
                continue
            if category not in column_filters["Category"]:
                continue
            if company_subcategory not in column_filters["Subcategory"]:
                continue
            presence = {"Website": has_website, "Phone": has_phones, "Socials": has_socials}
            if any(("Has entries" if has_entries else "No entries") not in column_filters[label] for label, has_entries in presence.items()):
                continue
            if location_filter and not set(location_filter).intersection(str(location) for location in locations):
                continue
            if added_after and (added is None or added < added_after):
                continue
            if added_before and (added is None or added > added_before):
                continue
            visible.append(company)

        sort_column = st.session_state.get("company_sort_column", "Company")
        sort_descending = bool(st.session_state.get("company_sort_descending", False))
        sort_secondary_column = st.session_state.get("company_sort_secondary_column")
        sort_secondary_descending = bool(st.session_state.get("company_sort_secondary_descending", False))

        def _company_sort_value(record: dict[str, Any], column: str) -> Any:
            company_id = str(record.get("company_id", ""))
            crm = crm_by_id.get(company_id, {})
            if column == "Status":
                return str(crm.get("status") or "New").casefold()
            if column == "Category":
                category_value = str(record.get("organization_category") or "")
                if category_value.casefold() == "unknown":
                    category_value = ""
                return category_value.casefold()
            if column == "Subcategory":
                return str(record.get("subcategory") or "").casefold()
            if column in ("Email", "Phone"):
                return int(record.get("email_count" if column == "Email" else "phone_count", 0))
            if column == "Website":
                return bool(record.get("has_website", False))
            if column == "Socials":
                return int(record.get("social_count", 0))
            if column == "Created":
                return _date_value(str(record.get("first_seen", ""))) or date.min
            if column == "Notes":
                return int(crm.get("note_count", 0))
            if column == "Next follow-up":
                return _date_value(str(crm.get("next_followup", ""))) or date.min
            if column == "Updated":
                return _date_value(str(record.get("last_seen", ""))) or date.min
            return str(record.get("company_name", "")).casefold()

        if sort_secondary_column:
            visible.sort(key=lambda record: _company_sort_value(record, sort_secondary_column), reverse=sort_secondary_descending)
        visible.sort(key=lambda record: _company_sort_value(record, sort_column), reverse=sort_descending)

        # Pagination controls
        PAGE_SIZE = 30
        total_companies = len(visible)
        total_pages = max(1, math.ceil(total_companies / PAGE_SIZE))
        current_page = st.session_state.get("company_list_page", 1)
        if current_page > total_pages:
            current_page = total_pages
        if current_page < 1:
            current_page = 1
        st.session_state["company_list_page"] = current_page

        count_col, page_col = st.columns([2, 1], vertical_alignment="center")
        with count_col:
            st.caption(f"{total_companies} companies" + (f" · Page {current_page} of {total_pages}" if total_pages > 1 else ""))
        with page_col:
            if total_pages > 1:
                prev_col, label_col, next_col = st.columns([1, 2, 1])
                with prev_col:
                    if st.button("◀ Prev", key="prev-page", disabled=current_page <= 1, use_container_width=True):
                        st.session_state["company_list_page"] = current_page - 1
                        st.rerun()
                with label_col:
                    st.markdown(f"<div style='text-align:center;line-height:2.2rem;font-size:0.9rem;font-weight:600'>{current_page} / {total_pages}</div>", unsafe_allow_html=True)
                with next_col:
                    if st.button("Next ▶", key="next-page", disabled=current_page >= total_pages, use_container_width=True):
                        st.session_state["company_list_page"] = current_page + 1
                        st.rerun()

        start_idx = (current_page - 1) * PAGE_SIZE
        page_items = visible[start_idx : start_idx + PAGE_SIZE]

        if not selected_columns:
            st.info("Select at least one column to display the company list.")
        elif page_items:
            # Browsable list rendering based on selected columns
            col_widths_map = {
                "Company": 2.7, "Status": 1.5, "Category": 1.9, "Subcategory": 1.7,
                "Email": 0.9, "Website": 0.9, "Phone": 0.9, "Socials": 0.9,
                "Created": 1.3, "Notes": 0.8, "Next follow-up": 1.4, "Updated": 1.3,
            }
            cols_to_show = [c for c in list_columns if c in selected_columns]
            column_widths = [col_widths_map.get(col, 1.0) for col in cols_to_show]

            st.markdown("<style>div[class*='st-key-company-list-header']{background:rgba(27,119,128,.12);border-bottom:2px solid #1b7780;border-radius:.45rem;padding:.2rem .35rem;margin-bottom:.25rem}div[class*='st-key-company-list-header'] div[data-testid='stMarkdown']{font-weight:700;color:var(--text-color);text-align:center;font-size:.88rem}div[class*='st-key-company-list-header'] div[data-testid='column']:first-child div[data-testid='stMarkdown']{text-align:left;padding-left:.25rem}div[class*='st-key-company-open-'] button{background:transparent;border:0;color:var(--primary-color);justify-content:flex-start;text-align:left;font-weight:600;padding-left:0}div[class*='st-key-company-open-'] button:hover{text-decoration:underline;color:var(--primary-color)}div[class*='st-key-company-list-row-']{border-bottom:1px solid rgba(128,128,128,.18);padding:.35rem .15rem}div[class*='st-key-company-list-row-']:hover{background:rgba(27,119,128,.04)}div[class*='st-key-company-list-row-'] div[data-testid='stMarkdown'] div.company-cell{text-align:center;font-size:.88rem}div[class*='st-key-company-list-row-'] div[data-testid='column']:first-child div[data-testid='stMarkdown'] div.company-cell{text-align:left}@media(max-width:760px){div[class*='st-key-company-list-header']{display:none}div[class*='st-key-company-list-row-'] div[data-testid='stMarkdown'] div.company-cell{text-align:left}}</style>", unsafe_allow_html=True)

            with st.container(key="company-list-header"):
                header = st.columns(column_widths)
                for column, label in zip(header, cols_to_show):
                    column.markdown(f"**{label}**")

            for company in page_items:
                company_id = str(company.get("company_id", ""))
                crm = crm_by_id.get(company_id, {})
                category_value = str(company.get("organization_category") or "")
                if category_value.casefold() == "unknown":
                    category_value = ""
                category_label = category_value.replace("_", " ").title() if category_value else "—"
                subcategory_label = str(company.get("subcategory") or "").strip().replace("_", " ") or "—"

                values = {
                    "Company": str(company.get("company_name") or "Unnamed company"),
                    "Status": str(crm.get("status") or "New"),
                    "Category": category_label,
                    "Subcategory": subcategory_label,
                    "Email": str(company.get("email_count", 0)) if company.get("email_count") else "—",
                    "Website": "Yes" if company.get("has_website") else "—",
                    "Phone": str(company.get("phone_count", 0)) if company.get("phone_count") else "—",
                    "Socials": str(company.get("social_count", 0)) if company.get("social_count") else "—",
                    "Created": _short_date(company.get("first_seen")) or "—",
                    "Notes": str(crm.get("note_count", 0)),
                    "Next follow-up": _short_date(crm.get("next_followup")) or "—",
                    "Updated": _short_date(company.get("last_seen")) or "—",
                }

                with st.container(key=f"company-list-row-{safe_stem(company_id)}"):
                    row = st.columns(column_widths)
                    for col, label in zip(row, cols_to_show):
                        if label == "Company":
                            with col:
                                if company.get("archived", False):
                                    st.caption("Archived")
                                notes_href = f"?notes_id={quote(company_id, safe='')}"
                                name_btn_col, info_btn_col = st.columns([10, 2], vertical_alignment="center")
                                with name_btn_col:
                                    st.button(
                                        values["Company"],
                                        key=f"company-open-{safe_stem(company_id)}",
                                        use_container_width=True,
                                        on_click=_open_company,
                                        args=(company_id,),
                                    )
                                with info_btn_col:
                                    st.markdown(f"<a href='{notes_href}' title='View notes' style='color:#7b8490;text-decoration:none;font-size:1.05rem'>ⓘ</a>", unsafe_allow_html=True)
                        elif label in ("Email", "Website", "Phone", "Socials") and values[label] != "—":
                            entry_kind = label.casefold()
                            entry_href = f"?entry_id={quote(company_id, safe='')}&amp;entry_kind={entry_kind}"
                            col.markdown(f"<div class='company-cell'><span class='mobile-field-label'>{label}</span><a href='{entry_href}' style='color:var(--primary-color);font-weight:600;text-decoration:none'>{html.escape(values[label])}</a></div>", unsafe_allow_html=True)
                        else:
                            col.markdown(f"<div class='company-cell'><span class='mobile-field-label'>{label}</span>{html.escape(values[label])}</div>", unsafe_allow_html=True)
        else:
            if companies:
                st.info("No companies match the current search and filters.")
            else:
                st.info("No companies yet. Add a source or use the rebuild button after saving extractions.")

