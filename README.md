# FOMC Dataset: Federal Reserve Statements, Minutes and Transcripts (2000–Present)

This project collects and organizes official monetary policy documents published by the Federal Reserve's **Federal Open Market Committee (FOMC)** from the year 2000 to the present.

Instead of manually browsing the Federal Reserve website to download hundreds of separate web pages and documents, you can use this repository to access the complete collection in ready-to-use formats.

---

## What's Included

The dataset covers five types of official FOMC communications:

| Document type | Coverage | Notes |
|---|---|---|
| **Meeting Statements** | 2000 – present | Official policy announcements released right after each FOMC meeting. |
| **Implementation Notes** | December 2015 – present | Instructions on how policy decisions are carried out. The Federal Reserve began issuing them at the December 2015 meeting. |
| **Meeting Minutes** | 2000 – present | Detailed summaries of the discussions held during each meeting, published about three weeks after it. |
| **Meeting Transcripts** | 2000 – 2020 (so far) | Word-for-word records of FOMC meetings. The Federal Reserve releases them after a five-year delay, so a new year appears each year. |
| **Press Conference Transcripts** | April 2011 – present | Full transcripts of the Chair's press conferences following meetings. |

Intermeeting conference calls and notation votes are included alongside regular meetings and are labelled in the `meeting_type` column.

---

## What Makes This Project Different

Most existing FOMC projects only collect one or two types of documents (usually just statements and minutes) and save them in a single file format. This project stands out in three ways:

1. **Complete Coverage (All 5 Document Types)** — In addition to statements and minutes, it includes implementation notes, full meeting transcripts, and press conference transcripts in one place.
2. **Ready-to-Use in 5 Formats** — You don't need to know how to code or convert files. The entire collection is pre-built and ready to download as **Excel**, **CSV**, **SQLite**, **Parquet**, and **plain text**.
3. **Kept Up to Date Automatically** — A monthly pipeline checks the Federal Reserve website for newly released documents and updates every format in the repository. New documents therefore appear up to about a month after the Federal Reserve publishes them.

---

## How to Use

### Option 1: Download the Ready-to-Use Data

You do not need to run any code if you just want the documents. Everything is already saved inside the [`data/`](./data) folder in multiple formats so you can pick whichever works best for you:

