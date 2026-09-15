"""Turning exported conversation records into posts.

WHY FIELD DETECTION IS A FIXED, ORDERED LIST
============================================
Every platform names its fields differently -- ``text``, ``body``, ``content``,
``selftext``, ``full_text`` -- and an export is the realistic way a thread gets
into this tool without scraping anybody.

The tempting shortcut is to sniff: take the longest string field, or ask a
model which column holds the comment. Both are wrong here for the same reason.
The column choice decides **what text gets attributed to whom**, and a wrong
guess produces an analysis that is confidently about the wrong words. So the
candidates are a fixed ordered list, the first match wins, and **which field
was used is reported** into the run record. A file that matches nothing is
refused with the keys it actually had, rather than analyzed on a guess.

The same reasoning applies twice over to the author field, since that is what
``-U`` filters on.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime

from abca.inputs.threads import Post

#: Keys that may hold a post's text, in priority order. First match wins.
TEXT_KEYS: tuple[str, ...] = (
    "text", "body", "content", "message", "comment", "post",
    "selftext", "full_text", "caption", "description",
)

#: Keys that may hold the author. ``-U`` filters on whatever this resolves to,
#: so the order matters: a display name and a handle are different things, and
#: the handle is the one a person means when they type ``-U @someone``.
AUTHOR_KEYS: tuple[str, ...] = (
    "author", "handle", "screen_name", "username", "user_name",
    "user", "by", "from", "account", "name",
)

ID_KEYS: tuple[str, ...] = ("id", "post_id", "comment_id", "message_id")
PARENT_KEYS: tuple[str, ...] = (
    "parent_id", "in_reply_to", "in_reply_to_id", "reply_to", "parent", "thread_id",
)
TIME_KEYS: tuple[str, ...] = (
    "created_at", "posted_at", "timestamp", "time", "date", "when",
    "created", "created_utc", "published",
)

#: Keys whose value may hold nested replies.
CHILD_KEYS: tuple[str, ...] = ("replies", "children", "comments", "responses")

#: Keys that may hold the whole list of posts in a wrapper object.
COLLECTION_KEYS: tuple[str, ...] = (
    "posts", "comments", "messages", "items", "data", "results", "entries", "thread",
)

TITLE_KEYS: tuple[str, ...] = ("title", "headline", "subject")


class RecordFormatError(ValueError):
    """An export could not be read as a conversation.

    Raised rather than salvaged. A partially-understood export would produce an
    analysis of some subset of somebody's words, with no way for a reader to
    tell which subset.
    """


@dataclass(slots=True)
class FieldMapping:
    """Which keys were actually used, across every row. Reported, not assumed.

    Keys are resolved PER ROW from the fixed priority lists rather than fixed
    once from the first record, because a real export is heterogeneous: an
    article object carries ``selftext`` while its comments carry ``body``, and
    a single mapping chosen from row one silently drops every comment.

    Per-row resolution stays deterministic -- the priority order is fixed and
    the first match always wins -- and this object accumulates what was seen so
    the run record can say which fields the analysis actually read.
    """

    text: set[str] = field(default_factory=set)
    author: set[str] = field(default_factory=set)
    identifier: set[str] = field(default_factory=set)
    parent: set[str] = field(default_factory=set)
    timestamp: set[str] = field(default_factory=set)

    def observe(self, row: dict) -> None:
        for attribute, candidates in (
            ("text", TEXT_KEYS), ("author", AUTHOR_KEYS), ("identifier", ID_KEYS),
            ("parent", PARENT_KEYS), ("timestamp", TIME_KEYS),
        ):
            key = _first_key(row, candidates)
            if key:
                getattr(self, attribute).add(key)

    def describe(self) -> str:
        parts = []
        for label, value in (
            ("text", self.text), ("author", self.author), ("id", self.identifier),
            ("parent", self.parent), ("time", self.timestamp),
        ):
            if value:
                parts.append(f"{label}={'/'.join(sorted(value))}")
        return ", ".join(parts) or "<no fields resolved>"


def _first_key(row: dict, candidates: tuple[str, ...]) -> str | None:
    """First candidate present in ``row`` with a usable value."""
    lowered = {key.lower(): key for key in row}
    for candidate in candidates:
        actual = lowered.get(candidate)
        if actual is None:
            continue
        value = row[actual]
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return actual
    return None


def _as_text(value: object) -> str:
    """Coerce a field value to text without inventing content."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        # Some exports nest the body one level down, e.g. {"text": {"body": ...}}.
        for key in TEXT_KEYS:
            if isinstance(value.get(key), str):
                return value[key]
    return "" if value is None else str(value)


