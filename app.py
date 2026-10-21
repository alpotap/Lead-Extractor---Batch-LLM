"""Local-first Streamlit lead manager."""

from __future__ import annotations

import math
import json
import html
import shutil
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import streamlit as st

from models.lead import (
    DEFAULT_STATUSES,
    ORGANIZATION_CATEGORIES,
    normalize_contacts,
    normalize_opening_hours,
    normalize_social_links,
)
from services.ollama import is_local_url, is_ollama_cloud_url, list_models
from services.pipeline import configure_logging, process_incoming, start_background_scanner
from services.storage import (
    data_root, load_category_taxonomy, load_deleted_default_categories, load_companies, load_settings, read_json,
    rebuild_companies, safe_stem, save_category_taxonomy, save_ollama_api_key, save_settings, write_json,
)
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
    "Main navigation", ["Companies", "Add a source", "Categories", "Review queue"],
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


def _open_company(company_id: str) -> None:
    st.session_state["selected_company_id"] = company_id


def _sort_companies_by(column: str) -> None:
    current = st.session_state.get("company_sort_column", "Company")
    if current == column:
        st.session_state["company_sort_descending"] = not st.session_state.get("company_sort_descending", False)
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

if active_section == "Add a source":
    st.subheader("Add a source")
    url_tab, source_tab = st.tabs(["Fetch from URL", "Paste text"])
    with url_tab:
        st.write("Load a web page in a browser, wait 10 seconds for dynamic content, and save the rendered HTML for processing.")
        page_url = st.text_input("Company page URL", placeholder="https://example.co.uk/contact")
        if st.button("Fetch page and save HTML", type="primary", disabled=not page_url.strip()):
            try:
                with st.spinner("Loading page and waiting 10 seconds for content…"):
                    html = capture_rendered_html(page_url, settle_seconds=10)
                host = safe_stem(urlparse(page_url).hostname or "website")
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                target = root / "ingest" / "incoming" / f"web_{host}_{stamp}.html"
                target.write_text(html, encoding="utf-8")
                configure_logging(root).info("Captured rendered URL source url=%s file=%s html_chars=%d", page_url, target.name, len(html))
                st.success(f"Saved rendered HTML as {target.name}. It will be processed on the next scan.")
            except Exception as exc:
                st.error(f"Could not fetch the page: {exc}")
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
            date_value = _date_value(created_at) or datetime.fromtimestamp(review_file.stat().st_mtime).date()
            age_days = (date.today() - date_value).days
            label = f"{source_name} · {review.get('reason', 'needs review')} · {date_value.isoformat()}"
            with st.expander(label):
                if age_days > 30:
                    st.warning(f"Older item · {age_days} days old. Check whether it still needs attention.")
                st.write(f"**Flagged:** {date_value.isoformat()}")
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
    companies = load_companies(root)
    company_by_id = {str(record.get("company_id")): record for record in companies}
    selected_id = st.session_state.get("selected_company_id")

    if selected_id and selected_id in company_by_id:
        company = company_by_id[selected_id]
        company_id = str(company.get("company_id", ""))
        header_back, header_name, header_dates, header_copy, header_archive = st.columns([1.2, 2.8, 2.2, 1.4, 1.1], vertical_alignment="center")
        with header_back:
            st.button(
                "← Companies", key=f"company-back-{safe_stem(company_id)}",
                on_click=lambda: st.session_state.pop("selected_company_id", None), use_container_width=True,
            )
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
        show_advanced_search = bool(st.session_state.get("show_advanced_search", False))
        search_col, advanced_col = st.columns([2.8, 1.7], vertical_alignment="bottom")
        with search_col:
            search = st.text_input("Search company, contact, email, or phone")
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

        visible: list[dict[str, Any]] = []
        query = search.casefold().strip()
        for company in companies:
            if company.get("archived", False) and not show_archived:
                continue
            haystack = " ".join([
                str(company.get("company_name", "")), str(company.get("organization_category", "")),
                str(company.get("subcategory", "")), _display(company.get("contacts", [])),
                _display(company.get("emails", [])), _display(company.get("phones", [])),
            ]).casefold()
            added = _date_value(str(company.get("first_seen", "")))
            category = str(company.get("organization_category") or "")
            if category.casefold() == "unknown":
                category = ""
            locations = company.get("locations", [])
            locations = locations if isinstance(locations, list) else [locations]
            if query and query not in haystack:
                continue
            if category_filter and category not in category_filter:
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
        def _company_sort_value(record: dict[str, Any]) -> Any:
            company_id = str(record.get("company_id", ""))
            if sort_column == "Status":
                return str(_crm(company_id).get("status") or "New").casefold()
            if sort_column == "Category":
                category_value = str(record.get("organization_category") or "")
                if category_value.casefold() == "unknown":
                    category_value = ""
                return f"{category_value} {record.get('subcategory') or ''}".casefold()
            if sort_column == "Notes":
                return len(_followup_notes(_crm(company_id).get("notes", [])))
            if sort_column == "Next follow-up":
                return _date_value(str(_crm(company_id).get("next_followup", ""))) or date.min
            if sort_column == "Updated":
                return _date_value(str(record.get("last_seen", ""))) or date.min
            return str(record.get("company_name", "")).casefold()

        visible.sort(key=_company_sort_value, reverse=sort_descending)
        st.caption(f"{len(visible)} companies")
        if visible:
            st.markdown("<style>div[class*='st-key-company-list-header']{background:rgba(27,119,128,.12);border-bottom:2px solid #1b7780;border-radius:.45rem;padding:.15rem .35rem;margin-bottom:.25rem}div[class*='st-key-sort-companies-'] button{background:transparent;border:0;color:var(--text-color);font-weight:700;justify-content:center;padding:.2rem 0;text-align:center;min-height:1.6rem}div[class*='st-key-sort-companies-'] button:hover{background:transparent;text-decoration:underline;color:var(--text-color)}div[class*='st-key-company-open-'] button{background:transparent;border:0;color:var(--primary-color);justify-content:center;text-align:center}div[class*='st-key-company-open-'] button:hover{text-decoration:underline;color:var(--primary-color)}div[class*='st-key-company-list-row-'] div[data-testid='stMarkdown'] div.company-cell{text-align:center} @media(max-width:760px){div[class*='st-key-company-list-header']{display:none}div[class*='st-key-company-open-'] button{justify-content:flex-start;text-align:left;padding-left:0}div[class*='st-key-company-list-row-'] div[data-testid='stMarkdown'] div.company-cell{text-align:left}}</style>", unsafe_allow_html=True)
            column_widths = [2.7, 1.6, 2.2, 0.8, 1.6, 1.4]
            with st.container(key="company-list-header"):
                header = st.columns(column_widths)
                for column, label in zip(header, ("Company", "Status", "Category", "Notes", "Next follow-up", "Updated")):
                    marker = (" ↓" if sort_descending else " ↑") if sort_column == label else ""
                    column.button(
                        f"{label}{marker}", key=f"sort-companies-{label}", use_container_width=True,
                        on_click=_sort_companies_by, args=(label,),
                    )
            for company in visible:
                company_id = str(company.get("company_id", ""))
                crm = _crm(company_id)
                note_count = len(_followup_notes(crm.get("notes", [])))
                next_followup = _short_date(crm.get("next_followup")) or "—"
                category_value = str(company.get("organization_category") or "")
                if category_value.casefold() == "unknown":
                    category_value = ""
                category_label = category_value.replace("_", " ").title() if category_value else "—"
                subcategory_label = str(company.get("subcategory") or "").strip().replace("_", " ")
                category_display = f"{category_label} · {subcategory_label}" if subcategory_label else category_label
                with st.container(key=f"company-list-row-{safe_stem(company_id)}"):
                    row = st.columns(column_widths)
                    with row[0]:
                        if company.get("archived", False):
                            st.caption("Archived")
                        st.button(
                            str(company.get("company_name") or "Unnamed company"),
                            key=f"company-open-{safe_stem(company_id)}", use_container_width=True,
                            on_click=_open_company, args=(company_id,),
                        )
                    row[1].markdown(f"<div class='company-cell'><span class='mobile-field-label'>Status</span>{html.escape(str(crm.get('status') or 'New'))}</div>", unsafe_allow_html=True)
                    row[2].markdown(f"<div class='company-cell'><span class='mobile-field-label'>Category</span>{html.escape(category_display)}</div>", unsafe_allow_html=True)
                    row[3].markdown(f"<div class='company-cell'><span class='mobile-field-label'>Notes</span>{note_count}</div>", unsafe_allow_html=True)
                    row[4].markdown(f"<div class='company-cell'><span class='mobile-field-label'>Next follow-up</span>{html.escape(next_followup)}</div>", unsafe_allow_html=True)
                    row[5].markdown(f"<div class='company-cell'><span class='mobile-field-label'>Updated</span>{html.escape(_short_date(company.get('last_seen')) or '—')}</div>", unsafe_allow_html=True)
        else:
            st.info("No companies yet. Add a source or use the rebuild button after saving extractions.")
