"""Files, URLs, threads and clustering: getting real input in without lying about it.

Four inputs land here, and each has its own way of quietly going wrong:

* a **file** can be mis-decoded, half-extracted, or read as the wrong format;
* a **URL** can be a page whose comments nobody can reliably identify;
* a **thread** carries authors, and a published record naming who said what
  would be the per-person analysis contract s8 refuses;
* **clustering** makes a large thread affordable by publishing one claim's
  verdict against another claim's words.

Usage::

    python scripts/demo_step7.py

No GPU and no network: the statute is seeded into a temporary cache and every
model call is served by scripts/mock_ollama.py.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from abca.inputs.files import decode, read_file
from abca.inputs.select import InputError, resolve
from abca.inputs.threads import Post, Thread
from abca.inputs.web import read_url
from abca.pipeline.cluster import run_cluster, similarity
from abca.pipeline.models import DraftClaim
from abca.providers.transport import TextResponse
from abca.schema.enums import ClaimType
from abca.sources.base import build_document
from abca.sources.cache import SourceCache
from abca.sources.ilcs import ILCSConnector

STATUTE = """(10 ILCS 5/10-2)

Sec. 10-2.
Any group of persons hereafter desiring to form a new political party
throughout the State shall file a petition signed by 1% of the number
of voters who voted at the next preceding Statewide general election or
25,000 qualified voters, whichever is less, and shall at the time of
filing contain a complete list of candidates of such party for all
offices to be filled.
"""

MARKER = "ballot access working group"

THREAD_EXPORT = [
    {"author": "@alice", "created_at": "2026-02-01T09:00:00Z",
     "text": f"Notes from the {MARKER}: Illinois makes you get 25,000 signatures "
             "to start a new party."},
    {"author": "bob", "created_at": "2026-02-01T09:04:00Z",
     "text": "Right — Illinois needs 25,000 signatures to start a new party, "
             "under 10 ILCS 5/10-2."},
    {"author": "@carol", "created_at": "2026-02-01T09:11:00Z",
     "text": "And the petition has to name a candidate for every office up "
             "that cycle."},
    {"author": "@dave", "created_at": "2026-02-01T09:15:00Z",
     "text": "Illinois makes you get 25,000 signatures to start a new party."},
]

PAGE = """<html><head><title>Ballot access, explained</title>
<script type="application/ld+json">
{"@type":"NewsArticle","headline":"Ballot access, explained",
 "articleBody":"Illinois requires 25,000 signatures to form a new party.",
 "author":{"@type":"Person","name":"Reporter"},
 "comment":[{"@type":"Comment","text":"That number is a ceiling, not a floor.",
             "author":{"name":"@bob"}},
            {"@type":"Comment","text":"The full slate is the harder part.",
             "author":{"name":"alice"},
             "comment":[{"@type":"Comment","text":"Agreed.",
                         "author":{"name":"@bob"}}]}]}
