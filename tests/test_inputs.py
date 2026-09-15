"""Reading files, URLs and exports into a thread (``-f``, ``-u``, ``-U``).

Two properties dominate this file:

1. **Nothing is silently lost or mangled.** Every extractor either keeps all
   the text or reports what it could not keep. A file no codec reads is an
   error, not a document full of replacement characters that would then be
   analyzed, quoted and hashed as though somebody wrote it.
2. **Author handles never reach a record.** Contract s8 refuses to characterize
   a person, and a run record is meant to be published. Handles live on the
   in-memory thread, where the local report can use them; the record gets
   ``a-01`` and counts.
"""

from __future__ import annotations

import json
import zipfile

import pytest

from abca.inputs.files import (
    ENCODINGS,
    MAX_FILE_BYTES,
    FileReadError,
    decode,
    docx_to_text,
    pdf_to_text,
    read_file,
)
from abca.inputs.records import (
    RecordFormatError,
    assert_has_text_field,
    posts_from_csv,
    posts_from_json,
)
from abca.inputs.select import InputError, resolve
from abca.inputs.threads import Post, Thread, normalize_handle
from abca.inputs.web import UrlReadError, posts_from_json_ld, read_url
from abca.providers.transport import TextResponse
from abca.schema.enums import InputKind

# --------------------------------------------------------------------------
# Threads
# --------------------------------------------------------------------------


class TestThread:
    def test_renders_posts_with_spans_that_locate_them(self):
        thread = Thread.build([
            Post(id="", text="First claim."),
            Post(id="", text="Second claim."),
        ])
        text, spans = thread.render()
        assert text == "First claim.\n\nSecond claim."
        assert [text[s.start:s.end] for s in spans] == ["First claim.", "Second claim."]

    def test_no_author_marker_is_written_into_the_document(self):
        """A ``[a-01]`` in the text would be hashed, segmented and extracted.

        The segmenter would eventually pull it into a claim, and the tool would
        be analyzing its own bookkeeping as though the author had written it.
        """
        thread = Thread.build([Post(id="", text="A claim.", author="@alice")])
        text, _ = thread.render()
        assert "a-01" not in text
        assert "alice" not in text
        assert text == "A claim."

    def test_pseudonyms_are_positional_and_stable(self):
        posts = [
            Post(id="", text="one", author="@Alice"),
            Post(id="", text="two", author="bob"),
            Post(id="", text="three", author="alice"),
        ]
        thread = Thread.build(posts)
        assert [p.author_pseudonym for p in thread.posts] == ["a-01", "a-02", "a-01"]
        # Same input, same pseudonyms -- otherwise two runs of one document
        # would produce two different records.
        assert Thread.build(posts).authors == thread.authors

    def test_pseudonyms_are_not_derived_from_the_handle(self):
        """A hash of a handle is reversible by anyone holding a handle list."""
        left = Thread.build([Post(id="", text="x", author="@someone_specific")])
        right = Thread.build([Post(id="", text="x", author="@entirely_different")])
        assert left.posts[0].author_pseudonym == right.posts[0].author_pseudonym == "a-01"

    def test_post_cap_truncates_visibly(self):
        thread = Thread.build(
            [Post(id="", text=f"claim {i}") for i in range(10)], max_posts=4
        )
        assert len(thread.posts) == 4
        assert thread.truncated
        assert any("NOT analyzed" in note for note in thread.notes)

    def test_by_author_filters_and_renumbers(self):
        thread = Thread.build([
            Post(id="", text="one", author="alice"),
            Post(id="", text="two", author="bob"),
            Post(id="", text="three", author="@Alice"),
        ])
        filtered = thread.by_author("@alice")
        assert [p.text for p in filtered.posts] == ["one", "three"]
        # Ids are reassigned because the filtered thread is a DIFFERENT
        # document; keeping the originals would leave spans pointing into text
        # that was never analyzed.
        assert [p.id for p in filtered.posts] == ["p-001", "p-002"]

    def test_handle_matching_is_literal(self):
        assert normalize_handle("@Alice ") == "alice"
        assert normalize_handle("alice") == normalize_handle("/alice")
        assert normalize_handle("alice1") != normalize_handle("alice")

    def test_a_bare_statement_is_a_thread_of_one(self):
        """``-t`` is not a second pipeline; it is the degenerate thread."""
        thread = Thread.single("A statement.")
        assert len(thread.posts) == 1
        assert thread.render()[0] == "A statement."