- **`data/excel/`** — Excel spreadsheets (`.xlsx`) for easy viewing and filtering. **Excel cells hold at most 32,767 characters, so long documents are cut** (see [Known limitations](#known-limitations)). Use another format when you need the full text.
- **`data/csv/`** — Standard CSV files (`.csv`) compatible with any spreadsheet or data tool. Transcripts are an index here; their text is in `data/text/`.
- **`data/sql/`** — SQLite database files (`.db`) for querying with SQL, with `meeting_date` indexed.
- **`data/parquet/`** — Parquet files (`.parquet`) for fast data analysis.
- **`data/text/`** — Full plain text files (`.txt`) for meeting and press conference transcripts.

The Excel, SQLite and Parquet files hold the same five tables: `statements`, `implementation_notes`, `minutes`, `meeting_transcripts` and `press_conferences`. For example:

```python
import pandas as pd

minutes = pd.read_parquet("data/parquet/minutes.parquet")
minutes = minutes[minutes["shared_with_meeting_date"] == ""]   # see "Known limitations"
```

### Option 2: Run the Scraper Yourself

If you want to run the tool on your own computer:

1. **Install the required packages:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Fetch whatever is new:**
   ```bash
   python fomc_scraper.py
   ```
   The scraper is safe to re-run. Documents already in `data/csv/` are skipped, never duplicated, so this only downloads what the Federal Reserve has published since the last run.

3. **Collect everything from scratch** into a separate folder:
   ```bash
   python fomc_scraper.py --output-dir ./my_data
   ```
   This makes several hundred requests and waits one second between them, so it takes a while.

Useful options:

| Option | What it does |
|---|---|
| `--documents statements,minutes` | Collect only some document types (`statements`, `notes`, `minutes`, `transcripts`, `presconf`). |
| `--start-year 2015 --end-year 2020` | Limit the years collected. |
| `--no-export` | Update the CSVs and text files only; skip Excel, SQLite and Parquet. |
| `--export-only` | Rebuild the exports from the CSVs already on disk. Only tables whose rows changed are rewritten. |
| `--force-export` | Rebuild every export, even unchanged ones. |
| `--validate-only` | Check the CSVs and text files for duplicates, empty or truncated documents, and missing files. |

The scraper exits with a non-zero status if any document failed to download or the data fails validation. Failed documents are retried on the next run.

Transcripts are downloaded only briefly so their text can be extracted. **The original files are not stored** in this repository; `source_url` points to the Federal Reserve's copy.

### Running the tests

```bash
pip install -r requirements-dev.txt
pytest
```

The tests run offline.

---

## Data Dictionary

Dates are ISO `YYYY-MM-DD`; `fetched_at` is a UTC timestamp.

| Column | Tables | Meaning |
|---|---|---|
| `meeting_date` | all | The meeting's date, taken from its last day. See the note below. |
| `meeting_type` | statements, transcripts | `scheduled`, `conference_call`, `unscheduled` or `notation_vote`. |
| `shared_with_meeting_date` | minutes | Blank for most rows. If set, this meeting's minutes were published inside another meeting's document and `text` is that whole document. |
| `source_url` | all | The Federal Reserve page or file the text came from. |
| `source_format` | statements, notes, minutes | The format the text was read from (`html` for every document so far). |
| `n_pages` | transcripts | Number of pages in the original transcript (an integer). |
| `text` | all | The document's plain text. In the CSV index for transcripts this column is replaced by `text_path`, which points into `data/text/`. |
| `fetched_at` | all | When the document was downloaded. |
| `landing_url`, `transcript_type` | `data/csv/transcripts.csv` only | The press conference page, and `meeting` or `press_conference`. |

**About `meeting_date`:** for 2000–2020 it is the date the Federal Reserve lists for the meeting; from 2021 it is read from the web address of the statement (or of the minutes, when there is none). For ordinary meetings these agree. For unscheduled meetings and notation votes it can differ by a day or more from the date printed on the document (for example, the unscheduled call dated `2020-03-02` produced a statement dated March 3, 2020). `source_url` always identifies the exact document.

---

## Known limitations

- **Excel truncates long text.** Excel cannot hold more than 32,767 characters in one cell. That cuts most minutes and almost every transcript. The `full_chars` and `truncated` columns show the real length and which rows were cut. SQLite, Parquet, CSV and `data/text/` always have the full text.
- **Some minutes appear more than once.** Where a conference call's minutes were published inside another meeting's minutes, each meeting gets a row containing the same document, with `shared_with_meeting_date` set. Filter on that column being empty to count each document once.
- **Plain text only.** Text is taken from paragraphs and list items. Page layout, images and any content that exists only inside tables are not preserved. Running page headers are removed from transcripts.
- **The data is as current as the last pipeline run.** The pipeline runs monthly, and the transcripts are released with a five-year delay.

---

## Automatic Monthly Updates

This project includes an automated GitHub workflow that runs **once a month** (and can be started by hand from the Actions tab). It checks the Federal Reserve website for any newly released statements, implementation notes, minutes, or transcripts, validates what it downloaded, updates the affected files in the [`data/`](./data) folder, and saves the changes directly to this repository.

- Documents already collected are skipped, so a run only fetches what is new.
- If anything fails to download, or the new data fails validation, the run is marked as failed. Valid documents that did download are still committed; invalid data is not exported.
- Only the tables that actually changed have their Excel, SQLite and Parquet files rewritten, which keeps the repository small. `data/export_manifest.json` is the bookkeeping file that records this; leave it in place.

---

## Source and License

All documents are published by the Board of Governors of the Federal Reserve System at [federalreserve.gov](https://www.federalreserve.gov/monetarypolicy/fomc.htm). Please refer to the Federal Reserve's website for the terms that apply to its content. This project is independent and is not affiliated with or endorsed by the Federal Reserve.

The scraper code is released under the [MIT License](./LICENSE).
