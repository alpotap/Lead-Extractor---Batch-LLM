# Lead Extractor reference

Detailed information about duplicate handling, an example workflow, and stored data.

## How duplicate companies are merged

1. When an extraction is compared with existing companies, the app scores matches using configured signals such as email, phone, website domain, address, contact name, company name, and city.
2. If the same source is already linked to a company, it is matched back to that company. Otherwise, a score of 70 or more merges the extraction into the best match. A lower score creates a separate company, so a weak match can appear as a duplicate that needs manual review.
3. On a merge, emails, phones, addresses, locations, contacts, social links, opening hours, notes, and source filenames are combined, with duplicates removed. Existing company name and website are kept when already populated. A manually assigned non-unknown category is retained; extraction fills an unknown category and an empty subcategory when it has a matching value.
4. CRM follow-up information—status, rating, dates, and follow-up notes—is stored separately and is not replaced by an extraction merge. Company details you edit manually are saved in the company record; merges add list data and preserve populated names and websites.
5. Rebuild uses the same merge rules on saved extraction files. It does not make new discoveries from original sources; use a source retry to get a fresh extraction first.

### Example: two sources for a fictional UK publisher

The following example is fictional and uses a reserved `.example` domain.

1. You process a contact page for **North Pennine Books Ltd**, based in York. It yields the website `northpenninebooks.example`, the general email `hello@northpenninebooks.example`, and one contact, Alice Morgan. The app creates a company record and links the source file to it.
2. Later, you process a team page for the same publisher. It contains the same website and adds editor Ben Patel, `ben@northpenninebooks.example`, and the company's Leeds office address.
3. The shared website and email produce a strong match. The app merges the new extraction into the existing North Pennine Books Ltd record: Ben and his email and the Leeds address are added, while Alice and the original general email remain. Duplicate values are not added twice.
4. Your CRM status, such as **Contacted**, and your follow-up notes remain as they were. They are not part of the extracted company details.
5. If the second page has no matching website, email, phone, address, or other strong signal and scores below 70, it may become a separate company record. Check the duplicate-decision details in `LeadData/logs/application.log` and correct or merge records manually as needed.

<!-- Screenshot: merged UK example company record -->

## Review queue and rebuild

### Handle a source in Review queue

1. Open **Review queue** to see processing errors and extractions that need attention, such as a missing company name or low confidence.
2. Expand an item to read the error or proposed extraction. Check `LeadData/logs/application.log` for timestamped processing details and `LeadData/logs/llm_raw.jsonl` for the exact prompts and raw Ollama responses.
3. If you correct the source or want another extraction attempt, use the retry action with the selected model. Retrying calls Ollama and may use model resources. The original source is retained under `LeadData/ingest/archived`.
4. Dismiss an item only when you no longer need to act on it. Dismissed review JSON is retained in `LeadData/review/dismissed` and excluded from company rebuilding.

<!-- Screenshot: Review queue with an expanded item -->

### Re-extract or rebuild

1. To ask Ollama to extract an archived source again with the current prompt and selected model, open that company's **Original sources** section and choose the source to retry. This makes model calls; inspect the parsed HTML text first when the source is HTML.
2. To recreate company records from extraction JSON already saved in `LeadData/extracted`, use **Rebuild from saved data** in the setup panel. Rebuilding reads saved JSON only; it does not read incoming or archived source files and does not call Ollama.
3. Rebuilding regenerates company records while CRM files, including follow-up status and notes, are kept separately. It cannot recover information that was never saved to an extraction JSON. Resolve sources still in Review queue by retrying them before rebuilding if you want them included.

<!-- Screenshot: rebuild controls and progress -->


`config/settings.json` controls `data_path`. The default is `~/LeadData`, outside this repository. The application creates this structure automatically:

```text
LeadData/
  ingest/incoming/   # manually saved source files and pasted text
  ingest/archived/   # original source files retained after processing
  ingest/parsed/     # readable TXT prepared from HTML; exact text sent to Ollama
  extracted/         # successful extraction JSON, before company merging
  companies/         # extracted company records
  crm/               # manually maintained CRM records
  review/            # errors and low-confidence extractions
  logs/              # rotating application and raw Ollama logs
  backups/
  categories.json    # user-managed category and subcategory taxonomy
```

Keep `data_path` outside the Git checkout. `.gitignore` excludes common local data folders and logs as a safety net. Never put lead or customer records in this public repository.

`logs/application.log` records ISO timestamps with the local UTC offset, ingestion, the selected model, extraction duration, per-candidate duplicate scores, decisions, and errors. `logs/llm_raw.jsonl` records each Ollama prompt and exact generated response, including source filename and chunk number. The raw log rotates at 10 MB and keeps five backups by default; `raw_llm_log_max_bytes` and `raw_llm_log_backups` in `config/settings.json` control its size. Raw logs contain the prepared source text sent to Ollama and remain in the configured Lead data folder.

Duplicate weights, scan interval (default: 1 minute), minimum extraction confidence, and custom CRM statuses can be configured in `config/settings.json`. The default merge threshold is 70. Processing decisions and errors are recorded in the rotating application log.