# --------------------------------------------------------------------------
# Exports
# --------------------------------------------------------------------------


class TestRecords:
    def test_reads_a_flat_json_array(self):
        payload = json.dumps([
            {"author": "@alice", "text": "First."},
            {"author": "bob", "text": "Second."},
        ])
        posts, title, mapping = posts_from_json(payload)
        assert [p.text for p in posts] == ["First.", "Second."]
        assert title is None
        assert "text=text" in mapping.describe()

    def test_reads_an_article_with_nested_replies(self):
        payload = json.dumps({
            "title": "Ballot access",
            "selftext": "Illinois requires 25,000 signatures.",
            "author": "op",
            "comments": [
                {"body": "No it does not.", "author": "@bob",
                 "replies": [{"body": "Yes, see 10 ILCS 5/10-2.", "author": "op"}]},
            ],
        })
        posts, title, _ = posts_from_json(payload)
        assert title == "Ballot access"
        assert len(posts) == 3
        assert [p.depth for p in posts] == [0, 0, 1]

    def test_fields_are_resolved_per_row_not_once(self):
        """An article carries ``selftext`` while its comments carry ``body``.

        A mapping fixed from the first record drops every comment -- silently,
        which is the part that matters.
        """
        payload = json.dumps({
            "selftext": "Lead post.", "author": "op",
            "comments": [{"body": "A reply.", "author": "bob"}],
        })
        posts, _, mapping = posts_from_json(payload)
        assert [p.text for p in posts] == ["Lead post.", "A reply."]
        assert mapping.text == {"selftext", "body"}

    def test_author_may_be_an_object(self):
        payload = json.dumps([{"text": "x", "author": {"@type": "Person", "name": "@bob"}}])
        posts, _, _ = posts_from_json(payload)
        assert posts[0].author == "@bob"

    def test_epoch_and_iso_timestamps_both_parse(self):
        payload = json.dumps([
            {"text": "a", "created_utc": 1735689600},
            {"text": "b", "created_at": "2026-01-02T10:00:00Z"},
        ])
        posts, _, _ = posts_from_json(payload)
        assert all(p.timestamp is not None for p in posts)

    def test_an_unparseable_timestamp_is_dropped_not_guessed(self):
        """A wrong timestamp would misorder a conversation and mislead ``-U``."""
        posts, _, _ = posts_from_json(json.dumps([{"text": "a", "date": "last Tuesday"}]))
        assert posts[0].timestamp is None

    def test_reads_csv_with_a_sniffed_dialect(self):
        posts, _, mapping = posts_from_csv(
            "user;comment\nalice;\"Needs 25,000 sigs\"\nbob;Wrong.\n"
        )
        assert [p.author for p in posts] == ["alice", "bob"]
        assert mapping.author == {"user"}

    def test_non_conversation_json_is_refused(self):
        with pytest.raises(RecordFormatError, match="not a conversation"):
            posts_from_json(json.dumps({"config": {"retries": 3}}))

    def test_the_refusal_names_the_fields_the_file_actually_had(self):
        """The only thing that tells someone how to fix it."""
        with pytest.raises(RecordFormatError, match="headline"):
            assert_has_text_field([{"headline": "x", "byline": "y"}])

    def test_nested_replies_satisfy_the_text_check(self):
        """A wrapper with no body of its own is still a valid conversation."""
        assert_has_text_field([{"id": 1, "replies": [{"body": "a claim"}]}])


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------


