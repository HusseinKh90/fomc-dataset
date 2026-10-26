"""Shared builders for the offline tests: stub fetcher, tiny HTML pages and a real PDF."""
import types

import requests
from bs4 import BeautifulSoup


def page(*paragraphs, extra=""):
    body = "".join("<p>%s</p>" % p for p in paragraphs)
    return '<html><body><div id="article">%s%s</div></body></html>' % (body, extra)


def words(n):
    """About n characters of plain text, enough to clear the validation floors."""
    return ("lorem ipsum " * (n // 12 + 1))[:n].strip()


class StubFetcher:
    """Serves canned pages and files; anything in `fail` raises like a dropped connection."""

    def __init__(self, pages=None, files=None, fail=(), boom=()):
        self.pages = pages or {}
        self.files = files or {}
        self.fail = set(fail)
        self.boom = set(boom)  # raises a non-network error, like a parser bug
        self.calls = []

    def _check(self, url):
        self.calls.append(url)
        if url in self.fail:
            raise requests.ConnectionError("simulated outage for %s" % url)
        if url in self.boom:
            raise ValueError("simulated parser failure for %s" % url)

    def soup(self, url):
        self._check(url)
        return BeautifulSoup(self.pages[url], "lxml")

    def get(self, url):
        self._check(url)
        return types.SimpleNamespace(content=self.files[url])


def _escape(line):
    return line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(pages):
    """Minimal text PDF: `pages` is a list of line lists, drawn top to bottom on each page."""
    n = len(pages)
    kids = " ".join("%d 0 R" % (4 + 2 * i) for i in range(n))
    objects = ["<< /Type /Catalog /Pages 2 0 R >>",
               "<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, n),
               "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    for i, lines in enumerate(pages):
        objects.append("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents %d 0 R "
                       "/Resources << /Font << /F1 3 0 R >> >> >>" % (5 + 2 * i))
        stream = "BT /F1 11 Tf 16 TL 60 740 Td " + " ".join(
            "(%s) Tj T*" % _escape(line) for line in lines) + " ET"
        objects.append("<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += ("%d 0 obj\n%s\nendobj\n" % (number, body)).encode("latin-1")
    xref = len(out)
    out += ("xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)).encode()
    out += "".join("%010d 00000 n \n" % offset for offset in offsets).encode()
    out += ("trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objects) + 1, xref)).encode()
    return out
