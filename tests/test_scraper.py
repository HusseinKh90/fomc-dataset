"""Offline tests: no network and no real Fed pages.

The HTML below is synthetic, shaped the way the parsers expect, so these guard the scraper's own
logic (dates, text extraction, de-duplication, error handling, exports). They cannot notice the
Fed redesigning its site; the monthly run's validation step is what catches that.
"""
import csv
import os
import sys

import pytest

import fomc_scraper as fs
from helpers import StubFetcher, make_pdf, page, words

BASE = fs.BASE


# --- dates and classification -------------------------------------------------------------

@pytest.mark.parametrize("text, default_year, expected", [
    ("January 31-February 1 Meeting - 2006", None, "2006-02-01"),
    ("February 1-2, 2011", None, "2011-02-02"),
    ("January 29/30 Meeting - 2008", None, "2008-01-30"),
    ("March 2", 2020, "2020-03-02"),
    ("no date here", 2020, None),
])
def test_date_from_text(text, default_year, expected):
    assert fs.date_from_text(text, default_year) == expected


def test_date_from_url():
    assert fs.date_from_url(BASE + "/newsevents/pressreleases/monetary20200303a.htm") == "2020-03-03"
    assert fs.date_from_url(BASE + "/fomc/minutes/20010320.htm#anchor") == "2001-03-20"
    assert fs.date_from_url("https://example.com/about.htm") is None
    assert fs.date_from_url(None) is None


@pytest.mark.parametrize("text, expected", [
    ("January 28-29 Meeting - 2025", "scheduled"),
    ("March 15 Conference Call - 2020", "conference_call"),
    ("October 4 Notation Vote - 2019", "notation_vote"),
    ("March 3 Unscheduled Meeting - 2020", "unscheduled"),
])
def test_meeting_type_from(text, expected):
    assert fs.meeting_type_from(text) == expected


# --- HTML parsing -------------------------------------------------------------------------

def test_html_text_keeps_list_items_once_and_drops_furniture():
    html = page(
        # Under 200 characters of paragraphs makes html_text fall back to its old-template path.
        "The Committee decided to raise the target range for the federal funds rate. " + words(250),
        "Last Update: January 1, 2020",
        '<a href="/">Home</a>',
        extra="<blockquote><ul><li>Roll over all principal payments.</li>"
              "<li>Cap redemptions at $60 billion.</li></ul></blockquote>")
    text = fs.html_text(fs.BeautifulSoup(html, "lxml"))
    assert "target range for the federal funds rate" in text
    assert text.count("- Roll over all principal payments.") == 1
    assert "- Cap redemptions at $60 billion." in text
    assert "Last Update" not in text
    assert "Home" not in text


def historical_panel(heading, body):
    return ('<div class="panel panel-default"><div class="panel-heading"><h5>%s</h5></div>'
            '<div class="panel-body">%s</div></div>' % (heading, body))


def test_parse_historical_year():
    html = historical_panel(
        "January 31-February 1 Meeting - 2006",
        '<p>Statement: <a href="/boarddocs/press/monetary/2006/20060201/default.htm">HTML</a> '
        '<a href="/boarddocs/press/monetary/2006/20060201/20060201.pdf">PDF</a></p>'
        '<p>Minutes: <a href="/fomc/minutes/20060201.htm">HTML</a></p>'
        '<a href="/monetarypolicy/files/FOMC20060201meeting.pdf">Transcript</a>')
    html += historical_panel(
        "January 9 Conference Call - 2006",
        '<p>Statement: <a href="/x/20060109.htm">HTML</a></p>'
        '<p><em>See end of minutes of January 31, 2006 meeting</em></p>')
    html += historical_panel("Transcripts and other historical materials", "<p>nothing</p>")
    meetings = fs.parse_historical_year(fs.BeautifulSoup("<html><body>%s</body></html>" % html, "lxml"), 2006)

    assert [m.date for m in meetings] == ["2006-02-01", "2006-01-09"]
    scheduled, call = meetings
    assert scheduled.meeting_type == "scheduled"
    assert scheduled.statement_html.endswith("/20060201/default.htm")
    assert scheduled.statement_pdf.endswith("20060201.pdf")
    assert scheduled.minutes_html.endswith("/fomc/minutes/20060201.htm")
    assert scheduled.transcript_url.endswith("FOMC20060201meeting.pdf")
    assert call.meeting_type == "conference_call"
    assert call.minutes_shared_with == "2006-01-31"
    assert call.minutes_html is None