class TestFiles:
    def test_reads_plain_text(self, tmp_path):
        path = tmp_path / "statement.txt"
        path.write_text("Illinois requires 25,000 signatures.", encoding="utf-8")
        read = read_file(path)
        assert read.extractor == "text"
        assert read.encoding == "utf-8"
        assert read.thread.posts[0].text.startswith("Illinois")

    def test_reads_a_json_export_as_a_thread(self, tmp_path):
        path = tmp_path / "thread.json"
        path.write_text(json.dumps([
            {"author": "a", "text": "One."}, {"author": "b", "text": "Two."},
        ]), encoding="utf-8")
        read = read_file(path)
        assert read.extractor == "json"
        assert len(read.thread.posts) == 2
        assert read.thread.author_count == 2

    def test_decoding_is_strict_and_reports_the_codec(self):
        text, codec = decode("café".encode("cp1252"))
        assert text == "café"
        assert codec == "cp1252"

    def test_undecodable_bytes_are_an_error_not_replacement_characters(self):
        """Replacement characters would be analyzed as though somebody wrote them."""
        # 0x81/0x8d/0x8f/0x90/0x9d are undefined in cp1252 and invalid as a
        # UTF-8 lead byte, so no codec in the ladder accepts this.
        with pytest.raises(FileReadError, match="could not decode"):
            decode(b"\x81\x8d\x8f\x90\x9d")

    def test_utf16_is_only_tried_behind_a_byte_order_mark(self):
        """Without this, a four-byte cp1252 word decodes as two CJK characters.

        ``b"caf\\xe9"`` has an even length, so ``decode("utf-16")`` succeeds on
        it and returns mojibake -- no exception, no warning. That mojibake would
        then be segmented, quoted and hashed as though somebody wrote it, which
        is precisely the silent-mangling failure this module exists to prevent.
        """
        assert "utf-16" not in ENCODINGS
        assert decode("café".encode("cp1252")) == ("café", "cp1252")
        assert decode("café".encode("utf-16")) == ("café", "utf-16")

    def test_utf8_is_preferred_over_cp1252(self):
        """cp1252 decodes almost anything, so trying it early masks real UTF-8."""
        assert ENCODINGS[0] == "utf-8"
        assert ENCODINGS[-1] == "cp1252"
        assert decode("naïve".encode())[1] == "utf-8"

    def test_dispatch_is_by_extension_not_by_sniffing(self, tmp_path):
        """A .txt holding JSON stays prose.

        Sniffing would silently reparse it as a conversation and change which
        words get attributed to whom, with no sign to the person who named it.
        """
        path = tmp_path / "notes.txt"
        path.write_text(json.dumps([{"author": "a", "text": "One."}]), encoding="utf-8")
        read = read_file(path)
        assert read.extractor == "text"
        assert len(read.thread.posts) == 1

    def test_an_unknown_extension_is_read_as_text_and_says_so(self, tmp_path):
        path = tmp_path / "message.eml"
        path.write_text("A political claim.", encoding="utf-8")
        read = read_file(path)
        assert read.extractor == "text"
        assert any("read as plain text" in note for note in read.thread.notes)

    def test_an_empty_file_is_refused(self, tmp_path):
        path = tmp_path / "empty.txt"
        path.write_bytes(b"")
        with pytest.raises(FileReadError, match="empty"):
            read_file(path)

    def test_an_oversized_file_is_refused_not_truncated(self, tmp_path, monkeypatch):
        path = tmp_path / "big.txt"
        path.write_text("x" * 5000, encoding="utf-8")
        monkeypatch.setattr("abca.inputs.files.MAX_FILE_BYTES", 1000)
        with pytest.raises(FileReadError, match="Refused rather than truncated"):
            read_file(path)
        assert MAX_FILE_BYTES > 0  # the real cap is still a real number

    def test_a_directory_is_refused(self, tmp_path):
        with pytest.raises(FileReadError, match="directory"):
            read_file(tmp_path)

    def test_missing_file(self, tmp_path):
        with pytest.raises(FileReadError, match="no such file"):
            read_file(tmp_path / "nope.txt")


