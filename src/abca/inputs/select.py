"""Choosing an input, and turning it into a thread.

ONE PLACE WHERE ``-t``, ``-f``, ``-u``, ``-U`` AND ``--stdin`` BECOME THE SAME THING
====================================================================================
Every input the CLI accepts resolves here into a
:class:`~abca.inputs.threads.Thread` plus the
:class:`~abca.schema.enums.InputKind` that says where it came from. Nothing
downstream branches on the flag that was used.

That is worth a module of its own because the alternative -- five call sites in
the CLI, each building its own document -- is how ``-t`` ends up normalizing
text one way and ``-f`` another, and how a limit that applies to one input
silently does not apply to the rest.

``-U`` IS A FILTER, NOT A FETCHER
=================================
``-U @someone`` names an account. It does NOT go and get that account's posts,
because the only way to do that for a real platform is to scrape it, and
scraping is fragile, usually against the site's terms, and would make this tool
a surveillance instrument the moment it worked.

So ``-U`` takes ``--from``: an export the person already has, or a thread URL.
The account is then a FILTER over posts that are already in hand. What comes out
is claims made in that account's own words, each adjudicated on its own
evidence -- and never an aggregate about the person (contract s8).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from abca.inputs.files import FileReadError, read_file
from abca.inputs.threads import DEFAULT_MAX_POSTS, Thread
from abca.inputs.web import UrlReadError, read_url
from abca.schema.enums import InputKind


class InputError(ValueError):
    """The requested input could not be resolved into something analyzable."""


@dataclass(slots=True)
class ResolvedInput:
    """A thread, with the provenance the ingest stage records."""

    thread: Thread
    kind: InputKind
    #: How the text was obtained: ``text``, ``docx``, ``json-ld``, ``page``...
    extractor: str = "text"
    #: Extractor-specific provenance, recorded in the ingest stage output.
    detail: dict[str, str | int] = field(default_factory=dict)


def looks_like_url(value: str) -> bool:
    return value.startswith(("http://", "https://"))


def resolve(
    *,
    text: str | None = None,
    file: Path | str | None = None,
    url: str | None = None,
    user: str | None = None,
    source: Path | str | None = None,
    stdin: str | None = None,
    max_posts: int = DEFAULT_MAX_POSTS,
    timeout: float = 30.0,
) -> ResolvedInput:
    """Resolve exactly one input into a thread.

    Exactly one, enforced here rather than by the CLI's flag parsing, so the
    rule is testable without a terminal and cannot differ between the two.
    """
    chosen = [
        name for name, value in (
            ("-t/--text", text), ("-f/--file", file), ("-u/--url", url),
            ("-U/--user", user), ("--stdin", stdin),
        )
        if value is not None
    ]
    if not chosen:
        raise InputError(
            "no input. Use one of:\n"
            '  -t "a statement"          analyze text directly\n'
            "  -f path/to/file           .txt .md .json .csv .docx .pdf\n"
            "  -u https://...            an article or thread\n"
            "  -U @handle --from SOURCE  one account's posts within an export or thread\n"
            "  --stdin                   read from a pipe"
        )
    if len(chosen) > 1:
        raise InputError(
            f"{' and '.join(chosen)} were both given; they are mutually exclusive. "
            "Analyze one document per run so a run record describes one thing."
        )

    if text is not None:
        return ResolvedInput(Thread.single(text), InputKind.TEXT)

    if stdin is not None:
        return ResolvedInput(Thread.single(stdin, locator="<stdin>"), InputKind.STDIN)

    if file is not None:
        return _from_file(file, max_posts=max_posts, kind=InputKind.FILE)

    if url is not None:
        return _from_url(url, max_posts=max_posts, timeout=timeout, kind=InputKind.URL)

    return _from_user(user or "", source, max_posts=max_posts, timeout=timeout)


def _from_file(
    file: Path | str, *, max_posts: int, kind: InputKind
) -> ResolvedInput:
    try:
        read = read_file(file, max_posts=max_posts)
    except FileReadError as exc:
        raise InputError(str(exc)) from exc
    detail: dict[str, str | int] = dict(read.detail)
    if read.encoding:
        detail["encoding"] = read.encoding
        if read.encoding != "utf-8":
            read.thread.notes.append(
                f"decoded as {read.encoding}, not UTF-8. Characters outside that "
                "codec's repertoire cannot survive; if anything below looks wrong, "
                "convert the file to UTF-8 and re-run."
            )
    return ResolvedInput(read.thread, kind, extractor=read.extractor, detail=detail)


def _from_url(
    url: str, *, max_posts: int, timeout: float, kind: InputKind
) -> ResolvedInput:
    try:
        page = read_url(url, max_posts=max_posts, timeout=timeout)
    except UrlReadError as exc:
        raise InputError(str(exc)) from exc
    return ResolvedInput(
        page.thread,
        kind,
        extractor=page.extractor,
        detail={
            "final_url": page.final_url,
            "content_type": page.content_type,
            "redirects": len(page.redirects),
        },
    )


def _from_user(
    handle: str,
    source: Path | str | None,
    *,
    max_posts: int,
    timeout: float,
) -> ResolvedInput:
    """``-U``: filter an export or thread down to one account's posts."""
    if not handle.strip():
        raise InputError("-U needs an account handle, e.g. -U @someone.")

    if source is None:
        raise InputError(
            f"-U {handle} needs --from, naming where the posts are:\n"
            "  --from export.json        a platform data export\n"
            "  --from thread.csv         a saved conversation\n"
            "  --from https://...        a thread with structured comment data\n\n"
            "This build does not go and fetch an account's history. Doing that means "
            "scraping, which is brittle, generally against the platform's terms, and "
            "would turn this into a surveillance tool the moment it worked. -U filters "
            "posts you already have."
        )

    reference = str(source)
    if looks_like_url(reference):
        resolved = _from_url(
            reference, max_posts=max_posts, timeout=timeout, kind=InputKind.USER
        )
    else:
        resolved = _from_file(reference, max_posts=max_posts, kind=InputKind.USER)

    if resolved.thread.author_count == 0:
        raise InputError(
            f"--from {reference} has no author information, so -U {handle} cannot "
            "select anyone's posts. Use an export whose records name an author, or "
            "analyze the whole document with -f/-u."
        )

    filtered = resolved.thread.by_author(handle)
    if not filtered.posts:
        available = resolved.thread.author_count
        raise InputError(
            f"no posts by {handle} in {reference} ({available} account(s) appear "
            "there). Check the handle exactly as the export spells it -- matching is "
            "deliberately literal, because near-matching would attribute one "
            "person's words to another."
        )

    return ResolvedInput(
        filtered,
        InputKind.USER,
        extractor=resolved.extractor,
        detail={
            **resolved.detail,
            "selected_posts": len(filtered.posts),
            "source_posts": len(resolved.thread.posts),
        },
    )


__all__ = ["InputError", "ResolvedInput", "looks_like_url", "resolve"]
