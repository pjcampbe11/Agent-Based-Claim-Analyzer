"""Conversations: posts, authors, and the one document they become.

WHY A THREAD IS FLATTENED INTO ONE DOCUMENT
===========================================
Everything downstream -- the content hash, sentence offsets, claim spans, the
ledger -- indexes a single string. A thread is therefore rendered into one
document with a span recorded per post, rather than analyzed post by post.

That choice is what keeps ``-t``, ``-f`` and ``-u`` on one code path: a bare
statement is a thread of one post, and no stage below ingest has to know the
difference.

AUTHORS ARE PSEUDONYMIZED BEFORE ANYTHING IS RECORDED
=====================================================
Contract s8 refuses to score, rank or characterize a person, and refuses to
analyze private individuals by default. A run record is meant to be published.
Put those together and the conclusion is forced: **a published record must not
carry the handles of the people whose comments were analyzed.**

So a handle becomes ``a-01``, ``a-02`` in first-appearance order, and only the
pseudonym is ever written into a record. The real handles live on the
:class:`Thread` in memory, where the local terminal report can use them --
exactly the rule the input text already follows, where the record stores the
hash and the local input store keeps the text.

Positional pseudonyms rather than hashed handles, deliberately. A hash of a
handle is reversible by anyone with a list of handles to try, which for a
public platform is everyone. ``a-03`` leaks the order someone commented in and
nothing else.

WHAT THIS BUILD DOES NOT DO
===========================
Cross-post reference resolution. A reply reading "that's wrong, it's actually
25,000" is analyzed on its own, without the post it answers, so segmentation
sees an assertion with an unresolved referent -- and the relevance gate usually
drops it. That is a real limitation, reported in the run notes rather than
papered over, because the alternative available today is to guess at what a
comment referred to and attribute the result to its author.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

#: Blank line between posts in the rendered document.
#:
#: Two newlines rather than one: the sentence splitter treats a paragraph break
#: as a hard boundary, so this stops a post that ends without punctuation from
#: running into the next one and producing a sentence with two authors in it.
POST_SEPARATOR = "\n\n"

#: Maximum posts carried into analysis from one input. A thread larger than
#: this is truncated -- visibly, with a note -- rather than turned into
#: thousands of model calls by accident.
DEFAULT_MAX_POSTS = 500

_HANDLE_NOISE = re.compile(r"^[@/\s]+|[\s]+$")


def normalize_handle(handle: str) -> str:
    """Canonical form of an author handle, for matching ``-U`` against a thread.

    Strips a leading ``@`` or ``/`` and surrounding whitespace, and lowercases.
    Deliberately nothing more: two handles differing by a character are two
    different people, and any cleverer matching would eventually attribute one
    person's claims to another.
    """
    return _HANDLE_NOISE.sub("", handle).lower()


@dataclass(frozen=True, slots=True)
class Post:
    """One message in a conversation."""

    id: str
    text: str
    #: The handle as it appeared in the source. IN MEMORY ONLY -- never written
    #: to a run record. ``None`` when the source did not name an author.
    author: str | None = None
    #: ``a-01``-style stand-in, assigned by :meth:`Thread.build`. The only form
    #: of authorship that reaches a record.
    author_pseudonym: str = "a-00"
    #: Id of the post this replies to, when the source expresses one.
    parent_id: str | None = None
    timestamp: datetime | None = None
    #: Reply depth; 0 for a root post.
    depth: int = 0

    @property
    def display_author(self) -> str:
        """What the LOCAL report shows: the real handle if there is one."""
        return self.author or self.author_pseudonym


@dataclass(frozen=True, slots=True)
class PostSpan:
    """Where one post landed in the rendered document."""

    post_id: str
    start: int
    end: int


@dataclass(slots=True)
class Thread:
    """A conversation, ready to be rendered into one analyzable document."""

    posts: list[Post] = field(default_factory=list)
    title: str | None = None
    #: Where this came from: a path, a URL, ``<inline>``.
    locator: str = "<inline>"
    #: Operator-facing notes from parsing. Carried into the run record.
    notes: list[str] = field(default_factory=list)
    #: Real handle -> pseudonym. In memory only.
    authors: dict[str, str] = field(default_factory=dict)
    #: Set when posts were dropped to respect a cap.
    truncated: bool = False

    # ------------------------------------------------------------------ build

    @classmethod
    def build(
        cls,
        posts: list[Post],
        *,
        title: str | None = None,
        locator: str = "<inline>",
        notes: list[str] | None = None,
        max_posts: int = DEFAULT_MAX_POSTS,
    ) -> Thread:
        """Assign pseudonyms and ids, applying the post cap.

        Ids and pseudonyms are both assigned by POSITION, so the same input
        always produces the same thread. Anything else -- a random id, a
        timestamp, a hash of the handle -- would make two runs of the same
        input produce two different documents and two different hashes.
        """
        collected = list(notes or [])
        truncated = len(posts) > max_posts
        if truncated:
            collected.append(
                f"thread has {len(posts)} posts; analyzed the first {max_posts}. "
                "The remainder was NOT analyzed, and no claim below should be read "
                "as covering it. Raise --max-posts to include more."
            )
            posts = posts[:max_posts]

        authors: dict[str, str] = {}
        numbered: list[Post] = []
        for index, post in enumerate(posts, start=1):
            pseudonym = "a-00"
            if post.author:
                key = normalize_handle(post.author)
                if key not in authors:
                    authors[key] = f"a-{len(authors) + 1:02d}"
                pseudonym = authors[key]
            numbered.append(
                Post(
                    id=f"p-{index:03d}",
                    text=post.text,
                    author=post.author,
                    author_pseudonym=pseudonym,
                    parent_id=post.parent_id,
                    timestamp=post.timestamp,
                    depth=post.depth,
                )
            )

        return cls(
            posts=numbered,
            title=title,
            locator=locator,
            notes=collected,
            authors=authors,
            truncated=truncated,
        )

    @classmethod
    def single(cls, text: str, *, locator: str = "<inline>") -> Thread:
        """A bare statement, as a thread of one anonymous post.

        Keeps ``-t`` on the same path as every other input rather than giving
        it a parallel one that would drift.
        """
        return cls.build([Post(id="p-001", text=text)], locator=locator)

    # ----------------------------------------------------------------- render

    def render(self) -> tuple[str, list[PostSpan]]:
        """Flatten to one document plus the span of each post within it.

        No author prefix is written into the text. A ``[a-01]`` marker would be
        hashed as part of the document, would be seen by the segmenter, and
        would eventually be extracted as though the author had written it.
        Authorship is tracked ALONGSIDE the text, in the span map, not inside it.
        """
        chunks: list[str] = []
        spans: list[PostSpan] = []
        cursor = 0
        for post in self.posts:
            body = post.text.strip()
            if not body:
                continue
            if chunks:
                cursor += len(POST_SEPARATOR)
            spans.append(PostSpan(post_id=post.id, start=cursor, end=cursor + len(body)))
            cursor += len(body)
            chunks.append(body)
        return POST_SEPARATOR.join(chunks), spans

    # ------------------------------------------------------------------ views

    @property
    def author_count(self) -> int:
        return len(self.authors)

    def by_author(self, handle: str) -> Thread:
        """A thread containing only ``handle``'s posts.

        Used by ``-U``. Ids and pseudonyms are REASSIGNED by
        :meth:`build`, because the filtered thread is a different document with
        different offsets -- carrying the originals over would produce spans
        that point into a document that was never analyzed.
        """
        wanted = normalize_handle(handle)
        kept = [
            post for post in self.posts
            if post.author and normalize_handle(post.author) == wanted
        ]
        return Thread.build(
            kept,
            title=self.title,
            locator=self.locator,
            notes=[
                *self.notes,
                (
                    f"filtered to {len(kept)} of {len(self.posts)} post(s) written "
                    "by the requested account. Claims below are that account's own "
                    "words; no claim from anyone else in the thread was analyzed."
                ),
            ],
        )

    def post_for_offset(self, offset: int, spans: list[PostSpan]) -> str | None:
        """Which post a document offset falls inside. For the local report only."""
        for span in spans:
            if span.start <= offset < span.end:
                return span.post_id
        return None


__all__ = [
    "DEFAULT_MAX_POSTS",
    "POST_SEPARATOR",
    "Post",
    "PostSpan",
    "Thread",
    "normalize_handle",
]
