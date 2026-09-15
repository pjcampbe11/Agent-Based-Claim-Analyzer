"""Reading a file into a thread (``-f``).

WHAT THIS MODULE OWES THE REST OF THE PIPELINE
==============================================
One string, plus an honest account of how it was obtained. Everything
downstream indexes that string: sentence offsets, claim spans, the content
hash, the ledger. So the two failure modes that matter are the two this module
is built around.

**Never silently lose text.** A dropped paragraph produces an analysis of a
document nobody wrote, and nothing downstream can detect it. Every extractor
here is either lossless (plain text, DOCX) or reports its lossiness in a note
that reaches the run record (PDF).

**Never silently mangle text.** Decoding with ``errors="replace"`` would turn a
mis-encoded file into confident nonsense. Encodings are tried in order and the
one used is REPORTED, so a document read as cp1252 says so in the record.

DEPENDENCIES, AND WHY DOCX HAS NONE
===================================
A ``.docx`` is a zip containing XML. ``zipfile`` and
``xml.etree.ElementTree`` are stdlib, the extraction is one screen of code, and
a reviewer can check it -- which matters more here than in most projects,
because a run record's package list is part of its reproducibility story and
every dependency added is one more thing that can differ between the machine
that produced a verdict and the machine checking it.

PDF is the exception. Its text layer is a positioned glyph soup with no
paragraph structure, and there is no honest hundred-line version. So it is an
OPTIONAL extra: absent the library, the file is refused with the install
command, rather than half-read.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

from abca.inputs.records import RecordFormatError, posts_from_csv, posts_from_json
from abca.inputs.threads import DEFAULT_MAX_POSTS, Post, Thread

#: Largest file read, in bytes. Refused rather than truncated: a truncated
#: statute or thread produces an analysis of part of a document while claiming
#: to be an analysis of the document.
MAX_FILE_BYTES = 20 * 1024 * 1024

#: Largest extracted text, in characters. Same reasoning.
MAX_TEXT_CHARS = 2_000_000

#: Encodings tried in order. UTF-8 first because everything modern is UTF-8;
#: cp1252 last because it decodes almost any byte sequence into *something*,
#: so trying it earlier would mask a real UTF-8 file with a stray byte.
#:
#: UTF-16 is NOT in this ladder, and that is a bug fix rather than an omission.
#: ``b"caf\xe9"`` is four bytes, so ``bytes.decode("utf-16")`` succeeds on it and
#: returns two unrelated CJK characters -- no exception, no warning, and the
#: mojibake would then be segmented, quoted and hashed as though somebody had
#: written it. UTF-16 is tried only when a byte-order mark says so; see
#: :func:`decode`.
ENCODINGS: tuple[str, ...] = ("utf-8", "utf-8-sig", "cp1252")

#: Byte-order marks that identify a UTF-16 file unambiguously.
_UTF16_BOMS: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xfe\x00\x00", "utf-32"),
    (b"\x00\x00\xfe\xff", "utf-32"),
    (b"\xff\xfe", "utf-16"),
    (b"\xfe\xff", "utf-16"),
)

#: Extensions this build reads, mapped to how they are handled.
TEXT_SUFFIXES = frozenset({".txt", ".md", ".markdown", ".text", ".rst", ".log"})
RECORD_SUFFIXES = frozenset({".json", ".csv", ".tsv"})
DOCX_SUFFIX = ".docx"
PDF_SUFFIX = ".pdf"

SUPPORTED_SUFFIXES = tuple(sorted(
    TEXT_SUFFIXES | RECORD_SUFFIXES | {DOCX_SUFFIX, PDF_SUFFIX}
))

_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class FileReadError(ValueError):
    """A file could not be read into something analyzable."""


@dataclass(slots=True)
class ReadFile:
    """A file, read. ``thread`` is what the pipeline analyzes."""

    thread: Thread
    #: How the text was obtained: ``text``, ``json``, ``csv``, ``docx``, ``pdf``.
    extractor: str = "text"
    #: Codec that decoded it, when it was decoded as text at all.
    encoding: str | None = None
    #: Extractor-specific provenance, recorded in the ingest stage output.
    detail: dict[str, str | int] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Decoding
# --------------------------------------------------------------------------


def decode(raw: bytes) -> tuple[str, str]:
    """Decode bytes, returning ``(text, codec)``.

    Strict on every attempt. A file that no listed codec reads cleanly is an
    error, not a document full of replacement characters -- the replacement
    character version would be analyzed, quoted, and hashed as though it were
    what somebody wrote.
    """
    # A byte-order mark is the only trustworthy evidence of UTF-16/32, so it is
    # checked first and the ladder never guesses at them afterwards.
    for bom, codec in _UTF16_BOMS:
        if raw.startswith(bom):
            try:
                return raw.decode(codec), codec
            except UnicodeDecodeError as exc:
                raise FileReadError(
                    f"this file starts with a {codec} byte-order mark but does not "
                    f"decode as {codec}: {exc}"
                ) from exc

    for codec in ENCODINGS:
        try:
            return raw.decode(codec), codec
        except (UnicodeDecodeError, LookupError):
            continue
    raise FileReadError(
        f"could not decode this file as any of {', '.join(ENCODINGS)}. If it is "
        "text, convert it to UTF-8 first; if it is a binary format, this build "
        f"reads: {', '.join(SUPPORTED_SUFFIXES)}."
    )


# --------------------------------------------------------------------------
# DOCX -- stdlib only
# --------------------------------------------------------------------------


def docx_to_text(raw: bytes) -> tuple[str, int]:
    """Extract text from a ``.docx``. Returns ``(text, paragraph_count)``.

    Walks the document body in order and emits every ``w:t`` run, breaking a
    line at each paragraph, tab and explicit break. Walking the whole tree
    rather than matching known containers is deliberate: text inside a table
    cell, a text box or a content control is still text the author wrote, and a
    reader of the analysis would never learn it had been skipped.

    Headers, footers and footnotes live in separate parts and are NOT included.
    They are page furniture rather than argument, and pulling them in would
    interleave a running header into the middle of the prose.
    """
    try:
        with zipfile.ZipFile(_as_stream(raw)) as archive:
            names = set(archive.namelist())
            if "word/document.xml" not in names:
                raise FileReadError(
                    "this .docx has no word/document.xml. It may be a .doc renamed, "
                    "or a different Office format; re-save it as .docx."
                )
            body_xml = archive.read("word/document.xml")
    except zipfile.BadZipFile as exc:
        raise FileReadError(
            "this file is not a readable .docx (a .docx is a zip archive). "
            "An older .doc must be re-saved as .docx."
        ) from exc

    try:
        root = ElementTree.fromstring(body_xml)
    except ElementTree.ParseError as exc:
        raise FileReadError(f"the .docx document body is not valid XML: {exc}") from exc

    lines: list[str] = []
    current: list[str] = []
    paragraphs = 0

    def flush() -> None:
        nonlocal paragraphs
        joined = "".join(current).strip()
        current.clear()
        if joined:
            lines.append(joined)
            paragraphs += 1

    for element in root.iter():
        tag = element.tag
        if tag == f"{_W_NS}t":
            current.append(element.text or "")
        elif tag == f"{_W_NS}tab":
            current.append("\t")
        elif tag in (f"{_W_NS}br", f"{_W_NS}cr"):
            current.append("\n")
        elif tag == f"{_W_NS}p":
            # `iter()` yields a paragraph BEFORE its runs, so this flushes the
            # PREVIOUS paragraph. The trailing flush below closes the last one.
            flush()
    flush()

    return "\n\n".join(lines), paragraphs


def _as_stream(raw: bytes):
    import io

    return io.BytesIO(raw)


# --------------------------------------------------------------------------
# PDF -- optional extra
# --------------------------------------------------------------------------

#: Attached to every PDF-derived run. Extraction from a PDF is genuinely lossy,
#: and a reader comparing a claim against the page deserves to know why the
#: words may sit differently.
PDF_LOSSINESS_NOTE = (
    "PDF TEXT EXTRACTION IS APPROXIMATE. A PDF stores positioned glyphs, not "
    "paragraphs, so column order, hyphenation, headers, footers and table layout "
    "may differ from what the page looks like. Claim spans below index the "
    "EXTRACTED text, not the page. For a legal document, prefer the publisher's "
    "HTML or plain-text edition where one exists."
)


def _other_suffixes() -> str:
    """Every supported extension except PDF, for the missing-extra message."""
    return ", ".join(suffix for suffix in SUPPORTED_SUFFIXES if suffix != PDF_SUFFIX)


def pdf_to_text(raw: bytes) -> tuple[str, int]:
    """Extract text from a PDF. Returns ``(text, page_count)``.

    Requires the optional ``pdf`` extra. Refused rather than approximated when
    it is missing: a PDF read by a stub would produce empty or partial text,
    and an analysis of partial text is worse than a clear failure.
    """
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise FileReadError(
            "reading PDFs needs an extra that is not installed:\n"
            "    pip install 'abca[pdf]'\n"
            "PDF text is a positioned glyph soup with no paragraph structure, so "
            "there is no honest stdlib-only version. Every other supported format "
             f"({_other_suffixes()}) needs nothing extra."
        ) from exc

    try:
        reader = PdfReader(_as_stream(raw))
        if reader.is_encrypted:
            # An empty password opens many "protected" PDFs; a real one is a
            # refusal, not a prompt -- this tool never asks for a credential.
            try:
                reader.decrypt("")
            except Exception as exc:
                raise FileReadError(
                    "this PDF is encrypted and could not be opened. Remove the "
                    "protection and try again."
                ) from exc
        pages = [page.extract_text() or "" for page in reader.pages]
    except FileReadError:
        raise
    except Exception as exc:
        raise FileReadError(f"could not read this PDF: {type(exc).__name__}: {exc}") from exc

    text = "\n\n".join(page.strip() for page in pages if page.strip())
    if not text.strip():
        raise FileReadError(
            "this PDF has no extractable text layer -- it is most likely a scan. "
            "Run OCR on it first, or supply the text with -f on a .txt file. "
            "Analyzing an empty extraction would report that the document makes "
            "no claims, which is not the same as finding none."
        )
    return text, len(reader.pages)


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------


def read_file(path: Path | str, *, max_posts: int = DEFAULT_MAX_POSTS) -> ReadFile:
    """Read ``path`` into a :class:`~abca.inputs.threads.Thread`.

    Dispatch is by EXTENSION, not by sniffing content. Sniffing would mean a
    ``.txt`` containing a JSON array is silently reparsed as a conversation,
    changing which words get attributed to whom -- and the person who named the
    file would have no way to see that it happened.
    """
    resolved = Path(path)
    if not resolved.exists():
        raise FileReadError(f"no such file: {resolved}")
    if resolved.is_dir():
        raise FileReadError(f"{resolved} is a directory. Point -f at a single file.")

    size = resolved.stat().st_size
    if size == 0:
        raise FileReadError(f"{resolved} is empty.")
    if size > MAX_FILE_BYTES:
        raise FileReadError(
            f"{resolved} is {size / 1_048_576:.1f} MB; the limit is "
            f"{MAX_FILE_BYTES // 1_048_576} MB. Refused rather than truncated: an "
            "analysis of the first part of a document, presented as an analysis of "
            "the document, is the kind of quiet error this tool exists to avoid."
        )

    raw = resolved.read_bytes()
    suffix = resolved.suffix.lower()

    if suffix == DOCX_SUFFIX:
        text, paragraphs = docx_to_text(raw)
        return _as_text_thread(
            text, resolved, extractor="docx", encoding=None,
            detail={"paragraphs": paragraphs},
        )

    if suffix == PDF_SUFFIX:
        text, pages = pdf_to_text(raw)
        result = _as_text_thread(
            text, resolved, extractor="pdf", encoding=None, detail={"pages": pages},
        )
        result.thread.notes.insert(0, PDF_LOSSINESS_NOTE)
        return result

    decoded, codec = decode(raw)
    _check_length(decoded, resolved)

    if suffix in RECORD_SUFFIXES:
        return _as_record_thread(decoded, resolved, codec, max_posts=max_posts)

    if suffix not in TEXT_SUFFIXES:
        # Read it as text, and SAY SO. Refusing an unknown extension outright
        # would reject a perfectly readable ``.eml`` or an extensionless note;
        # reading it silently would hide that a structured file was flattened.
        result = _as_text_thread(decoded, resolved, extractor="text", encoding=codec)
        result.thread.notes.append(
            f"{resolved.suffix or '<no extension>'} is not a format this build "
             "understands, so the file was read as plain text. If it is a "
            "conversation export, rename it to .json or .csv so its authors and "
            "replies are parsed."
        )
        return result

    return _as_text_thread(decoded, resolved, extractor="text", encoding=codec)


def _check_length(text: str, path: Path) -> None:
    if len(text) > MAX_TEXT_CHARS:
        raise FileReadError(
            f"{path} holds {len(text):,} characters of text; the limit is "
            f"{MAX_TEXT_CHARS:,}. Split it, or analyze the section you mean."
        )


def _as_text_thread(
    text: str,
    path: Path,
    *,
    extractor: str,
    encoding: str | None,
    detail: dict[str, str | int] | None = None,
) -> ReadFile:
    """Wrap extracted prose as a single-post thread."""
    if not text.strip():
        raise FileReadError(f"{path} contained no text after extraction.")
    _check_length(text, path)
    return ReadFile(
        thread=Thread.build([Post(id="", text=text)], locator=str(path)),
        extractor=extractor,
        encoding=encoding,
        detail=detail or {},
    )


def _as_record_thread(
    text: str, path: Path, codec: str, *, max_posts: int
) -> ReadFile:
    """Parse a JSON or CSV conversation export."""
    parser = posts_from_json if path.suffix.lower() == ".json" else posts_from_csv
    try:
        posts, title, mapping = parser(text)
    except RecordFormatError as exc:
        raise FileReadError(f"{path}: {exc}") from exc

    if not posts:
        raise FileReadError(
            f"{path} parsed cleanly but produced no posts with text in them."
        )

    thread = Thread.build(
        posts,
        title=title,
        locator=str(path),
        notes=[
            (
                f"parsed {len(posts)} post(s) from {path.suffix.lstrip('.')}; "
                f"fields used: {mapping.describe()}"
            )
        ],
        max_posts=max_posts,
    )
    return ReadFile(
        thread=thread,
        extractor=path.suffix.lstrip(".").lower(),
        encoding=codec,
        detail={"posts": len(thread.posts), "fields": mapping.describe()},
    )


__all__ = [
    "ENCODINGS",
    "MAX_FILE_BYTES",
    "MAX_TEXT_CHARS",
    "PDF_LOSSINESS_NOTE",
    "SUPPORTED_SUFFIXES",
    "FileReadError",
    "ReadFile",
    "decode",
    "docx_to_text",
    "pdf_to_text",
    "read_file",
]