def test_parse_calendar_reads_note_and_skips_future_rows():
    html = (
        '<html><body><div class="panel panel-default"><div class="panel-heading"><h4>2025 FOMC Meetings</h4></div>'
        '<div class="fomc-meeting">'
        '<div>Statement: <a href="/newsevents/pressreleases/monetary20250129a.htm">HTML</a> '
        '<a href="/newsevents/pressreleases/monetary20250129a.pdf">PDF</a> '
        '<a href="/newsevents/pressreleases/monetary20250129a1.htm">Implementation Note issued January 29, 2025</a></div>'
        '<div>Minutes: <a href="/monetarypolicy/fomcminutes20250219.htm">HTML</a></div>'
        '<a href="/monetarypolicy/fomcpresconf20250129.htm">Press Conference</a>'
        '</div>'
        '<div class="fomc-meeting"><div>January 27-28</div></div>'  # nothing published yet
        '</div></body></html>')
    meetings = fs.parse_calendar(fs.BeautifulSoup(html, "lxml"), {2025})
    assert len(meetings) == 1
    meeting = meetings[0]
    assert meeting.date == "2025-01-29"
    assert meeting.statement_html.endswith("monetary20250129a.htm")
    assert meeting.statement_pdf.endswith("monetary20250129a.pdf")
    assert meeting.note_url.endswith("monetary20250129a1.htm")
    assert meeting.minutes_html.endswith("fomcminutes20250219.htm")
    assert meeting.presconf_url.endswith("fomcpresconf20250129.htm")


# --- PDFs are read in memory and never stored ---------------------------------------------

def transcript_pdf():
    pages = []
    for i in range(1, 9):
        pages.append(["January 27-28, 2015 %d of 8" % i,           # running header, number varies
                      "CHAIRMAN YELLEN. Distinct opening remark %s." % ("alpha beta gamma delta eps zeta eta theta".split()[i - 1]),
                      "Body text for page %s stays." % ("one two three four five six seven eight".split()[i - 1]),
                      "(more)"])
    return make_pdf(pages)


def test_pdf_text_strips_running_headers_and_keeps_content():
    text, n_pages = fs.pdf_text(transcript_pdf())
    assert n_pages == 8
    assert "of 8" not in text
    assert "(more)" not in text
    assert "Body text for page one stays." in text
    assert "Body text for page eight stays." in text


def test_write_transcript_stores_text_only(tmp_path):
    (tmp_path / "csv").mkdir()
    (tmp_path / "text").mkdir()
    url = BASE + "/monetarypolicy/files/FOMC20150128meeting.pdf"
    outputs = {"transcripts": fs.CsvOutput(str(tmp_path / "csv" / "transcripts.csv"), fs.TRANSCRIPT_COLUMNS)}
    seen = {"transcripts": set()}
    meeting = fs.Meeting(date="2015-01-28", meeting_type="scheduled")
    fs.write_transcript(StubFetcher(files={url: transcript_pdf()}), meeting, "meeting", url, "",
                        outputs, seen, str(tmp_path), "now")
    outputs["transcripts"].close()

    files = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file())
    assert files == [os.path.join("csv", "transcripts.csv"), os.path.join("text", "FOMC20150128meeting.txt")]
    rows = list(csv.DictReader(open(tmp_path / "csv" / "transcripts.csv", newline="", encoding="utf-8")))
    assert rows[0]["text_path"] == "text/FOMC20150128meeting.txt"
    assert rows[0]["n_pages"] == "8"
    assert ("2015-01-28", "meeting") in seen["transcripts"]
    assert "pdf_path" not in rows[0]