</script></head><body><p>nav junk, ads, a cookie banner</p></body></html>"""


def rule(title: str) -> None:
    print(f"\n{'=' * 4} {title} {'=' * max(0, 68 - len(title))}")


def scripted(html: str):
    class Scripted:
        def __init__(self, origin):
            self.origin = origin

        def get_text(self, path, **kwargs):
            return TextResponse(status=200, text=html, content_type="text/html")

        def close(self):
            return None

    return Scripted


def make_docx(path: Path) -> Path:
    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    def paragraph(text: str) -> str:
        return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"
    body = (
        paragraph("Illinois requires 25,000 signatures to form a new party.")
        + "<w:tbl><w:tr><w:tc>"
        + paragraph("Threshold: 1% or 25,000, whichever is less")
        + "</w:tc></w:tr></w:tbl>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "word/document.xml",
            f'<?xml version="1.0"?><w:document {ns}><w:body>{body}</w:body></w:document>',
        )
    return path


def main() -> int:
    workspace = Path(tempfile.mkdtemp(prefix="abca-demo7-"))

    # ------------------------------------------------------------------
    rule("1. Files: five formats, one thread")
    (workspace / "statement.txt").write_text(
        "Illinois requires 25,000 signatures.", encoding="utf-8")
    (workspace / "thread.json").write_text(json.dumps(THREAD_EXPORT), encoding="utf-8")
    (workspace / "thread.csv").write_text(
        "user,comment\nalice,\"Needs 25,000 signatures\"\nbob,\"And a full slate\"\n",
        encoding="utf-8")
    make_docx(workspace / "brief.docx")
    (workspace / "legacy.txt").write_bytes(
        "Le café requires 25,000 signatures.".encode("cp1252"))

    for name in ("statement.txt", "thread.json", "thread.csv", "brief.docx", "legacy.txt"):
        read = read_file(workspace / name)
        print(f"  {name:16} extractor={read.extractor:5} "
              f"encoding={read.encoding or '-':8} posts={len(read.thread.posts)} "
              f"authors={read.thread.author_count}")
    print("\n  Dispatch is by EXTENSION, never by sniffing content. A .txt holding")
    print("  JSON stays prose: reparsing it silently would change which words get")
    print("  attributed to whom, with no sign to the person who named the file.")

    rule("2. Decoding is strict, and says what it used")
    for label, raw in (
        ("utf-8", "naïve".encode()),
        ("cp1252", "café".encode("cp1252")),
        ("utf-16 (BOM)", "café".encode("utf-16")),
    ):
        text, codec = decode(raw)
        print(f"  {label:14} -> {text!r} via {codec}")
    try:
        decode(b"\x81\x8d\x8f\x90\x9d")
    except Exception as exc:
        print(f"  undecodable    -> refused: {str(exc).splitlines()[0][:58]}...")
    print("\n  UTF-16 is tried ONLY behind a byte-order mark. Without that rule")
    print("  b'caf\\xe9' decodes as two unrelated CJK characters with no error, and")
    print("  the mojibake gets segmented, quoted and hashed as though someone")
    print("  wrote it. Replacement characters are never used on an input file.")

    # ------------------------------------------------------------------
    rule("3. Authors: pseudonymized before anything is recorded")
    thread = Thread.build([
        Post(id="", text=row["text"], author=row["author"]) for row in THREAD_EXPORT
    ], locator="thread.json")
    for post in thread.posts:
        print(f"  {post.id}  local: {post.display_author:8} "
              f"record: {post.author_pseudonym}")
    text, spans = thread.render()
    print(f"\n  rendered document: {len(text)} chars, {len(spans)} spans")
    print(f"  markers in the text? {'a-01' in text or 'alice' in text}")
    print("\n  Positional stand-ins, not hashes: a hash of a handle is reversible")
    print("  by anyone holding a list of handles, which for a public platform is")
    print("  everyone. 'a-03' leaks the order somebody commented in, and nothing")
    print("  else. Real handles stay in memory for THIS terminal's report.")

    # ------------------------------------------------------------------
    rule("4. A URL: structured data, or the whole page and say so")
    page = read_url("https://example.test/article", transport_factory=scripted(PAGE))
    print(f"  extractor : {page.extractor}")
    print(f"  title     : {page.thread.title}")
    for post in page.thread.posts:
        print(f"    {post.id} depth={post.depth} {post.display_author:9} "
              f"{post.text[:44]}")
    plain = read_url(
        "https://example.test/plain",
        transport_factory=scripted("<html><body><p>A claim about a law.</p></body></html>"),
    )
    print(f"\n  a page with no JSON-LD -> extractor={plain.extractor}, "
          f"{len(plain.thread.posts)} post")
    print(f"  {plain.thread.notes[0][:72]}...")
    print("\n  There is deliberately no third strategy that hunts for comment")
    print("  containers by class name. Heuristic scraping mis-attributes, and for")
    print("  a tool whose value is that claims trace to what somebody actually")
    print("  wrote, 'roughly the right comments' is not a usable standard.")

    # ------------------------------------------------------------------
    rule("5. -U filters posts you already have")
    resolved = resolve(user="@alice", source=workspace / "thread.json")
    print(f"  selected {resolved.detail['selected_posts']} of "
          f"{resolved.detail['source_posts']} posts")
    try:
        resolve(user="@alice")
    except InputError as exc:
        print(f"\n  -U without --from:\n    {str(exc).splitlines()[-1][:74]}")

    # ------------------------------------------------------------------
    rule("6. Clustering: what merges, and what must never")
    SIGS = "Under 10 ILCS 5/10-2 Illinois requires 25,000 signatures to form a new party."
    cases = [
        ("reworded, same claim",
         "Illinois requires 25,000 signatures under 10 ILCS 5/10-2 to start a new party."),
        ("different number (20,000)",
         "Under 10 ILCS 5/10-2 Illinois requires 20,000 signatures to form a new party."),
        ("different section (5/10-3)",
         "Under 10 ILCS 5/10-3 Illinois requires 25,000 signatures to form a new party."),
        ("different claim, same statute",
         "Under 10 ILCS 5/10-2 the petition must list candidates for all offices."),
    ]
    base = DraftClaim(id="c-001", text=SIGS, sentence_index=0, claim_type=ClaimType.LEGAL)
    from abca.pipeline.cluster import signature

    for label, text_b in cases:
        other = DraftClaim(id="c-002", text=text_b, sentence_index=1,
                           claim_type=ClaimType.LEGAL)
        merged = len(run_cluster([base, other]).value.clusters) == 1
        if merged:
            why = "prose overlap cleared the bar"
        elif signature(base) != signature(other):
            why = "an exact gate differs (anchors / citation / type / stance)"
        else:
            why = "prose overlap below the threshold"
        print(f"  {'MERGE   ' if merged else 'SEPARATE'}  {label:32} "
              f"overlap={similarity(SIGS, text_b):.2f}  {why}")
    print("\n  Note the second row: 25,000 and 20,000 have IDENTICAL prose overlap,")
    print("  because a token comparison cannot see the difference between two")
    print("  numbers. The numeric-anchor gate is what separates them, and it runs")
    print("  BEFORE overlap is measured for exactly that reason.")
    print("\n  Type, stance, numeric anchors and the statute cited must all match")
    print("  EXACTLY before overlap is even measured. A cluster that should have")
    print("  merged and did not costs model calls; one that merged and should not")
    print("  publishes a wrong verdict about somebody's words. Only the second is")
    print("  a lie, so the thresholds make the first mistake instead.")

    # ------------------------------------------------------------------
    rule("7. `abca analyze -f thread.json` end to end")
    from mock_ollama import MODEL, serve

    data_home = workspace / "data"
    cache = SourceCache(data_home / "abca" / "sources")
    connector = ILCSConnector(cache=cache, offline=True)
    cache.put(build_document(
        connector=connector, doc_id="s-001", title="10 ILCS 5/10-2",
        url="https://www.ilga.gov/documents/legislation/ilcs/documents/001000050K10-2.htm",
        text=STATUTE, locator="10 ILCS 5/10-2",
        retrieved_at=datetime(2026, 9, 1, tzinfo=UTC),
    ))

    httpd, port = serve()
    config_path = workspace / "config.toml"
    config_path.write_text(
        f'[models.classifier]\nprovider = "ollama"\nmodel = "{MODEL}"\n'
        f'base_url = "http://127.0.0.1:{port}"\n\n'
        f'[models.adjudicator]\nprovider = "ollama"\nmodel = "{MODEL}"\n'
        f'base_url = "http://127.0.0.1:{port}"\n',
        encoding="utf-8")

    env = {**os.environ,
           "PYTHONPATH": str(Path(__file__).parent.parent / "src"),
           "COLUMNS": "150",
           "XDG_DATA_HOME": str(data_home)}
    env.pop("LOCALAPPDATA", None)

    def cli(args):
        return subprocess.run([sys.executable, "-m", "abca.cli.main", *args],
                              capture_output=True, text=True, env=env)

    run = cli(["analyze", "-f", str(workspace / "thread.json"),
               "--config", str(config_path), "--offline", "--json"])
    if run.returncode == 0:
        payload = json.loads(run.stdout)
        claims = payload["result"]["claims"]
        clustered = [c for c in claims if c.get("cluster")]
        print("  stages          : "
              + " -> ".join(s["name"] for s in payload["stages"]))
        print(f"  claims          : {len(claims)}")
        print(f"  clustered       : {len(clustered)} "
              + (f"(group of {clustered[0]['cluster']['members']}, weakest pair "
                 f"{clustered[0]['cluster']['min_similarity']:.2f})" if clustered else ""))
        print("  verdicts        : "
              + json.dumps({c['id']: c['verdict'] for c in claims}))
        blob = json.dumps(payload).lower()
        leaked = [h for h in ("alice", "bob", "carol", "dave") if h in blob]
        print(f"  handles in record: {leaked or 'none'}")
        inherited = [c for c in claims if "NOT adjudicated on its own text" in c["reasoning"]]
        print(f"  claims that say they inherited a verdict: {[c['id'] for c in inherited]}")
        audit = cli(["ledger", "audit", "--ledger-root",
                     str(data_home / "abca" / "runs")])
        print(f"  ledger audit    : exit {audit.returncode}")
    else:
        print(f"  exit {run.returncode}")
        print("  " + (run.stdout + run.stderr).strip()[:600])

    httpd.shutdown()
    print(f"\nworkspace: {workspace}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