def _as_timestamp(value: object) -> datetime | None:
    """Parse a timestamp, or return None. Never guesses a date.

    A wrong timestamp is worse than no timestamp: it would order a conversation
    incorrectly and, with ``-U``, could place words in a window the author was
    not writing in.
    """
    if isinstance(value, int | float):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except (OSError, OverflowError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def assert_has_text_field(rows: list[dict]) -> None:
    """Refuse an export in which no record carries readable text.

    Checked before parsing so the failure names the fields the file actually
    has, which is the only thing that tells someone how to fix it. Walks nested
    replies too: an article wrapper whose own body is absent is still a valid
    conversation if its comments have bodies.
    """
    def has_text(candidates: list[dict], depth: int = 0) -> bool:
        if depth > 8:
            return False
        for row in candidates[:200]:
            if not isinstance(row, dict):
                continue
            if _first_key(row, TEXT_KEYS):
                return True
            for key in CHILD_KEYS:
                nested = row.get(key)
                if isinstance(nested, list) and has_text(nested, depth + 1):
                    return True
        return False

    if has_text(rows):
        return

    seen = sorted({key for row in rows[:20] if isinstance(row, dict) for key in row})
    raise RecordFormatError(
        "no field holding post text was found. One of "
        f"{', '.join(TEXT_KEYS[:6])} (and similar) is required; this export has: "
        f"{', '.join(seen) or '<no fields>'}. Rename the column that holds the "
        "message body, or pass the text with -t."
    )


def _row_to_post(row: dict, mapping: FieldMapping, depth: int) -> Post | None:
    """Build one post, resolving each field by priority. None if it has no text."""
    mapping.observe(row)

    text_key = _first_key(row, TEXT_KEYS)
    if not text_key:
        return None
    text = _as_text(row[text_key]).strip()
    if not text:
        return None

    author = None
    author_key = _first_key(row, AUTHOR_KEYS)
    if author_key:
        raw = row[author_key]
        if isinstance(raw, dict):
            # {"author": {"handle": "..."}} -- common in API exports.
            nested = _first_key(raw, AUTHOR_KEYS)
            raw = raw.get(nested) if nested else None
        if isinstance(raw, str) and raw.strip():
            author = raw.strip()

    parent_key = _first_key(row, PARENT_KEYS)
    time_key = _first_key(row, TIME_KEYS)
    return Post(
        id="",  # assigned positionally by Thread.build
        text=text,
        author=author,
        parent_id=str(row[parent_key]) if parent_key else None,
        timestamp=_as_timestamp(row[time_key]) if time_key else None,
        depth=depth,
    )


def _flatten(rows: list[dict], mapping: FieldMapping, depth: int = 0) -> list[Post]:
    """Depth-first walk, so a reply follows the post it answers."""
    posts: list[Post] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        post = _row_to_post(row, mapping, depth)
        if post is not None:
            posts.append(post)
        for key in CHILD_KEYS:
            children = row.get(key)
            if isinstance(children, list) and children:
                posts.extend(_flatten(children, mapping, depth + 1))
    return posts


def rows_from_json(payload: object) -> tuple[list[dict], str | None]:
    """Locate the list of post records inside a decoded JSON document.

    Returns ``(rows, title)``. Accepts three shapes, and refuses anything else
    rather than analyzing an arbitrary JSON dump as though it were prose:

    * a bare list of objects;
    * a wrapper object with a ``posts`` / ``comments`` / ``data`` list;
    * an article object that is itself a post and carries a comment list.
    """
    if isinstance(payload, list):
        rows = [row for row in payload if isinstance(row, dict)]
        if not rows:
            raise RecordFormatError(
                "the JSON array contains no objects. A conversation export is a "
                "list of records, each with a text field."
            )
        return rows, None

    if not isinstance(payload, dict):
        raise RecordFormatError(
            f"expected a JSON array or object, got {type(payload).__name__}."
        )

    title = None
    for key in TITLE_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            title = value.strip()
            break

    for key in COLLECTION_KEYS:
        value = payload.get(key)
        if isinstance(value, list) and any(isinstance(row, dict) for row in value):
            rows = [row for row in value if isinstance(row, dict)]
            # An article object whose own body is present leads its own thread.
            if _first_key(payload, TEXT_KEYS):
                lead = {k: v for k, v in payload.items() if not isinstance(v, list)}
                return [lead, *rows], title
            return rows, title

    if _first_key(payload, TEXT_KEYS):
        return [payload], title

    raise RecordFormatError(
        "this JSON object is not a conversation. Expected a list under one of "
        f"{', '.join(COLLECTION_KEYS[:5])}, or an object with a text field. "
        f"Top-level keys found: {', '.join(sorted(payload)) or '<none>'}."
    )


def posts_from_json(text: str) -> tuple[list[Post], str | None, FieldMapping]:
    """Parse a JSON conversation export."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RecordFormatError(f"not valid JSON: {exc}") from exc

    rows, title = rows_from_json(payload)
    assert_has_text_field(rows)
    mapping = FieldMapping()
    return _flatten(rows, mapping), title, mapping


def posts_from_csv(text: str) -> tuple[list[Post], str | None, FieldMapping]:
    """Parse a CSV conversation export.

    The dialect is sniffed, falling back to a comma. A wrong delimiter yields
    one column whose header is the whole line, which then fails field detection
    with a message naming that header -- a confusing but honest failure, and
    better than silently analyzing every row as one long string.
    """
    sample = text[:8192]
    try:
        dialect: type[csv.Dialect] | csv.Dialect = csv.Sniffer().sniff(
            sample, delimiters=",;\t|"
        )
    except csv.Error:
        dialect = csv.excel

    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    rows = [
        {key: value for key, value in row.items() if key is not None}
        for row in reader
    ]
    if not rows:
        raise RecordFormatError("the CSV has a header but no data rows.")

    assert_has_text_field(rows)
    mapping = FieldMapping()
    return _flatten(rows, mapping), None, mapping


__all__ = [
    "AUTHOR_KEYS",
    "CHILD_KEYS",
    "COLLECTION_KEYS",
    "ID_KEYS",
    "PARENT_KEYS",
    "TEXT_KEYS",
    "TIME_KEYS",
    "TITLE_KEYS",
    "FieldMapping",
    "RecordFormatError",
    "assert_has_text_field",
    "posts_from_csv",
    "posts_from_json",
    "rows_from_json",
]
