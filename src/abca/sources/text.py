"""HTML to plain text, for statute and opinion pages.

WHY NOT BeautifulSoup / readability
===================================
The pages this handles are not modern web apps. ILGA's statute pages are
HTML 4.01 tables of ``<code><font>`` blocks -- and that is representative of
primary-legal-source publishing generally, which runs a decade or two behind.
A focused converter handles them, is auditable in one screen, and keeps the
dependency list short enough that a run record's package versions stay
meaningful.

WHAT MATTERS HERE, AND WHY
==========================
The extracted text becomes the thing quotes are verified against and the thing
whose hash detects drift. Two consequences shape every choice below:

* **Never drop content.** A dropped clause would make a correct quote fail
  verification, and the adjudicator would be pushed toward citing nothing --
  the opposite of what the evidence gate exists to produce.
* **Never invent content.** Entities are unescaped, but nothing is
  paraphrased, summarized, or reordered.

Whitespace inside a line is collapsed because HTML rendering collapses it
anyway, so preserving source indentation would make the text disagree with
what a human reading the page actually sees.
"""

from __future__ import annotations

import html
import re

#: Elements whose contents are never document text.
_DROP_ELEMENTS = re.compile(
    r"(?is)<(script|style|noscript|head|nav|footer)\b.*?</\1\s*>"
)

#: Elements that imply a line break when removed.
_LINE_BREAKS = re.compile(r"(?i)<(br|tr|li|h[1-6])\s*/?>")

#: Elements that imply a paragraph break.
_PARAGRAPH_BREAKS = re.compile(r"(?i)</?(p|div|table|blockquote|section)\b[^>]*>")

_TAG = re.compile(r"<[^>]+>")
_TITLE = re.compile(r"(?is)<title>(.*?)</title>")

#: Horizontal whitespace, including the non-breaking and typographic spaces
#: these pages are full of. Written as escapes rather than literals so the
#: source stays readable and diffable -- an invisible character in a character
#: class is a bug nobody can see.
_HORIZONTAL_SPACE = re.compile("[ \t\u00a0\u2000-\u200a\u202f\u205f\u3000]+")


def extract_title(raw_html: str) -> str | None:
    """Return the ``<title>`` text, unescaped, or ``None``."""
    match = _TITLE.search(raw_html)
    if not match:
        return None
    title = html.unescape(_TAG.sub("", match.group(1))).strip()
    return title or None


def html_to_text(raw_html: str) -> str:
    """Convert an HTML document to plain text.

    Ordering matters: block-level markers are turned into newlines BEFORE tags
    are stripped, otherwise every paragraph boundary is lost and the statute
    arrives as one unreadable run.
    """
    # Line endings first. ILGA serves CRLF, and a stray \r survives every
    # later step -- ending up inside stored citation quotes and, worse, inside
    # the content hash, which would then differ between a run that fetched over
    # one platform's HTTP stack and one that fetched over another's.
    text = raw_html.replace("\r\n", "\n").replace("\r", "\n")

    text = _DROP_ELEMENTS.sub(" ", text)
    text = _LINE_BREAKS.sub("\n", text)
    text = _PARAGRAPH_BREAKS.sub("\n\n", text)
    text = _TAG.sub("", text)
    text = html.unescape(text)

    text = _HORIZONTAL_SPACE.sub(" ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def collapse(text: str) -> str:
    """Collapse ALL whitespace to single spaces. For search indexing only."""
    return " ".join(text.split())


__all__ = ["collapse", "extract_title", "html_to_text"]