class TestDocx:
    """DOCX extraction is stdlib-only, so it is tested against real archives."""

    def _docx(self, path, body_xml: str):
        ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(
                "word/document.xml",
                f'<?xml version="1.0"?><w:document {ns}><w:body>{body_xml}</w:body></w:document>',
            )
        return path

    def _paragraph(self, *runs: str) -> str:
        return "<w:p>" + "".join(f"<w:r><w:t>{run}</w:t></w:r>" for run in runs) + "</w:p>"

    def test_extracts_paragraphs_in_order(self, tmp_path):
        path = self._docx(
            tmp_path / "doc.docx",
            self._paragraph("Illinois requires ", "25,000 signatures.")
            + self._paragraph("A full slate is also required."),
        )
        text, paragraphs = docx_to_text(path.read_bytes())
        assert text == (
            "Illinois requires 25,000 signatures.\n\nA full slate is also required."
        )
        assert paragraphs == 2

    def test_table_cell_text_is_not_dropped(self, tmp_path):
        """Text in a table is still text the author wrote."""
        path = self._docx(
            tmp_path / "table.docx",
            "<w:tbl><w:tr><w:tc>" + self._paragraph("25,000 or 1%") + "</w:tc></w:tr></w:tbl>",
        )
        text, _ = docx_to_text(path.read_bytes())
        assert "25,000 or 1%" in text

    def test_a_non_zip_is_refused_with_a_useful_message(self, tmp_path):
        path = tmp_path / "fake.docx"
        path.write_text("this is not a zip", encoding="utf-8")
        with pytest.raises(FileReadError, match="not a readable .docx"):
            read_file(path)

    def test_a_zip_without_a_document_part_is_refused(self, tmp_path):
        path = tmp_path / "wrong.docx"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("hello.txt", "nope")
        with pytest.raises(FileReadError, match="word/document.xml"):
            read_file(path)


# --------------------------------------------------------------------------
# URLs
# --------------------------------------------------------------------------


PAGE_WITH_LD = """<html><head><title>Ballot access rules</title>
<script type="application/ld+json">
{"@type":"NewsArticle","articleBody":"Illinois requires 25,000 signatures.",
 "author":{"@type":"Person","name":"Reporter"},
 "comment":[{"@type":"Comment","text":"That is not right.","author":{"name":"@bob"}},
            {"@type":"Comment","text":"It is, see 10 ILCS 5/10-2.","author":{"name":"alice"},
             "comment":[{"@type":"Comment","text":"Fair enough.","author":{"name":"@bob"}}]}]}
</script></head><body><p>rendered junk</p></body></html>"""

PLAIN_PAGE = "<html><head><title>An article</title></head><body><p>A claim about a law.</p></body></html>"


def scripted(*responses):
    """A transport factory returning canned responses in order."""
    queue = list(responses)

    class Scripted:
        def __init__(self, origin):
            self.origin = origin

        def get_text(self, path, **kwargs):
            item = queue.pop(0) if len(queue) > 1 else queue[0]
            if isinstance(item, str):
                return TextResponse(status=200, text=item, content_type="text/html")
            return item

        def close(self):
            return None

    return Scripted


class TestUrls:
    def test_json_ld_gives_authors_and_reply_depth(self):
        page = read_url("https://example.com/t", transport_factory=scripted(PAGE_WITH_LD))
        assert page.extractor == "json-ld"
        assert page.thread.title == "Ballot access rules"
        assert [p.depth for p in page.thread.posts] == [0, 1, 1, 2]
        assert page.thread.author_count == 3

    def test_json_ld_extraction_finds_nested_comments(self):
        posts = posts_from_json_ld(PAGE_WITH_LD)
        assert [p.author for p in posts] == ["Reporter", "@bob", "alice", "@bob"]

    def test_a_page_without_structured_data_is_one_post_and_says_so(self):
        """No heuristic comment hunting. Mis-attribution is the worse failure."""
        page = read_url("https://example.com/a", transport_factory=scripted(PLAIN_PAGE))
        assert page.extractor == "page"
        assert len(page.thread.posts) == 1
        assert any("WHOLE PAGE" in note for note in page.thread.notes)

    def test_malformed_json_ld_falls_back_rather_than_failing(self):
        html = (
            '<html><script type="application/ld+json">{ broken</script>'
            "<body><p>A claim.</p></body></html>"
        )
        page = read_url("https://example.com/b", transport_factory=scripted(html))
        assert page.extractor == "page"
        assert "A claim." in page.thread.posts[0].text

    def test_redirects_are_followed_and_recorded(self):
        page = read_url(
            "https://example.com/old",
            transport_factory=scripted(
                TextResponse(status=301, text="", headers={"location": "https://example.com/new"}),
                PLAIN_PAGE,
            ),
        )
        assert page.final_url == "https://example.com/new"
        assert page.redirects == ["https://example.com/old"]
        assert any("followed" in note for note in page.thread.notes)

    def test_a_cross_host_redirect_is_called_out(self):
        page = read_url(
            "https://example.com/x",
            transport_factory=scripted(
                TextResponse(status=302, text="", headers={"location": "https://elsewhere.test/y"}),
                PLAIN_PAGE,
            ),
        )
        assert any("crossed hosts" in note for note in page.thread.notes)

    def test_an_error_page_is_not_analyzed(self):
        with pytest.raises(UrlReadError, match="HTTP 404"):
            read_url(
                "https://example.com/missing",
                transport_factory=scripted(TextResponse(status=404, text="gone")),
            )

    def test_non_http_schemes_are_refused(self):
        with pytest.raises(UrlReadError, match="http or https"):
            read_url("file:///etc/passwd", transport_factory=scripted(PLAIN_PAGE))

    def test_a_json_endpoint_is_parsed_as_a_conversation(self):
        response = TextResponse(
            status=200,
            text=json.dumps([{"author": "a", "text": "One."}, {"author": "b", "text": "Two."}]),
            content_type="application/json",
        )
        page = read_url("https://example.com/api", transport_factory=scripted(response))
        assert page.extractor == "json"
        assert len(page.thread.posts) == 2


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------


