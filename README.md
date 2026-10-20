# Lead Extractor

[![License](https://img.shields.io/github/license/alpotap/Lead-Extractor---Batch-LLM)](LICENSE)
[![Last commit](https://img.shields.io/github/last-commit/alpotap/Lead-Extractor---Batch-LLM)](https://github.com/alpotap/Lead-Extractor---Batch-LLM/commits/main)
[![Repository size](https://img.shields.io/github/repo-size/alpotap/Lead-Extractor---Batch-LLM)](https://github.com/alpotap/Lead-Extractor---Batch-LLM)
[![GitHub stars](https://img.shields.io/github/stars/alpotap/Lead-Extractor---Batch-LLM)](https://github.com/alpotap/Lead-Extractor---Batch-LLM/stargazers)
[![Open issues](https://img.shields.io/github/issues/alpotap/Lead-Extractor---Batch-LLM)](https://github.com/alpotap/Lead-Extractor---Batch-LLM/issues)
[![Top language](https://img.shields.io/github/languages/top/alpotap/Lead-Extractor---Batch-LLM)](https://github.com/alpotap/Lead-Extractor---Batch-LLM)

A local-first lead manager for any businesses. It accepts manually gathered text, HTML, and Markdown, extracts lead data with Ollama, and stores business records in a convenient way. The tool detects duplicates and merges them if new data is found.

# How does it work?
1. FInd the businesses that you need and enter their website. (for example if you find them on google maps).
2. Go to the "Our team" or "Contact us" page.
3. Save the page in html format or press Ctrl+a to select everything on a page and press Ctrl+c to copy all the contents.
4. Place the html file in the directory that you set up in the tool (Default is c:\users\<your user name>\LeadData\ingest\incoming (you can change it to anything you like in the app). If you used copy-paste - use a dedicated feild in the tool.
5. The tool will process the data automatically, using a default Ollama model you have. (You can change the model in the tool, using a drop down)
6. Use the data - it will include all the contacts on the page, descriptions, addresses and socials. You can add statuses and notes to each record.

#### Pro tips: 
* you can view pagfe source and copy paste it instead of saving html.
* it is faster to save a bunch of html files and then copy them to your destination folder. This way you can separate tasks: open pages, save them, copy them. Benchamark - i got leads saved on ~50 pages in about 5 minutes.
* HTML as a source is better than getting the page content with Ctrl+a. The HTML will include lings to socials
* If the business has a separate page for business hours, save it too. The tool merges all data under the same business so you can have multiple pages to enrich a single entry.   

## Start the app

1. Clone this repository on Windows.
2. Double-click **Start Lead Desk.bat**.
3. Keep the launcher window open while using the app. It finds Python 3.11 or newer (and can install it for the current user if needed), creates a private `.venv`, installs the packages in `requirements.txt`, starts Ollama when available, and opens the app in your browser. No administrator access or multi-step setup is required.
4. In the app's setup panel, select an installed Ollama model. The model list includes cloud models exposed by `ollama list`; if Ollama's command-line tool is not on `PATH`, the app checks its local `/api/tags` endpoint instead. Model downloads are not automatic because they can be large. A local model can be used without a hosted API or paid account.
5. URL capture uses an installed Microsoft Edge or Google Chrome browser through Playwright; it does not download a separate browser.

If Ollama is unavailable, you can still browse and edit existing company records, but the app cannot extract new leads until Ollama is running.

<!-- Screenshot: app setup and model selection -->

## How to use Lead Extractor

### 1. Add source material

1. To submit a file, copy a `.html`, `.txt`, or `.md` file into `LeadData/ingest/incoming`. The default data folder is `C:\Users\<your-name>\LeadData`.
2. Alternatively, open **Add a source**, paste text copied from a company or contact page, and save it. The saved text is picked up by the background scanner.
3. To capture a live page, open **Add a source** → **Fetch from URL**, paste an `http` or `https` address, and select **Fetch page and save HTML**. The app opens the page in a headless browser, waits 10 seconds after the DOM is ready, and saves the rendered HTML under `LeadData/ingest/incoming`.
4. The scanner checks the incoming folder every 1 minute. Keep the app running while you wait. It records the selected model and source filename in the processing logs.

<!-- Screenshot: Companies and Add a source tabs -->

### 2. Inspect prepared HTML text

1. For an HTML source, the app extracts readable text before sending anything to Ollama. It omits scripts, styles, and embedded non-text content; it keeps visible text, image alt text, and link targets.
2. Open `LeadData/ingest/parsed`. The file named `<original filename>.parsed.txt` contains the exact text sent to Ollama. For example, `Bristol-House-contact.html.parsed.txt` is the prepared text from `Bristol-House-contact.html`.
3. Compare this text with the original HTML if you suspect that useful information was missed. If the preparation omitted important content, keep the original and adjust the HTML preparation rules before relying on that kind of source.
4. The original HTML is preserved in `LeadData/ingest/archived` after processing. TXT and Markdown inputs are read as text without this HTML preparation step.

If preparation produces no readable text, the source is sent to **Review queue** as an error; the original HTML is still preserved. A parsed text file is kept separately and is not scanned again as a new incoming source.

<!-- Screenshot: original HTML beside its parsed TXT file -->

### 3. Find and review extracted companies

1. Open **Companies** to browse the company list. Use the search field to find a company, contact, email, or phone.
2. Click a company name to open its details.
3. Review and edit company fields, contacts, social accounts, opening hours, and notes. Contacts are individual rows with a name, role, emails, and phones. Social accounts pair a platform name with a profile URL.
4. Click **Save company changes** after editing company information.
5. Use the follow-up controls to update status, rating, last-contacted date, and next follow-up date. Add follow-up notes as separate entries; edit or delete an individual note with its controls.
6. Use **Copy as JSON** when you explicitly want a copy of the current company record in your clipboard.
7. Use **Archive** on a company record to hide it from the default company list. Archived companies remain available for duplicate matching when new sources are processed. Expand **Advanced search** and turn on **Show archived** to find or restore one.

<!-- Screenshot: company details and follow-up controls -->

### 4. Handle a source in Review queue

1. Open **Review queue** to see processing errors and extractions that need attention, such as a missing company name or low confidence.
2. Expand an item to read the error or proposed extraction. Check `LeadData/logs/application.log` for timestamped processing details and `LeadData/logs/llm_raw.jsonl` for the exact prompts and raw Ollama responses.
3. If you correct the source or want another extraction attempt, use the retry action with the selected model. Retrying calls Ollama and may use model resources. The original source is retained under `LeadData/ingest/archived`.
4. Dismiss an item only when you no longer need to act on it. Dismissed review JSON is retained in `LeadData/review/dismissed` and excluded from company rebuilding.

<!-- Screenshot: Review queue with an expanded item -->

### 5. Re-extract or rebuild

1. To ask Ollama to extract an archived source again with the current prompt and selected model, open that company's **Original sources** section and choose the source to retry. This makes model calls; inspect the parsed HTML text first when the source is HTML.
2. To recreate company records from extraction JSON already saved in `LeadData/extracted`, use **Rebuild from saved data** in the setup panel. Rebuilding reads saved JSON only; it does not read incoming or archived source files and does not call Ollama.
3. Rebuilding regenerates company records while CRM files, including follow-up status and notes, are kept separately. It cannot recover information that was never saved to an extraction JSON. Resolve sources still in Review queue by retrying them before rebuilding if you want them included.

<!-- Screenshot: rebuild controls and progress -->

## How duplicate companies are merged

1. When an extraction is compared with existing companies, the app scores matches using configured signals such as email, phone, website domain, address, contact name, company name, and city.
2. If the same source is already linked to a company, it is matched back to that company. Otherwise, a score of 70 or more merges the extraction into the best match. A lower score creates a separate company, so a weak match can appear as a duplicate that needs manual review.
3. On a merge, emails, phones, addresses, locations, contacts, social links, opening hours, notes, and source filenames are combined, with duplicates removed. Existing company name and website are kept when already populated. A manually assigned non-unknown category is retained; extraction fills an unknown category and an empty subcategory when it has a matching value.
4. CRM follow-up information—status, rating, dates, and follow-up notes—is stored separately and is not replaced by an extraction merge. Company details you edit manually are saved in the company record; merges add list data and preserve populated names and websites.
5. Rebuild uses the same merge rules on saved extraction files. It does not make new discoveries from original sources; use a source retry to get a fresh extraction first.

## Add and use categories

1. Open **Add a source** and expand **Manage categories and subcategories**.
2. To create a new main category, enter its name. To add subcategories to an existing category, choose it from **Add subcategories to**. You can paste multiple subcategories, one per line.
3. Select **Save categories**. The category tree is saved in `LeadData/categories.json` and is available when editing company records and during future Ollama extractions.
4. To remove a category, choose it under **Delete a category or subcategory** and select **Delete category**. This also removes its subcategories from future choices. To remove only one subcategory, select its parent and child, then select **Delete subcategory**.
5. Removing a category or subcategory only changes the taxonomy offered for new work. Existing company records keep their assigned category and subcategory; the old value remains available on that company’s edit form. Re-adding a built-in category makes it available again.
6. On a company record, select a main category and then one of its subcategories. The main company list displays both values together and lets you sort by them. Built-in category names such as `academic_publisher` are displayed as normal words.

<!-- Screenshot: category and subcategory manager -->

### Example: two sources for a fictional UK publisher

The following example is fictional and uses a reserved `.example` domain.

1. You process a contact page for **North Pennine Books Ltd**, based in York. It yields the website `northpenninebooks.example`, the general email `hello@northpenninebooks.example`, and one contact, Alice Morgan. The app creates a company record and links the source file to it.
2. Later, you process a team page for the same publisher. It contains the same website and adds editor Ben Patel, `ben@northpenninebooks.example`, and the company's Leeds office address.
3. The shared website and email produce a strong match. The app merges the new extraction into the existing North Pennine Books Ltd record: Ben and his email and the Leeds address are added, while Alice and the original general email remain. Duplicate values are not added twice.
4. Your CRM status, such as **Contacted**, and your follow-up notes remain as they were. They are not part of the extracted company details.
5. If the second page has no matching website, email, phone, address, or other strong signal and scores below 70, it may become a separate company record. Check the duplicate-decision details in `LeadData/logs/application.log` and correct or merge records manually as needed.

<!-- Screenshot: merged UK example company record -->

## Data, logs, and privacy

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
