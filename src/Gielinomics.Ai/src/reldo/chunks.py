"""Split article wikitext into embeddable passages.

The lead-paragraph index answers "which page is this about" well and "which page
explains this mechanic" badly. Measured on the 35-question eval, every remaining
miss is the same shape: a fact that lives in body prose and never appears in a
lead. "Boss that heals itself when you use the wrong attack style" is in a
Strategies section; "that spiky whip upgrade thing" is in the tentacle's Creation
section. No amount of re-ranking over lead paragraphs finds those.

Section *headings* were tried first and made things worse -- generic furniture
diluted the vector. Bodies are the right lever: each chunk gets its own vector
and its own dimensions to spend, rather than competing for one article's.

**Wikitext, not rendered extracts.** ``prop=extracts`` without ``exintro``
silently clamps to one title per call, which would make a full-corpus fetch 35k
sequential requests -- hours. Raw wikitext batches 50 at a time (~700 calls, a
few minutes) at the cost of having to clean markup ourselves. Embeddings tolerate
imperfect cleaning far better than the schedule tolerates hours.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Target chunk size in characters. Roughly 250 tokens -- big enough to carry a
# whole mechanic ("Vorkath heals when you attack with the wrong style") and small
# enough that one vector isn't averaging three unrelated topics.
CHUNK_CHARS = 1_000

# Carried between adjacent chunks so a fact split across a boundary survives in
# at least one of them whole.
CHUNK_OVERLAP = 150

# Sections that are navigational or trivia rather than content. Same reasoning as
# the heading-frequency filter, but here it saves embedding cost rather than
# diluting a vector: nobody asks a question answered by a Gallery.
SKIP_SECTIONS = frozenset(
    {
        "references", "gallery", "trivia", "changes", "update history",
        "external links", "see also", "navigation", "notes",
    }
)

_HEADING = re.compile(r"^[ \t]*(={2,6})[ \t]*(.+?)[ \t]*\1[ \t]*$", re.MULTILINE)
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_REF = re.compile(r"<ref[^>]*>.*?</ref>|<ref[^>]*/>", re.DOTALL | re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_FILE = re.compile(r"\[\[(?:File|Image):[^\]]*\]\]", re.IGNORECASE)
_TABLE = re.compile(r"\{\|.*?\|\}", re.DOTALL)
_LINK_PIPED = re.compile(r"\[\[(?:[^\]|]*\|)?([^\]|]+)\]\]")
_EXTLINK = re.compile(r"\[https?://\S+\s+([^\]]+)\]")
_QUOTES = re.compile(r"'{2,5}")
_WS = re.compile(r"[ \t]+")
_BLANKS = re.compile(r"\n{2,}")


@dataclass(frozen=True, slots=True)
class Chunk:
    """One embeddable passage, and where it came from."""

    title: str
    section: str
    text: str

    def document(self) -> str:
        """The string that actually gets embedded.

        Title and section are prepended because a bare paragraph often omits its
        own subject -- a Strategies section says "it heals when..." without ever
        repeating which monster "it" is.
        """
        header = f"{self.title} - {self.section}" if self.section else self.title
        return f"{header}. {self.text}"


def strip_wikitext(raw: str) -> str:
    """Flatten wikitext into prose.

    Templates are dropped rather than expanded. Most of the bulk on an OSRS page
    is infoboxes and navboxes, which are structured data better served by the
    Bucket API than by an embedding; what's left after removing them is the prose
    that actually explains mechanics.
    """
    text = _COMMENT.sub("", raw)
    text = _REF.sub("", text)
    text = _FILE.sub("", text)
    text = _TABLE.sub(" ", text)
    text = _strip_templates(text)
    text = _LINK_PIPED.sub(r"\1", text)
    text = _EXTLINK.sub(r"\1", text)
    text = _TAG.sub("", text)
    text = _QUOTES.sub("", text)
    text = re.sub(r"^[ \t]*[*#:;]+[ \t]*", "", text, flags=re.MULTILINE)
    text = _WS.sub(" ", text)
    return _BLANKS.sub("\n", text).strip()


def _strip_templates(text: str) -> str:
    """Remove {{...}} including nested braces, which a regex can't do alone."""
    out: list[str] = []
    depth = 0
    i = 0
    while i < len(text):
        if text.startswith("{{", i):
            depth += 1
            i += 2
        elif text.startswith("}}", i) and depth:
            depth -= 1
            i += 2
        else:
            if not depth:
                out.append(text[i])
            i += 1
    return "".join(out)


def split_sections(wikitext: str) -> list[tuple[str, str]]:
    """Split into (section_name, body). The lead is section ""."""
    matches = list(_HEADING.finditer(wikitext))
    if not matches:
        return [("", wikitext)]

    sections = [("", wikitext[: matches[0].start()])]
    for n, match in enumerate(matches):
        end = matches[n + 1].start() if n + 1 < len(matches) else len(wikitext)
        sections.append((match.group(2).strip(), wikitext[match.end() : end]))
    return sections


def chunk_text(text: str, *, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split text into overlapping chunks, preferring paragraph boundaries."""
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = start + size
        if end < len(text):
            # Back off to the last paragraph or sentence break in the tail of the
            # window, so a chunk doesn't end mid-sentence when it needn't.
            window = text[start:end]
            for sep in ("\n", ". "):
                cut = window.rfind(sep, int(size * 0.5))
                if cut != -1:
                    end = start + cut + len(sep)
                    break
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return [c for c in chunks if c]


def chunk_article(title: str, wikitext: str, *, min_chars: int = 80) -> list[Chunk]:
    """Turn one article's wikitext into embeddable chunks.

    **Every article yields at least one chunk.** The lead index embedded one
    vector per article, so every page was findable; a naive length filter here
    would silently drop short pages out of the index entirely and quietly lose
    coverage the previous design had. Two guards prevent that:

    * The lead section is exempt from ``min_chars`` -- a one-line lead plus the
      title is still a usable vector, and it is the article's identity.
    * An article whose wikitext is all infobox and no prose (about 7% of the
      corpus) still gets a title-only chunk, so it can be matched by name.
    """
    out: list[Chunk] = []
    for section, body in split_sections(wikitext):
        if section.strip().lower() in SKIP_SECTIONS:
            continue
        prose = strip_wikitext(body)
        is_lead = section == ""
        if not prose or (len(prose) < min_chars and not is_lead):
            continue
        for piece in chunk_text(prose):
            if len(piece) >= min_chars or is_lead:
                out.append(Chunk(title=title, section=section, text=piece))

    return out or [Chunk(title=title, section="", text="")]