class TestSelect:
    def test_exactly_one_input_is_required(self, tmp_path):
        with pytest.raises(InputError, match="no input"):
            resolve()
        path = tmp_path / "a.txt"
        path.write_text("x", encoding="utf-8")
        with pytest.raises(InputError, match="mutually exclusive"):
            resolve(text="a statement", file=path)

    def test_text_and_stdin_carry_their_own_kinds(self):
        assert resolve(text="a claim").kind is InputKind.TEXT
        assert resolve(stdin="a claim").kind is InputKind.STDIN

    def _export(self, tmp_path):
        path = tmp_path / "thread.json"
        path.write_text(json.dumps([
            {"author": "@alice", "text": "Illinois requires 25,000 signatures."},
            {"author": "bob", "text": "No it does not."},
            {"author": "alice", "text": "A full slate is also required."},
        ]), encoding="utf-8")
        return path

    def test_user_filters_an_export_to_one_account(self, tmp_path):
        resolved = resolve(user="@alice", source=self._export(tmp_path))
        assert resolved.kind is InputKind.USER
        assert len(resolved.thread.posts) == 2
        assert resolved.detail["source_posts"] == 3

    def test_user_without_from_explains_why_it_does_not_scrape(self, tmp_path):
        with pytest.raises(InputError, match="scraping"):
            resolve(user="@alice")

    def test_an_unknown_handle_is_an_error_not_an_empty_analysis(self, tmp_path):
        """Empty output would read as 'this account made no claims'."""
        with pytest.raises(InputError, match="no posts by"):
            resolve(user="@nobody", source=self._export(tmp_path))

    def test_user_needs_author_information_in_the_source(self, tmp_path):
        path = tmp_path / "plain.json"
        path.write_text(json.dumps([{"text": "A claim."}]), encoding="utf-8")
        with pytest.raises(InputError, match="no author information"):
            resolve(user="@alice", source=path)

    def test_a_non_utf8_file_is_flagged_in_the_notes(self, tmp_path):
        path = tmp_path / "legacy.txt"
        path.write_bytes("Le café requires 25,000 signatures.".encode("cp1252"))
        resolved = resolve(file=path)
        assert resolved.detail["encoding"] == "cp1252"
        assert any("not UTF-8" in note for note in resolved.thread.notes)


