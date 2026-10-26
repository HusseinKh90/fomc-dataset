#!/usr/bin/env python3
"""Scrape FOMC post-meeting statements, implementation notes and minutes (2000-present) into CSVs.

Requires requests, beautifulsoup4 and lxml; pdfplumber is only imported to read PDF-only documents
(the transcripts). PDFs are parsed in memory and never written to disk.
"""

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

BASE = "https://www.federalreserve.gov"
CALENDAR_URL = BASE + "/monetarypolicy/fomccalendars.htm"
HISTORICAL_URL = BASE + "/monetarypolicy/fomchistorical{year}.htm"

# The calendar page covers 2021 onwards; anything older lives in the per-year archive.
CALENDAR_FIRST_YEAR = 2021

USER_AGENT = (
    "fomc-scraper/1.0 (+https://github.com/HusseinKh90/fomc-dataset; research use; "
    "contact via repository issues) python-requests"
)

# Longest field is a ~95k-character set of minutes; the csv module's default cap is 131072.
csv.field_size_limit(2 ** 31 - 1)

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}
MONTH_RE = "|".join(MONTHS)

# "February 1-2", "January 29/30", "January 31-February 1", "March 2"
DATE_RE = re.compile(
    r"(?P<m1>%s)\.?\s+(?P<d1>\d{1,2})(?:\s*[-–/]\s*(?:(?P<m2>%s)\.?\s+)?(?P<d2>\d{1,2}))?" % (MONTH_RE, MONTH_RE),
    re.IGNORECASE,
)
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
URL_DATE_RE = re.compile(r"(19|20)(\d{2})(\d{2})(\d{2})")

# Boilerplate that shows up in the page body on the older templates.
JUNK_PREFIXES = (
    "Board of Governors of the Federal Reserve System",
    "Last Update",
    "Last update",
    "Return to top",
    "Home |",
    "Skip to",
    "20th Street and Constitution",
)

STATEMENT_COLUMNS = ["meeting_date", "meeting_type", "source_url", "source_format", "text", "fetched_at"]
NOTE_COLUMNS = ["meeting_date", "source_url", "source_format", "text", "fetched_at"]
MINUTES_COLUMNS = ["meeting_date", "shared_with_meeting_date", "source_url", "source_format", "text", "fetched_at"]
TRANSCRIPT_COLUMNS = ["meeting_date", "transcript_type", "meeting_type", "source_url", "landing_url",
                      "text_path", "n_pages", "fetched_at"]

DOCUMENT_TYPES = ("statements", "notes", "minutes", "transcripts", "presconf")

# The first Implementation Note was issued with the December 2015 statement (liftoff); no earlier
# meeting has one, so there is no point looking for them.
FIRST_NOTE_DATE = "2015-12-16"

# Smallest documents seen so far are 568 (statement), 1893 (note) and 20314 (minutes) characters
# and 1500 characters per transcript page; anything under these floors is a failed extraction.
MIN_TEXT_CHARS = {"statements": 300, "implementation_notes": 800, "minutes": 5000}
MIN_CHARS_PER_PAGE = 500

# The Fed's pages are served through Cloudflare, which swaps any e-mail address for this literal
# placeholder and keeps the real one hex-encoded in a data-cfemail attribute.
EMAIL_PLACEHOLDER_RE = re.compile(r"\[email\s+protected\]")

# Excel refuses more than this in one cell and openpyxl drops the overflow silently, so the
# export cuts at the limit itself and flags which rows lost text. SQL and Parquet keep it whole.
EXCEL_CELL_LIMIT = 32767

# Columns stored as integers in the exports; everything else is text (dates are ISO yyyy-mm-dd).
INTEGER_COLUMNS = {"n_pages"}

# Bump when the layout of the exports changes so every corpus is rebuilt once.
EXPORT_VERSION = 2
MANIFEST_NAME = "export_manifest.json"


@dataclass
class Meeting:
    date: str
    meeting_type: str
    statement_html: Optional[str] = None
    statement_pdf: Optional[str] = None
    note_url: Optional[str] = None
    minutes_html: Optional[str] = None
    minutes_pdf: Optional[str] = None
    minutes_shared_with: Optional[str] = None
    transcript_url: Optional[str] = None
    presconf_url: Optional[str] = None