# --- CSV safety ---------------------------------------------------------------------------

def test_csv_output_refuses_a_different_header(tmp_path):
    path = tmp_path / "statements.csv"
    path.write_text("meeting_date,text\n2020-01-29,hello\n", encoding="utf-8")
    with pytest.raises(ValueError, match="migrate"):
        fs.CsvOutput(str(path), fs.STATEMENT_COLUMNS)


def test_csv_output_appends_with_lf(tmp_path):
    path = str(tmp_path / "notes.csv")
    for _ in range(2):
        out = fs.CsvOutput(path, fs.NOTE_COLUMNS)
        out.write(["2016-01-27", "u", "html", "multi\nline", "now"])
        out.close()
    raw = open(path, "rb").read()
    assert b"\r" not in raw
    assert raw.count(b"meeting_date") == 1  # header written once


# --- note backfill ------------------------------------------------------------------------

STATEMENT_URL = BASE + "/newsevents/pressreleases/monetary20170315a.htm"
NOTE_URL = BASE + "/newsevents/pressreleases/monetary20170315a1.htm"


class Sink:
    def __init__(self):
        self.rows = []

    def write(self, row):
        self.rows.append(row)


def run_note_step(meeting, seen, fetcher):
    outputs = {"statements": Sink(), "notes": Sink()}
    fs.write_statement_and_note(fetcher, meeting, outputs, seen, {"statements", "notes"}, "now")
    return outputs


def statement_page_with_note():
    return page("Statement text.", '<a href="%s">Implementation Note issued March 15, 2017</a>' % NOTE_URL)


def test_note_is_backfilled_when_statement_was_already_recorded():
    fetcher = StubFetcher(pages={STATEMENT_URL: statement_page_with_note(),
                                 NOTE_URL: page("Implementation note body text for the desk.")})
    meeting = fs.Meeting(date="2017-03-15", meeting_type="scheduled", statement_html=STATEMENT_URL)
    seen = {"statements": {"2017-03-15"}, "notes": set()}
    outputs = run_note_step(meeting, seen, fetcher)
    assert outputs["statements"].rows == []
    assert [row[0] for row in outputs["notes"].rows] == ["2017-03-15"]
    assert "2017-03-15" in seen["notes"]


def test_no_note_lookup_before_the_first_implementation_note():
    fetcher = StubFetcher(pages={})
    meeting = fs.Meeting(date="2010-01-27", meeting_type="scheduled", statement_html=STATEMENT_URL)
    run_note_step(meeting, {"statements": {"2010-01-27"}, "notes": set()}, fetcher)
    assert fetcher.calls == []  # would otherwise re-read ~190 statement pages every run


def test_no_note_lookup_when_note_already_recorded():
    fetcher = StubFetcher(pages={})
    meeting = fs.Meeting(date="2017-03-15", meeting_type="scheduled", statement_html=STATEMENT_URL)
    run_note_step(meeting, {"statements": {"2017-03-15"}, "notes": {"2017-03-15"}}, fetcher)
    assert fetcher.calls == []


def test_failed_note_lookup_is_a_warning_not_an_error(capsys):
    fetcher = StubFetcher(fail={STATEMENT_URL})
    meeting = fs.Meeting(date="2017-03-15", meeting_type="scheduled", statement_html=STATEMENT_URL)
    outputs = run_note_step(meeting, {"statements": {"2017-03-15"}, "notes": set()}, fetcher)
    assert outputs["notes"].rows == []
    assert "warning" in capsys.readouterr().out


# --- end to end through main() ------------------------------------------------------------

def scenario(tmp_path, monkeypatch, fetcher, meetings, extra_args=()):
    monkeypatch.setattr(fs, "Fetcher", lambda **kwargs: fetcher)
    monkeypatch.setattr(fs, "collect_meetings", lambda f, years, errors: meetings)
    monkeypatch.setattr(sys, "argv", ["fomc_scraper.py", "--output-dir", str(tmp_path), "--delay", "0",
                                      "--documents", "statements,minutes", "--no-export", *extra_args])
    return fs.main()