class TestPdf:
    """PDF reading is an optional extra, and its lossiness is reported."""

    def test_missing_library_is_refused_with_the_install_command(self, tmp_path, monkeypatch):
        """Half-reading a PDF is worse than refusing one.

        A stub extractor would return empty or partial text, and an analysis of
        partial text reports that a document makes no claims -- which is not the
        same finding as looking and finding none.
        """
        import builtins

        real_import = builtins.__import__

        def blocked(name, *args, **kwargs):
            if name == "pypdf":
                raise ImportError("no pypdf")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", blocked)
        with pytest.raises(FileReadError, match=r"abca\[pdf\]"):
            pdf_to_text(b"%PDF-1.4 not really")

    def test_reads_a_real_pdf_and_flags_the_extraction(self, tmp_path):
        pdfgen = pytest.importorskip(
            "reportlab.pdfgen.canvas", reason="needs reportlab to build a fixture PDF"
        )
        pytest.importorskip("pypdf", reason="needs the abca[pdf] extra")

        path = tmp_path / "brief.pdf"
        canvas = pdfgen.Canvas(str(path))
        canvas.drawString(72, 720, "Illinois requires 25,000 signatures.")
        canvas.showPage()
        canvas.drawString(72, 720, "A complete slate is also required.")
        canvas.save()

        read = read_file(path)
        assert read.extractor == "pdf"
        assert read.detail["pages"] == 2
        assert "25,000 signatures" in read.thread.posts[0].text
        # The lossiness caveat leads the notes, so it reaches the run record.
        assert read.thread.notes[0].startswith("PDF TEXT EXTRACTION IS APPROXIMATE")

    def test_a_scanned_pdf_with_no_text_layer_is_refused(self, tmp_path):
        pytest.importorskip("pypdf", reason="needs the abca[pdf] extra")
        pdfgen = pytest.importorskip("reportlab.pdfgen.canvas")

        path = tmp_path / "scan.pdf"
        canvas = pdfgen.Canvas(str(path))
        canvas.showPage()  # a page with no text at all
        canvas.save()

        with pytest.raises(FileReadError, match="no extractable text layer"):
            read_file(path)


# --------------------------------------------------------------------------
# The command surface
# --------------------------------------------------------------------------


class TestAnalyzeFlags:
    """Argument handling for ``-f``, ``-u``, ``-U`` and ``--private``.

    These reach the input layer and stop before any model is built, so they
    need no backend -- which is the point: a bad flag should fail before a
    hosted provider is billed for anything.
    """

    def _runner(self):
        from typer.testing import CliRunner

        return CliRunner()

    def _invoke(self, *args):
        from abca.cli.main import app

        return self._runner().invoke(app, ["analyze", *args])

    def test_two_inputs_are_refused(self, tmp_path):
        path = tmp_path / "a.txt"
        path.write_text("a claim", encoding="utf-8")
        result = self._invoke("-t", "a claim", "-f", str(path))
        assert result.exit_code == 2
        assert "mutually exclusive" in result.output

    def test_no_input_lists_every_way_in(self):
        result = self._invoke("--profile", "fast")
        assert result.exit_code == 2
        for flag in ("-t", "-f", "-u", "-U", "--stdin"):
            assert flag in result.output

    def test_a_missing_file_fails_before_any_backend_is_built(self, tmp_path):
        result = self._invoke("-f", str(tmp_path / "nope.txt"))
        assert result.exit_code == 2
        assert "no such file" in result.output

    def test_user_without_from_explains_the_refusal_to_scrape(self):
        result = self._invoke("-U", "@someone")
        assert result.exit_code == 2
        assert "scraping" in result.output

    def test_private_requires_the_second_flag(self, tmp_path):
        """Contract s8. Same shape as --no-red-team: one flag is not enough."""
        path = tmp_path / "a.txt"
        path.write_text("a claim", encoding="utf-8")
        result = self._invoke("-f", str(path), "--private")
        assert result.exit_code == 2
        assert "--i-know" in result.output

    def test_the_help_documents_every_input(self):
        from abca.cli.main import app

        result = self._runner().invoke(app, ["analyze", "--help"])
        assert result.exit_code == 0
        for fragment in ("--file", "--url", "--user", "--from", "--max-posts",
                         "--no-cluster", "--private"):
            assert fragment in result.output

    def test_no_red_team_needs_i_know_before_any_backend_is_touched(self, tmp_path):
        """It used to sit after the health check.

        Somebody with no model running was told their backend was unreachable
        when what they actually needed was a second flag.
        """
        path = tmp_path / "a.txt"
        path.write_text("a claim", encoding="utf-8")
        result = self._invoke("-f", str(path), "--no-red-team")
        assert result.exit_code == 2
        assert "--i-know" in result.output