class Fetcher:
    def __init__(self, delay=1.0):
        self.delay = delay
        self.last_request = 0.0
        self.session = requests.Session()
        retry = Retry(
            total=5,
            backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET",),
            raise_on_status=False,
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/pdf,*/*"})

    def get(self, url):
        wait = self.delay - (time.monotonic() - self.last_request)
        if wait > 0:
            time.sleep(wait)
        self.last_request = time.monotonic()
        response = self.session.get(url, timeout=60)
        response.raise_for_status()
        return response

    def soup(self, url):
        response = self.get(url)
        response.encoding = "utf-8"
        return BeautifulSoup(response.text, "lxml")


def norm(text):
    return re.sub(r"\s+", " ", text or "").strip()


def meeting_type_from(text):
    lowered = text.lower()
    if "notation vote" in lowered:
        return "notation_vote"
    if "conference call" in lowered:
        return "conference_call"
    if "unscheduled" in lowered:
        return "unscheduled"
    return "scheduled"


def date_from_text(text, default_year=None):
    """Last day of a meeting written as e.g. 'January 31-February 1 Meeting - 2006'."""
    matches = list(DATE_RE.finditer(text))
    if not matches:
        return None
    match = matches[-1]
    month = MONTHS[(match.group("m2") or match.group("m1")).lower()]
    day = int(match.group("d2") or match.group("d1"))
    year_match = YEAR_RE.search(text[match.end():]) or YEAR_RE.search(text)
    year = int(year_match.group(0)) if year_match else default_year
    if year is None:
        return None
    try:
        return "%04d-%02d-%02d" % (year, month, day)
    except ValueError:
        return None


def date_from_url(url):
    match = URL_DATE_RE.search(url or "")
    if not match:
        return None
    return "%s%s-%s-%s" % match.groups()


def classify_links(container):
    """Pull the HTML and PDF href out of a labelled block, ignoring links we never want."""
    html_url = pdf_url = None
    for link in container.find_all("a"):
        href = link.get("href")
        if not href:
            continue
        text = norm(link.get_text()).lower()
        if "longer-run goals" in text or "implementation note" in text:
            continue
        if href.lower().endswith(".pdf") or text == "pdf":
            pdf_url = pdf_url or urljoin(BASE, href)
        else:
            html_url = html_url or urljoin(BASE, href)
    return html_url, pdf_url


def labelled_block(scope, label, tags=("div",)):
    """Smallest element in `scope` whose text starts with `label` (e.g. 'Statement:')."""
    best = None
    for tag in scope.find_all(tags):
        text = norm(tag.get_text())
        if not text.lower().startswith(label.lower()):
            continue
        if "longer-run goals" in text.lower()[:80]:
            continue
        if best is None or len(text) < len(norm(best.get_text())):
            best = tag
    return best


def find_note_link(scope):
    for link in scope.find_all("a"):
        if norm(link.get_text()).lower().startswith("implementation note") and link.get("href"):
            return urljoin(BASE, link["href"])
    return None


def transcript_link(scope):
    """Meeting transcript PDF for one meeting panel.

    The .pdf test is what separates it from the "Transcripts and other historical materials"
    navigation link, which also starts with "Transcript" but points at an .htm page.
    """
    for link in scope.find_all("a"):
        href = link.get("href") or ""
        if norm(link.get_text()).lower().startswith("transcript") and href.lower().endswith(".pdf"):
            return urljoin(BASE, href)
    return None


def presconf_link(scope):
    for link in scope.find_all("a"):
        href = link.get("href")
        if href and norm(link.get_text()).lower().startswith("press conference"):
            return urljoin(BASE, href)
    return None


def presconf_pdf(soup):
    """Transcript PDF on a press conference landing page.

    The link under the video is injected by the Brightcove player at runtime and is absent from
    the served HTML, but "Related Information" carries a static link to the same file. The bare
    "PDF" links there are the projections table and the minutes.
    """
    for link in soup.find_all("a"):
        href = link.get("href")
        if href and norm(link.get_text()).lower().startswith("press conference transcript"):
            return urljoin(BASE, href)
    return None


def parse_calendar(soup, years):
    meetings = []
    for panel in soup.select("div.panel.panel-default"):
        heading = panel.find(class_="panel-heading")
        year_match = YEAR_RE.search(heading.get_text()) if heading else None
        if not year_match:
            continue
        year = int(year_match.group(0))
        if year not in years:
            continue

        for row in panel.select("div.fomc-meeting"):
            statement_block = labelled_block(row, "Statement:")
            minutes_block = labelled_block(row, "Minutes:")
            if statement_block is None and minutes_block is None:
                continue  # future or notation-vote row with nothing usable

            statement_html = statement_pdf = note_url = None
            if statement_block is not None:
                statement_html, statement_pdf = classify_links(statement_block)
                note_url = find_note_link(statement_block)

            minutes_html = minutes_pdf = None
            if minutes_block is not None:
                minutes_html, minutes_pdf = classify_links(minutes_block)

            date = (
                date_from_url(statement_html)
                or date_from_url(statement_pdf)
                or date_from_url(minutes_html)
                or date_from_url(minutes_pdf)
                or date_from_text(norm(row.get_text(" ")), default_year=year)
            )
            if not date:
                continue

            meetings.append(Meeting(
                date=date,
                meeting_type=meeting_type_from(norm(row.get_text(" "))[:200]),
                statement_html=statement_html,
                statement_pdf=statement_pdf,
                note_url=note_url,
                minutes_html=minutes_html,
                minutes_pdf=minutes_pdf,
                # No meeting transcript here: those stay embargoed for five years.
                presconf_url=presconf_link(row),
            ))
    return meetings


def parse_historical_year(soup, year):
    meetings = []
    for panel in soup.select("div.panel.panel-default"):
        heading = panel.find(class_="panel-heading")
        if heading is None:
            continue
        heading_text = norm(heading.get_text())
        date = date_from_text(heading_text, default_year=year)
        if not date:
            continue  # not a meeting panel (transcripts, materials, ...)

        statement_html = statement_pdf = None
        block = labelled_block(panel, "Statement", tags=("p",))
        if block is not None:
            statement_html, statement_pdf = classify_links(block)
        else:
            # A few pages put the link outside a paragraph.
            for link in panel.find_all("a"):
                text = norm(link.get_text()).lower()
                if text == "statement" and link.get("href"):
                    statement_html = urljoin(BASE, link["href"])
                    break

        # Minutes folded into another meeting's document. The marker is usually inside the
        # "Minutes:" paragraph, but on some pages (2008) it sits loose in the column div.
        minutes_html = minutes_pdf = shared_with = None
        for em in panel.find_all("em"):
            if norm(em.get_text()).lower().startswith("see end of minutes of"):
                shared_with = date_from_text(norm(em.get_text()), default_year=year)
                break
        if shared_with is None:
            block = labelled_block(panel, "Minutes", tags=("p",))
            if block is not None:
                minutes_html, minutes_pdf = classify_links(block)

        meetings.append(Meeting(
            date=date,
            meeting_type=meeting_type_from(heading_text),
            statement_html=statement_html,
            statement_pdf=statement_pdf,
            minutes_html=minutes_html,
            minutes_pdf=minutes_pdf,
            minutes_shared_with=shared_with,
            transcript_url=transcript_link(panel),
            presconf_url=presconf_link(panel),
        ))
    return meetings


def main_content(soup):
    for selector in ("#article", "#content", "#leftText", "div.col-xs-12.col-sm-8.col-md-8"):
        node = soup.select_one(selector)
        if node is not None:
            return node
    return soup.body or soup


def decode_cfemail(encoded):
    """Cloudflare hides an address as hex: one key byte, then every character XOR-ed with it."""
    key = int(encoded[:2], 16)
    return "".join(chr(int(encoded[i:i + 2], 16) ^ key) for i in range(2, len(encoded), 2))


def reveal_emails(content):
    for tag in content.find_all(attrs={"data-cfemail": True}):
        try:
            tag.replace_with(decode_cfemail(tag["data-cfemail"]))
        except ValueError:
            pass  # not valid hex; the placeholder stays and the run reports it


def html_text(soup):
    content = main_content(soup)
    for tag in content.find_all(["script", "style", "nav", "header", "footer", "form"]):
        tag.decompose()
    reveal_emails(content)
    for link in content.find_all("a"):
        if norm(link.get_text()).lower() == "return to text":  # footnote back-link, not content
            link.decompose()

    paragraphs = []
    # Implementation notes put the domestic policy directive in a <ul> inside a
    # <blockquote>, so <p>-only extraction silently dropped every Desk instruction
    # (caps, redemptions, rollovers). Walk <li> too, but only leaf items: a list
    # item that itself contains an <li> or <p> would re-emit its children's text.
    for node in content.find_all(["p", "li"]):
        if node.name == "li" and (node.find("li") or node.find("p")):
            continue
        text = norm(node.get_text(" "))
        if len(text) < 3 or text.startswith(JUNK_PREFIXES):
            continue
        # Old templates end with a breadcrumb paragraph that is nothing but a link.
        # The same test drops the Home / News & Events / Press Releases crumb list.
        if len(text) < 120 and text == norm(" ".join(a.get_text(" ") for a in node.find_all("a"))):
            continue
        paragraphs.append("- " + text if node.name == "li" else text)

    body = "\n\n".join(paragraphs)
    if len(body) < 200:
        # The 2000-2006 templates are <font>/table soup with few real <p> tags.
        lines = [norm(line) for line in content.get_text("\n").splitlines()]
        body = "\n".join(l for l in lines if len(l) > 2 and not l.startswith(JUNK_PREFIXES))
    return body


def pdf_text(content):
    """Return (text, page_count) with running headers and page-break markers removed."""
    import pdfplumber

    with pdfplumber.open(io.BytesIO(content)) as pdf:
        pages = [page.extract_text() or "" for page in pdf.pages]

    # Running headers/footers repeat on most pages, but the page number makes each one differ.
    # Transcripts head every page with e.g. "January 27-28, 2015 3 of 361", where the number
    # sits mid-line, so every digit run has to go before the lines can be compared.
    def key(line):
        return re.sub(r"\d+", "#", norm(line))

    edges = Counter()
    for page in pages:
        lines = [l for l in page.splitlines() if norm(l)]
        if lines:
            edges[key(lines[0])] += 1
            edges[key(lines[-1])] += 1
    # Headers often alternate between recto and verso, so each variant covers only half
    # the pages; a quarter is a safe floor for "this is furniture, not content".
    threshold = max(3, len(pages) // 4)
    boilerplate = {k for k, count in edges.items() if k and len(pages) > 2 and count >= threshold}

    kept = []
    for page in pages:
        for line in page.splitlines():
            text = norm(line)
            if not text or text == "(more)" or key(line) in boilerplate:
                continue
            kept.append(text)
    return "\n".join(kept), len(pages)


def fetch_document(fetcher, html_url, pdf_url):
    """Return (text, url, format, soup). soup is None for PDFs."""
    if html_url:
        soup = fetcher.soup(html_url)
        return html_text(soup), html_url, "html", soup
    if pdf_url:
        text, _ = pdf_text(fetcher.get(pdf_url).content)
        return text, pdf_url, "pdf", None
    return None, None, None, None


class CsvOutput:
    def __init__(self, path, columns):
        is_new = not os.path.exists(path) or os.path.getsize(path) == 0
        if not is_new:
            # Appending rows under a different header would silently shift every column.
            with open(path, newline="", encoding="utf-8") as handle:
                header = next(csv.reader(handle), [])
            if header != columns:
                raise ValueError("%s has columns %s but this version writes %s; migrate the file first"
                                 % (path, header, columns))
        self.handle = open(path, "a", newline="", encoding="utf-8")
        self.writer = csv.writer(self.handle, lineterminator="\n")
        if is_new:
            self.writer.writerow(columns)
            self.handle.flush()

    def write(self, row):
        self.writer.writerow(row)
        self.handle.flush()

    def close(self):
        self.handle.close()


def read_csv_rows(path):
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.reader(handle))


def write_csv_rows(path, rows):
    """Replace a CSV in one step so an interrupted run cannot leave half a file behind."""
    temp = path + ".tmp"
    with open(temp, "w", newline="", encoding="utf-8") as handle:
        csv.writer(handle, lineterminator="\n").writerows(rows)
    os.replace(temp, path)


def drop_placeholder_rows(path):
    """Remove stored documents whose text still holds Cloudflare's e-mail placeholder.

    The address was never decoded when they were collected, so the normal run downloads them again.
    Must run before the file is opened for appending. Returns the removed rows, which the caller
    puts back with restore_rows() if the new download fails.
    """
    if not os.path.exists(path):
        return []
    rows = read_csv_rows(path)
    if not rows or "text" not in rows[0]:
        return []
    column = rows[0].index("text")
    stale = [r for r in rows[1:] if EMAIL_PLACEHOLDER_RE.search(r[column])]
    if stale:
        write_csv_rows(path, [rows[0]] + [r for r in rows[1:] if r not in stale])
    return stale


def restore_rows(path, rows):
    """Put back removed rows that were not replaced, so a failed download never loses a document.

    meeting_date is the first column of the statements, notes and minutes files.
    """
    have = existing_dates(path)
    missing = [r for r in rows if r[0] not in have]
    if missing:
        with open(path, "a", newline="", encoding="utf-8") as handle:
            csv.writer(handle, lineterminator="\n").writerows(missing)


def count_placeholder_rows(path):
    if not os.path.exists(path):
        return 0
    rows = read_csv_rows(path)
    if not rows or "text" not in rows[0]:
        return 0
    column = rows[0].index("text")
    return sum(1 for r in rows[1:] if EMAIL_PLACEHOLDER_RE.search(r[column]))


def sort_csv(path, keys):
    """Keep rows in date order. Documents retried on a later run are appended and would sit out of place."""
    if not os.path.exists(path):
        return
    rows = read_csv_rows(path)
    if len(rows) < 3:
        return
    header, body = rows[0], rows[1:]
    indexes = [header.index(k) for k in keys]
    ordered = sorted(body, key=lambda r: tuple(r[i] for i in indexes))
    if ordered != body:
        write_csv_rows(path, [header] + ordered)


def existing_dates(path):
    if not os.path.exists(path):
        return set()
    with open(path, newline="", encoding="utf-8") as handle:
        return {row["meeting_date"] for row in csv.DictReader(handle) if row.get("meeting_date")}


def recorded_transcripts(path):
    """Transcripts key on (date, type) since a meeting can have both kinds."""
    if not os.path.exists(path):
        return set()
    with open(path, newline="", encoding="utf-8") as handle:
        return {(row["meeting_date"], row["transcript_type"]) for row in csv.DictReader(handle)
                if row.get("meeting_date")}


def write_statement_and_note(fetcher, meeting, outputs, seen, kinds, now):
    note_url = meeting.note_url
    soup = None
    if (meeting.statement_html or meeting.statement_pdf) and meeting.date not in seen["statements"]:
        text, url, fmt, soup = fetch_document(fetcher, meeting.statement_html, meeting.statement_pdf)
        if "statements" in kinds:
            outputs["statements"].write([meeting.date, meeting.meeting_type, url, fmt, text, now])
            seen["statements"].add(meeting.date)
            print("statement %s (%s) %s" % (meeting.date, fmt, url))
        # Before 2021 the note is only discoverable from the statement page itself.
        note_url = note_url or (find_note_link(soup) if soup is not None else None)

    wants_note = "notes" in kinds and meeting.date not in seen["notes"] and meeting.date >= FIRST_NOTE_DATE
    if wants_note and not note_url and soup is None and meeting.statement_html:
        # The statement was recorded by an earlier run (or its note was not up yet), so the page
        # has to be read again. Best effort: notation-vote statements legitimately have no note.
        try:
            note_url = find_note_link(fetcher.soup(meeting.statement_html))
        except requests.RequestException as exc:
            print("warning: could not re-read %s for a note link: %s" % (meeting.statement_html, exc))

    if wants_note and note_url:
        text, url, fmt, _ = fetch_document(fetcher, note_url, None)
        outputs["notes"].write([meeting.date, url, fmt, text, now])
        seen["notes"].add(meeting.date)
        print("implementation note %s (%s) %s" % (meeting.date, fmt, url))


def write_minutes(fetcher, meeting, meetings_by_date, outputs, seen, minutes_cache, now):
    if meeting.date in seen["minutes"]:
        return

    html_url, pdf_url = meeting.minutes_html, meeting.minutes_pdf
    shared_with = meeting.minutes_shared_with
    if shared_with:
        host = meetings_by_date.get(shared_with)
        if host is None:
            print("skip minutes %s: referenced meeting %s not found" % (meeting.date, shared_with))
            return
        html_url, pdf_url = host.minutes_html, host.minutes_pdf
    if not (html_url or pdf_url):
        return

    # Some conference calls link the same minutes document with a different #anchor.
    cache_key = (html_url or pdf_url).split("#")[0]
    if cache_key in minutes_cache:
        text, url, fmt = minutes_cache[cache_key]
    else:
        text, url, fmt, _ = fetch_document(fetcher, html_url, pdf_url)
        minutes_cache[cache_key] = (text, url, fmt)
    # Some conference calls just link another meeting's minutes instead of carrying a "See end of
    # minutes of ..." marker. That document's address carries the other meeting's date, so use it
    # to flag the row; otherwise the same text would look like a separate meeting's minutes.
    host_date = date_from_url(url)
    if not shared_with and host_date and host_date != meeting.date and host_date in meetings_by_date:
        shared_with = host_date
    outputs["minutes"].write([meeting.date, shared_with or "", url, fmt, text, now])
    seen["minutes"].add(meeting.date)
    print("minutes %s (%s)%s %s" % (meeting.date, fmt, " shared with " + shared_with if shared_with else "", url))


def write_transcript(fetcher, meeting, kind, pdf_url, landing_url, outputs, seen, data_dir, now):
    """Transcripts are PDF only: read the text out of the download and keep just the text."""
    name = os.path.basename(urlparse(pdf_url).path)
    text_rel = os.path.join("text", os.path.splitext(name)[0] + ".txt")

    text, n_pages = pdf_text(fetcher.get(pdf_url).content)
    with open(os.path.join(data_dir, text_rel), "w", encoding="utf-8") as handle:
        handle.write(text)

    outputs["transcripts"].write([meeting.date, kind, meeting.meeting_type, pdf_url, landing_url,
                                  text_rel.replace("\\", "/"), n_pages, now])
    seen["transcripts"].add((meeting.date, kind))
    print("%s %s (%d pages) %s" % (kind, meeting.date, n_pages, name))


def write_presconf(fetcher, meeting, outputs, seen, data_dir, now):
    pdf_url = presconf_pdf(fetcher.soup(meeting.presconf_url))
    if pdf_url is None:
        print("no transcript link on %s" % meeting.presconf_url)
        return
    write_transcript(fetcher, meeting, "press_conference", pdf_url, meeting.presconf_url,
                     outputs, seen, data_dir, now)


def process(fetcher, meeting, meetings_by_date, outputs, seen, minutes_cache, kinds, data_dir, errors):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def attempt(label, step, *args):
        try:
            step(*args)
        except Exception as exc:  # a failing document must not block the meeting's other documents
            errors.append("%s %s: %r" % (meeting.date, label, exc))
            print("error on %s (%s): %r" % (meeting.date, label, exc))

    if "statements" in kinds or "notes" in kinds:
        attempt("statement", write_statement_and_note, fetcher, meeting, outputs, seen, kinds, now)
    if "minutes" in kinds:
        attempt("minutes", write_minutes, fetcher, meeting, meetings_by_date, outputs, seen,
                minutes_cache, now)
    if ("transcripts" in kinds and meeting.transcript_url
            and (meeting.date, "meeting") not in seen["transcripts"]):
        attempt("meeting transcript", write_transcript, fetcher, meeting, "meeting",
                meeting.transcript_url, "", outputs, seen, data_dir, now)
    if ("presconf" in kinds and meeting.presconf_url
            and (meeting.date, "press_conference") not in seen["transcripts"]):
        attempt("press conference", write_presconf, fetcher, meeting, outputs, seen, data_dir, now)


def calendar_years_present(soup):
    """Years that currently have a panel on the main calendar page."""
    found = set()
    for panel in soup.select("div.panel.panel-default"):
        heading = panel.find(class_="panel-heading")
        year_match = YEAR_RE.search(heading.get_text()) if heading else None
        if year_match:
            found.add(int(year_match.group(0)))
    return found


def collect_meetings(fetcher, years, errors):
    """Meetings by date. Archive pages that cannot be fetched are appended to `errors`."""
    meetings = {}
    covered_years = set()
    if max(years) >= CALENDAR_FIRST_YEAR:
        print("fetching %s" % CALENDAR_URL)
        cal_soup = fetcher.soup(CALENDAR_URL)
        covered_years = calendar_years_present(cal_soup)
        for meeting in parse_calendar(cal_soup, years & covered_years):
            meetings.setdefault(meeting.date, meeting)

    cutoff = min(covered_years) if covered_years else max(years) + 1
    for year in sorted(y for y in years if y < cutoff):
        url = HISTORICAL_URL.format(year=year)
        print("fetching %s" % url)
        try:
            soup = fetcher.soup(url)
        except requests.RequestException as exc:
            print("skipping %d: %s" % (year, exc))
            errors.append("%d archive page: %s" % (year, exc))
            continue
        for meeting in parse_historical_year(soup, year):
            meetings.setdefault(meeting.date, meeting)
    return meetings


def cell(row, column):
    value = row.get(column, "")
    return int(value) if column in INTEGER_COLUMNS and value != "" else value


def load_corpus(data_dir, filename, columns):
    rows = []
    path = os.path.join(data_dir, "csv", filename)
    if not os.path.exists(path):
        return rows
    with open(path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            rows.append([cell(row, c) for c in columns] + [row["text"]])
    return rows


def load_transcript_corpus(data_dir, kind, columns):
    """Transcript text sits in the text/ sidecars rather than inline in the index."""
    rows = []
    path = os.path.join(data_dir, "csv", "transcripts.csv")
    if not os.path.exists(path):
        return rows
    with open(path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row["transcript_type"] != kind:
                continue
            sidecar = os.path.join(data_dir, row["text_path"])
            if not os.path.exists(sidecar):
                print("missing text for %s" % row["meeting_date"])
                continue
            with open(sidecar, encoding="utf-8") as text_handle:
                rows.append([cell(row, c) for c in columns] + [text_handle.read()])
    return rows


def validate_data(data_dir):
    """Problems in the CSVs and text sidecars; an empty list means the data is sound.

    Run before every export so a changed Fed page that yields empty or truncated documents fails
    the job instead of being committed.
    """
    problems = []
    specs = (
        ("statements.csv", STATEMENT_COLUMNS, ("meeting_date",), MIN_TEXT_CHARS["statements"]),
        ("implementation_notes.csv", NOTE_COLUMNS, ("meeting_date",), MIN_TEXT_CHARS["implementation_notes"]),
        ("minutes.csv", MINUTES_COLUMNS, ("meeting_date",), MIN_TEXT_CHARS["minutes"]),
        ("transcripts.csv", TRANSCRIPT_COLUMNS, ("meeting_date", "transcript_type"), None),
    )
    for filename, columns, key, min_chars in specs:
        path = os.path.join(data_dir, "csv", filename)
        if not os.path.exists(path):
            problems.append("%s is missing" % filename)
            continue
        with open(path, newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames != columns:
                problems.append("%s: columns are %s, expected %s" % (filename, reader.fieldnames, columns))
                continue
            keys = Counter()
            for row in reader:
                label = "%s %s" % (filename, "/".join(row[k] for k in key))
                keys[tuple(row[k] for k in key)] += 1
                if not row["meeting_date"] or not row["source_url"]:
                    problems.append("%s: empty meeting_date or source_url" % label)
                if min_chars is not None:
                    if len(row["text"].strip()) < min_chars:
                        problems.append("%s: text is %d characters (floor %d)"
                                        % (label, len(row["text"].strip()), min_chars))
                    continue
                sidecar = os.path.join(data_dir, row["text_path"])
                if not os.path.exists(sidecar):
                    problems.append("%s: %s is missing" % (label, row["text_path"]))
                    continue
                if not row["n_pages"].isdigit() or int(row["n_pages"]) < 1:
                    problems.append("%s: n_pages is %r" % (label, row["n_pages"]))
                    continue
                with open(sidecar, encoding="utf-8") as text_handle:
                    size = len(text_handle.read().strip())
                if size < MIN_CHARS_PER_PAGE * int(row["n_pages"]):
                    problems.append("%s: %d characters over %s pages (floor %d per page)"
                                    % (label, size, row["n_pages"], MIN_CHARS_PER_PAGE))
            problems.extend("%s: %s appears %d times" % (filename, "/".join(k), n)
                            for k, n in keys.items() if n > 1)
    return problems


def check_data(data_dir):
    problems = validate_data(data_dir)
    for problem in problems[:25]:
        print("invalid: " + problem)
    if len(problems) > 25:
        print("... and %d more" % (len(problems) - 25))
    print("validation: %s" % ("%d problem(s)" % len(problems) if problems else "ok"))
    return not problems


def corpora(data_dir):
    """The five communication types, each as (name, columns, rows) with text as the last column."""
    meta = ["meeting_date", "meeting_type", "source_url", "source_format", "fetched_at"]
    yield ("statements", meta + ["text"],
           load_corpus(data_dir, "statements.csv", meta))
    note_columns = ["meeting_date", "source_url", "source_format", "fetched_at"]
    yield ("implementation_notes", note_columns + ["text"],
           load_corpus(data_dir, "implementation_notes.csv", note_columns))
    minutes_columns = ["meeting_date", "shared_with_meeting_date", "source_url", "source_format",
                       "fetched_at"]
    yield ("minutes", minutes_columns + ["text"],
           load_corpus(data_dir, "minutes.csv", minutes_columns))
    transcript_columns = ["meeting_date", "meeting_type", "source_url", "n_pages", "fetched_at"]
    for name, kind in (("meeting_transcripts", "meeting"),
                       ("press_conferences", "press_conference")):
        yield (name, transcript_columns + ["text"],
               load_transcript_corpus(data_dir, kind, transcript_columns))


def write_xlsx(path, name, columns, rows):
    from openpyxl import Workbook
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
    from openpyxl.styles import Font

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = name[:31]
    sheet.append(columns + ["full_chars", "truncated"])
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    sheet.freeze_panes = "A2"
    for row in rows:
        text = row[-1]
        # Control characters make openpyxl raise, which would abort the whole export.
        shown = ILLEGAL_CHARACTERS_RE.sub("", text[:EXCEL_CELL_LIMIT])
        sheet.append(row[:-1] + [shown, len(text), "YES" if len(text) > EXCEL_CELL_LIMIT else ""])
    workbook.save(path)


def write_sqlite(path, name, columns, rows):
    import sqlite3

    if os.path.exists(path):
        os.remove(path)
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE %s (%s)" % (name, ", ".join(
        '"%s" %s' % (c, "INTEGER" if c in INTEGER_COLUMNS else "TEXT") for c in columns)))
    connection.executemany("INSERT INTO %s VALUES (%s)" % (name, ", ".join("?" * len(columns))), rows)
    connection.execute("CREATE INDEX idx_%s_meeting_date ON %s (meeting_date)" % (name, name))
    connection.commit()
    connection.close()


def write_parquet(path, columns, rows):
    import pyarrow
    import pyarrow.parquet

    table = pyarrow.table({c: [row[i] if c in INTEGER_COLUMNS else str(row[i]) for row in rows]
                           for i, c in enumerate(columns)})
    pyarrow.parquet.write_table(table, path, compression="snappy")


def corpus_digest(columns, rows):
    """Fingerprint of a corpus' content and export layout, to tell whether its exports are stale."""
    digest = hashlib.sha256(("v%d" % EXPORT_VERSION).encode())
    digest.update("\x1f".join(columns).encode("utf-8"))
    for row in rows:
        for value in row:
            digest.update(str(value).encode("utf-8"))
            digest.update(b"\x1f")
        digest.update(b"\x1e")
    return digest.hexdigest()


def export_all(data_dir, force=False):
    """Write each corpus to excel/, sql/ and parquet/ -- five files in each folder.

    A corpus whose source rows are unchanged since the last export is left alone. That keeps git
    history small: the Excel files embed a timestamp, so rewriting them always changes the bytes,
    and a new press conference should not re-commit the 58 MB transcripts database.
    """
    folders = {name: os.path.join(data_dir, name) for name in ("excel", "sql", "parquet")}
    for folder in folders.values():
        os.makedirs(folder, exist_ok=True)

    manifest_path = os.path.join(data_dir, MANIFEST_NAME)
    try:
        with open(manifest_path, encoding="utf-8") as handle:
            manifest = json.load(handle)
    except (OSError, ValueError):
        manifest = {}

    print("\n%-22s %6s %10s %8s %8s" % ("corpus", "rows", "excel", "sql", "parquet"))
    for name, columns, rows in corpora(data_dir):
        if not rows:
            print("%-22s %6s  no source CSV, skipped" % (name, "-"))
            continue
        paths = (os.path.join(folders["excel"], name + ".xlsx"),
                 os.path.join(folders["sql"], name + ".db"),
                 os.path.join(folders["parquet"], name + ".parquet"))
        digest = corpus_digest(columns, rows)
        if not force and manifest.get(name) == digest and all(os.path.exists(p) for p in paths):
            print("%-22s %6d  unchanged, exports kept" % (name, len(rows)))
            continue
        write_xlsx(paths[0], name, columns, rows)
        write_sqlite(paths[1], name, columns, rows)
        try:
            write_parquet(paths[2], columns, rows)
            parquet_state = "ok"
        except ImportError:
            parquet_state = "no pyarrow"
        manifest[name] = digest
        cut = sum(1 for row in rows if len(row[-1]) > EXCEL_CELL_LIMIT)
        print("%-22s %6d %10s %8s %8s" % (name, len(rows), "%d cut" % cut if cut else "ok",
                                          "ok", parquet_state))

    with open(manifest_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print("\nexports in %s" % ", ".join(sorted(folders.values())))


def main():
    parser = argparse.ArgumentParser(
        description="Scrape FOMC statements, implementation notes, minutes and transcripts. "
                    "Safe to re-run: documents already in the CSVs are skipped, never duplicated.")
    parser.add_argument("--start-year", type=int, default=2000)
    parser.add_argument("--end-year", type=int, default=datetime.now().year)
    parser.add_argument("--output-dir", default="./data",
                        help="use a new directory to collect everything from scratch")
    parser.add_argument("--documents", default=",".join(DOCUMENT_TYPES),
                        help="comma-separated subset of: %s" % ", ".join(DOCUMENT_TYPES))
    parser.add_argument("--delay", type=float, default=1.0, help="seconds between requests")
    parser.add_argument("--export-only", action="store_true",
                        help="rebuild excel/, sql/ and parquet/ from the CSVs already on disk")
    parser.add_argument("--no-export", action="store_true",
                        help="scrape only, do not write the excel/, sql/ and parquet/ folders")
    parser.add_argument("--force-export", action="store_true",
                        help="rebuild every export even if its corpus is unchanged")
    parser.add_argument("--validate-only", action="store_true",
                        help="check the CSVs and text files for problems, then exit")
    args = parser.parse_args()

    if args.validate_only:
        return 0 if check_data(args.output_dir) else 1
    if args.export_only:
        if not check_data(args.output_dir):
            return 1
        export_all(args.output_dir, force=args.force_export)
        return 0

    if args.end_year < args.start_year:
        parser.error("--end-year must not be before --start-year")
    kinds = {k.strip() for k in args.documents.split(",") if k.strip()}
    unknown = kinds - set(DOCUMENT_TYPES)
    if unknown:
        parser.error("unknown --documents value: %s" % ", ".join(sorted(unknown)))

    for sub in ("csv", "text"):
        os.makedirs(os.path.join(args.output_dir, sub), exist_ok=True)
    csv_dir = os.path.join(args.output_dir, "csv")
    paths = {
        "statements": os.path.join(csv_dir, "statements.csv"),
        "notes": os.path.join(csv_dir, "implementation_notes.csv"),
        "minutes": os.path.join(csv_dir, "minutes.csv"),
        "transcripts": os.path.join(csv_dir, "transcripts.csv"),
    }

    fetcher = Fetcher(delay=args.delay)
    errors = []
    try:
        meetings = collect_meetings(fetcher, set(range(args.start_year, args.end_year + 1)), errors)
    except KeyboardInterrupt:
        print("interrupted")
        return 1
    print("found %d meetings" % len(meetings))
    if not meetings:
        # The Fed pages parsed to nothing, i.e. the layout changed; do not report success.
        print("error: no meetings found, the page layout may have changed")
        return 1

    # Only now that the Fed pages were read is it safe to touch the stored data.
    stale = {kind: drop_placeholder_rows(paths[kind]) for kind in ("statements", "notes", "minutes")
             if kind in kinds}
    if any(stale.values()):
        print("downloading %d documents again: their stored text held an undecoded e-mail placeholder"
              % sum(len(rows) for rows in stale.values()))
    seen = {name: existing_dates(path) for name, path in paths.items() if name != "transcripts"}
    seen["transcripts"] = recorded_transcripts(paths["transcripts"])
    print("already recorded: %d statements, %d notes, %d minutes, %d transcripts"
          % (len(seen["statements"]), len(seen["notes"]), len(seen["minutes"]),
             len(seen["transcripts"])))

    try:
        outputs = {
            "statements": CsvOutput(paths["statements"], STATEMENT_COLUMNS),
            "notes": CsvOutput(paths["notes"], NOTE_COLUMNS),
            "minutes": CsvOutput(paths["minutes"], MINUTES_COLUMNS),
            "transcripts": CsvOutput(paths["transcripts"], TRANSCRIPT_COLUMNS),
        }
    except ValueError as exc:
        print("error: %s" % exc)
        return 1

    minutes_cache = {}
    try:
        for date in sorted(meetings):
            try:
                process(fetcher, meetings[date], meetings, outputs, seen, minutes_cache,
                        kinds, args.output_dir, errors)
            except Exception as exc:  # backstop: nothing should escape process(), but never stall
                errors.append("%s: %r" % (date, exc))
                print("error on %s: %r" % (date, exc))
    except KeyboardInterrupt:
        print("interrupted")
        return 1
    finally:
        for output in outputs.values():
            output.close()
        for kind, rows in stale.items():
            restore_rows(paths[kind], rows)

    sort_csv(paths["statements"], ("meeting_date",))
    sort_csv(paths["notes"], ("meeting_date",))
    sort_csv(paths["minutes"], ("meeting_date",))
    sort_csv(paths["transcripts"], ("meeting_date", "transcript_type"))
    leftover = sum(count_placeholder_rows(paths[kind]) for kind in ("statements", "notes", "minutes"))
    if leftover:
        errors.append("%d stored documents still contain the [email protected] placeholder; "
                      "the page markup may have changed" % leftover)

    valid = check_data(args.output_dir)
    if valid and not args.no_export:
        export_all(args.output_dir, force=args.force_export)
    if errors:
        print("\n%d error(s); they are retried on the next run:" % len(errors))
        for error in errors:
            print("  " + error)
    return 0 if valid and not errors else 1


if __name__ == "__main__":
    sys.exit(main())