def two_meetings():
    urls = {}
    meetings = {}
    for date, tag in (("2024-01-31", "a"), ("2024-03-20", "b")):
        urls[tag] = (BASE + "/s_%s.htm" % tag, BASE + "/m_%s.htm" % tag)
        meetings[date] = fs.Meeting(date=date, meeting_type="scheduled",
                                    statement_html=urls[tag][0], minutes_html=urls[tag][1])
    pages = {}
    for statement, minutes in urls.values():
        pages[statement] = page(words(400))
        pages[minutes] = page(*[words(1500) for _ in range(5)])
    return meetings, pages, urls


def count_rows(path):
    with open(path, newline="", encoding="utf-8") as handle:
        return len(list(csv.DictReader(handle)))


def test_rerunning_never_duplicates_rows(tmp_path, monkeypatch):
    meetings, pages, _ = two_meetings()
    for _ in range(3):
        assert scenario(tmp_path, monkeypatch, StubFetcher(pages=pages), meetings) == 0
    assert count_rows(tmp_path / "csv" / "statements.csv") == 2
    assert count_rows(tmp_path / "csv" / "minutes.csv") == 2


def test_one_unexpected_error_does_not_stall_later_meetings_and_fails_the_run(tmp_path, monkeypatch, capsys):
    meetings, pages, urls = two_meetings()
    broken = StubFetcher(pages=pages, boom={urls["a"][1]})  # minutes of the first meeting blow up
    assert scenario(tmp_path, monkeypatch, broken, meetings) == 1
    assert "simulated parser failure" in capsys.readouterr().out
    assert count_rows(tmp_path / "csv" / "statements.csv") == 2   # both statements kept
    assert count_rows(tmp_path / "csv" / "minutes.csv") == 1      # only the healthy meeting's minutes

    assert scenario(tmp_path, monkeypatch, StubFetcher(pages=pages), meetings) == 0  # next run heals
    assert count_rows(tmp_path / "csv" / "minutes.csv") == 2


def test_network_failure_on_one_meeting_fails_the_run(tmp_path, monkeypatch):
    meetings, pages, urls = two_meetings()
    assert scenario(tmp_path, monkeypatch, StubFetcher(pages=pages, fail={urls["b"][0]}), meetings) == 1


def test_no_meetings_found_is_a_failure(tmp_path, monkeypatch, capsys):
    assert scenario(tmp_path, monkeypatch, StubFetcher(), {}) == 1
    assert "no meetings found" in capsys.readouterr().out


def test_old_csv_layout_is_rejected_instead_of_corrupted(tmp_path, monkeypatch, capsys):
    (tmp_path / "csv").mkdir()
    (tmp_path / "csv" / "transcripts.csv").write_text(
        "meeting_date,transcript_type,meeting_type,source_url,landing_url,pdf_path,text_path,n_pages,fetched_at\n",
        encoding="utf-8")
    meetings, pages, _ = two_meetings()
    assert scenario(tmp_path, monkeypatch, StubFetcher(pages=pages), meetings) == 1
    assert "migrate" in capsys.readouterr().out


def test_invalid_scraped_data_fails_the_run(tmp_path, monkeypatch, capsys):
    meetings, pages, urls = two_meetings()
    pages[urls["a"][0]] = page("too short")  # a layout change that leaves almost no text
    assert scenario(tmp_path, monkeypatch, StubFetcher(pages=pages), meetings) == 1
    assert "floor 300" in capsys.readouterr().out


# --- validation ---------------------------------------------------------------------------

def write_dataset(root, statement_chars=400, transcript_chars=8 * 600, n_pages="8", sidecar=True):
    (root / "csv").mkdir(parents=True, exist_ok=True)
    (root / "text").mkdir(exist_ok=True)

    def write(name, columns, rows):
        with open(root / "csv" / name, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(columns)
            writer.writerows(rows)

    write("statements.csv", fs.STATEMENT_COLUMNS,
          [["2024-01-31", "scheduled", "u", "html", words(statement_chars), "now"]])
    write("implementation_notes.csv", fs.NOTE_COLUMNS, [])
    write("minutes.csv", fs.MINUTES_COLUMNS, [])
    write("transcripts.csv", fs.TRANSCRIPT_COLUMNS,
          [["2018-01-31", "meeting", "scheduled", "u", "", "text/t.txt", n_pages, "now"]])
    if sidecar:
        (root / "text" / "t.txt").write_text(words(transcript_chars), encoding="utf-8")


def test_validate_accepts_sound_data(tmp_path):
    write_dataset(tmp_path)
    assert fs.validate_data(str(tmp_path)) == []


@pytest.mark.parametrize("kwargs, expected", [
    ({"statement_chars": 100}, "floor 300"),
    ({"transcript_chars": 800}, "per page"),
    ({"n_pages": "many"}, "n_pages"),
    ({"sidecar": False}, "is missing"),
])
def test_validate_flags_problems(tmp_path, kwargs, expected):
    write_dataset(tmp_path, **kwargs)
    problems = fs.validate_data(str(tmp_path))
    assert problems and expected in " ".join(problems)


def test_validate_flags_duplicates(tmp_path):
    write_dataset(tmp_path)
    with open(tmp_path / "csv" / "statements.csv", newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    with open(tmp_path / "csv" / "statements.csv", "w", newline="", encoding="utf-8") as handle:
        csv.writer(handle, lineterminator="\n").writerows(rows + [rows[1]])
    assert any("appears 2 times" in p for p in fs.validate_data(str(tmp_path)))


# --- exports ------------------------------------------------------------------------------

class Counting:
    """Wraps the export writers to see which corpora actually get rewritten."""

    def __init__(self, monkeypatch):
        self.calls = []
        for name in ("write_xlsx", "write_sqlite", "write_parquet"):
            original = getattr(fs, name)

            def wrapper(*args, _name=name, _original=original):
                self.calls.append((_name, os.path.basename(args[0])))
                return _original(*args)
            monkeypatch.setattr(fs, name, wrapper)

    def reset(self):
        calls, self.calls = self.calls, []
        return sorted({os.path.splitext(base)[0] for _, base in calls}), len(calls)


def test_export_rewrites_only_changed_corpora(tmp_path, monkeypatch):
    pytest.importorskip("pyarrow")
    write_dataset(tmp_path)
    counting = Counting(monkeypatch)
    data = str(tmp_path)

    fs.export_all(data)
    assert counting.reset() == (["meeting_transcripts", "statements"], 6)
    manifest = (tmp_path / fs.MANIFEST_NAME).read_text(encoding="utf-8")

    fs.export_all(data)
    assert counting.reset() == ([], 0)                       # nothing changed, nothing rewritten
    assert (tmp_path / fs.MANIFEST_NAME).read_text(encoding="utf-8") == manifest

    with open(tmp_path / "csv" / "statements.csv", newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    rows[1][rows[0].index("fetched_at")] = "later"
    with open(tmp_path / "csv" / "statements.csv", "w", newline="", encoding="utf-8") as handle:
        csv.writer(handle, lineterminator="\n").writerows(rows)
    fs.export_all(data)
    assert counting.reset() == (["statements"], 3)           # transcripts left alone

    (tmp_path / "sql" / "statements.db").unlink()            # a missing file is rebuilt too
    fs.export_all(data)
    assert counting.reset() == (["statements"], 3)

    fs.export_all(data, force=True)
    assert counting.reset() == (["meeting_transcripts", "statements"], 6)


def test_export_types_page_counts_as_integers(tmp_path):
    pyarrow_parquet = pytest.importorskip("pyarrow.parquet")
    import sqlite3
    write_dataset(tmp_path)
    fs.export_all(str(tmp_path))

    db = sqlite3.connect(str(tmp_path / "sql" / "meeting_transcripts.db"))
    assert db.execute("select typeof(n_pages) from meeting_transcripts").fetchone() == ("integer",)
    assert db.execute("select count(*) from sqlite_master where type='index'").fetchone() == (1,)
    db.close()
    schema = pyarrow_parquet.read_schema(str(tmp_path / "parquet" / "meeting_transcripts.parquet"))
    assert str(schema.field("n_pages").type) == "int64"
    assert str(schema.field("meeting_date").type) == "string"


def test_export_survives_control_characters(tmp_path):
    pytest.importorskip("pyarrow")
    write_dataset(tmp_path, statement_chars=400)
    path = tmp_path / "csv" / "statements.csv"
    path.write_text(path.read_text(encoding="utf-8").replace("lorem", "lo\x0brem", 1), encoding="utf-8")
    fs.export_all(str(tmp_path))  # openpyxl raises IllegalCharacterError without the sanitising
    assert (tmp_path / "excel" / "statements.xlsx").exists()


# --- shared minutes -----------------------------------------------------------------------

def run_minutes(meeting, meetings_by_date, url):
    fetcher = StubFetcher(pages={url: page(*[words(1500) for _ in range(5)])})
    outputs = {"minutes": Sink()}
    fs.write_minutes(fetcher, meeting, meetings_by_date, outputs, {"minutes": set()}, {}, "now")
    return outputs["minutes"].rows[0]


def test_minutes_linked_to_another_meetings_document_are_flagged_as_shared():
    host = fs.Meeting(date="2003-03-18", meeting_type="scheduled")
    call = fs.Meeting(date="2003-03-25", meeting_type="conference_call",
                      minutes_html=BASE + "/fomc/minutes/20030318.htm#March2003minutes")
    row = run_minutes(call, {"2003-03-18": host, "2003-03-25": call}, call.minutes_html)
    assert row[0:2] == ["2003-03-25", "2003-03-18"]


def test_a_meetings_own_minutes_are_not_flagged():
    own = fs.Meeting(date="2025-01-29", meeting_type="scheduled",
                     minutes_html=BASE + "/monetarypolicy/fomcminutes20250129.htm")
    assert run_minutes(own, {"2025-01-29": own}, own.minutes_html)[1] == ""


def test_an_unknown_address_date_is_not_mistaken_for_a_host_meeting():
    odd = fs.Meeting(date="2025-01-29", meeting_type="scheduled",
                     minutes_html=BASE + "/monetarypolicy/fomcminutes20250219.htm")
    assert run_minutes(odd, {"2025-01-29": odd}, odd.minutes_html)[1] == ""


def test_committed_minutes_count_each_document_once_when_filtered():
    path = os.path.join(os.path.dirname(__file__), "..", "data", "csv", "minutes.csv")
    if not os.path.exists(path):
        pytest.skip("no committed data")
    with open(path, newline="", encoding="utf-8") as handle:
        rows = [r for r in csv.DictReader(handle) if not r["shared_with_meeting_date"]]
    texts = [r["text"] for r in rows]
    assert len(texts) == len(set(texts)), "documents repeated even after dropping shared_with rows"


# --- a failing document does not block the meeting's others -------------------------------

def test_failed_statement_does_not_block_minutes_or_the_run_continuing(tmp_path, monkeypatch):
    meetings, pages, urls = two_meetings()
    broken = StubFetcher(pages=pages, boom={urls["a"][0]})   # first meeting's statement fails
    assert scenario(tmp_path, monkeypatch, broken, meetings) == 1
    assert count_rows(tmp_path / "csv" / "statements.csv") == 1
    assert count_rows(tmp_path / "csv" / "minutes.csv") == 2  # its minutes were still collected


# --- press conferences --------------------------------------------------------------------

LANDING = BASE + "/monetarypolicy/fomcpresconf20250129.htm"
PRESCONF_PDF = BASE + "/mediacenter/files/FOMCpresconf20250129.pdf"


def presconf_setup(tmp_path, landing_html):
    (tmp_path / "csv").mkdir()
    (tmp_path / "text").mkdir()
    outputs = {"transcripts": fs.CsvOutput(str(tmp_path / "csv" / "transcripts.csv"), fs.TRANSCRIPT_COLUMNS)}
    meeting = fs.Meeting(date="2025-01-29", meeting_type="scheduled", presconf_url=LANDING)
    fetcher = StubFetcher(pages={LANDING: landing_html}, files={PRESCONF_PDF: transcript_pdf()})
    seen = {"transcripts": set()}
    return fetcher, meeting, outputs, seen


def test_press_conference_transcript_is_found_and_stored_as_text(tmp_path):
    landing = page('<a href="/files/projections.pdf">PDF</a>',
                   '<a href="/mediacenter/files/FOMCpresconf20250129.pdf">Press Conference Transcript</a>')
    fetcher, meeting, outputs, seen = presconf_setup(tmp_path, landing)
    fs.write_presconf(fetcher, meeting, outputs, seen, str(tmp_path), "now")
    outputs["transcripts"].close()
    assert ("2025-01-29", "press_conference") in seen["transcripts"]
    assert (tmp_path / "text" / "FOMCpresconf20250129.txt").exists()
    assert not list(tmp_path.rglob("*.pdf"))


def test_press_conference_without_a_transcript_link_is_retried_later(tmp_path, capsys):
    landing = page('<a href="/files/projections.pdf">PDF</a>')   # transcript not posted yet
    fetcher, meeting, outputs, seen = presconf_setup(tmp_path, landing)
    fs.write_presconf(fetcher, meeting, outputs, seen, str(tmp_path), "now")
    outputs["transcripts"].close()
    assert seen["transcripts"] == set()                            # not recorded, so tried again
    assert "no transcript link" in capsys.readouterr().out


# --- Cloudflare e-mail placeholder and footnote back-links --------------------------------

def cf_encode(address, key=0x5A):
    """What Cloudflare does to an address: a key byte, then every character XOR-ed with it."""
    return "%02x" % key + "".join("%02x" % (ord(char) ^ key) for char in address)


def protected(address):
    return ('<a href="/cdn-cgi/l/email-protection" class="__cf_email__" data-cfemail="%s">'
            '[email&#160;protected]</a>' % cf_encode(address))


def test_decode_cfemail_round_trips():
    assert fs.decode_cfemail(cf_encode("press.office@example.org", key=0x11)) == "press.office@example.org"


def test_html_text_reveals_protected_addresses_and_drops_back_links():
    html = page(
        words(300),
        "For media inquiries, please email %s or call 202-555-0100." % protected("media@example.org"),
        '1. The Desk will publish a technical note. <a href="#t1">Return to text</a>')
    text = fs.html_text(fs.BeautifulSoup(html, "lxml"))
    assert "please email media@example.org or call" in text
    assert "protected" not in text
    assert "1. The Desk will publish a technical note." in text
    assert "Return to text" not in text


STATEMENT = BASE + "/newsevents/pressreleases/monetary20240131a.htm"


def stale_statement_row(text=None):
    text = text or words(400) + " For media inquiries, please email [email protected] or call 1."
    return ["2024-01-31", "scheduled", STATEMENT, "html", text, "old"]


def seed_statements(tmp_path, *rows):
    (tmp_path / "csv").mkdir(exist_ok=True)
    out = fs.CsvOutput(str(tmp_path / "csv" / "statements.csv"), fs.STATEMENT_COLUMNS)
    for row in rows:
        out.write(row)
    out.close()


def read_statements(tmp_path):
    with open(tmp_path / "csv" / "statements.csv", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def statement_meeting():
    return {"2024-01-31": fs.Meeting(date="2024-01-31", meeting_type="scheduled", statement_html=STATEMENT)}


def test_documents_stored_with_the_placeholder_are_downloaded_again(tmp_path, monkeypatch):
    seed_statements(tmp_path, stale_statement_row())
    fixed = page(words(400), "For media inquiries, please email %s or call 1." % protected("media@example.org"))
    assert scenario(tmp_path, monkeypatch, StubFetcher(pages={STATEMENT: fixed}), statement_meeting()) == 0
    rows = read_statements(tmp_path)
    assert len(rows) == 1                                   # replaced, not duplicated
    assert "media@example.org" in rows[0]["text"] and "protected" not in rows[0]["text"]
    assert rows[0]["fetched_at"] != "old"


def test_stale_document_is_kept_when_the_new_download_fails(tmp_path, monkeypatch, capsys):
    seed_statements(tmp_path, stale_statement_row())
    assert scenario(tmp_path, monkeypatch, StubFetcher(fail={STATEMENT}), statement_meeting()) == 1
    rows = read_statements(tmp_path)
    assert len(rows) == 1 and "[email protected]" in rows[0]["text"]   # nothing was lost
    assert "still contain the [email protected] placeholder" in capsys.readouterr().out


def test_an_undecodable_page_fails_the_run_instead_of_looping_silently(tmp_path, monkeypatch, capsys):
    seed_statements(tmp_path)
    pointless = page(words(400), "Please email [email&#160;protected] for details.")   # no data-cfemail
    assert scenario(tmp_path, monkeypatch, StubFetcher(pages={STATEMENT: pointless}), statement_meeting()) == 1
    assert "markup may have changed" in capsys.readouterr().out


def test_stored_data_is_untouched_when_the_fed_pages_cannot_be_read(tmp_path, monkeypatch):
    seed_statements(tmp_path, stale_statement_row())
    before = (tmp_path / "csv" / "statements.csv").read_bytes()
    assert scenario(tmp_path, monkeypatch, StubFetcher(), {}) == 1
    assert (tmp_path / "csv" / "statements.csv").read_bytes() == before


def test_only_requested_document_types_are_repaired(tmp_path, monkeypatch, capsys):
    seed_statements(tmp_path, stale_statement_row())
    before = (tmp_path / "csv" / "statements.csv").read_bytes()
    monkeypatch.setattr(fs, "Fetcher", lambda **kwargs: StubFetcher())
    monkeypatch.setattr(fs, "collect_meetings", lambda f, years, errors: statement_meeting())
    monkeypatch.setattr(sys, "argv", ["fomc_scraper.py", "--output-dir", str(tmp_path), "--delay", "0",
                                      "--documents", "minutes", "--no-export"])
    fs.main()
    assert (tmp_path / "csv" / "statements.csv").read_bytes() == before   # statements not asked for
    assert "downloading" not in capsys.readouterr().out                   # nor announced for download


# --- row order ----------------------------------------------------------------------------

def test_sort_csv_restores_date_order_and_leaves_sorted_files_alone(tmp_path):
    path = str(tmp_path / "minutes.csv")
    fs.write_csv_rows(path, [["meeting_date", "text"], ["2024-03-20", "b"], ["2024-01-31", "a"]])
    fs.sort_csv(path, ("meeting_date",))
    assert [r[0] for r in fs.read_csv_rows(path)[1:]] == ["2024-01-31", "2024-03-20"]
    stamp = os.stat(path).st_mtime_ns
    fs.sort_csv(path, ("meeting_date",))
    assert os.stat(path).st_mtime_ns == stamp               # already sorted: not rewritten


def test_retried_documents_end_up_in_date_order(tmp_path, monkeypatch):
    meetings, pages, urls = two_meetings()
    scenario(tmp_path, monkeypatch, StubFetcher(pages=pages, boom={urls["a"][1]}), meetings)
    scenario(tmp_path, monkeypatch, StubFetcher(pages=pages), meetings)
    with open(tmp_path / "csv" / "minutes.csv", newline="", encoding="utf-8") as handle:
        dates = [r["meeting_date"] for r in csv.DictReader(handle)]
    assert dates == ["2024-01-31", "2024-03-20"]
