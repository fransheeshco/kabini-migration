"""Normalize WordPress object descriptions while preserving paragraphs."""

from __future__ import annotations

import html
import re
import unicodedata

from bs4 import BeautifulSoup

ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]")
SPACE_RE = re.compile(r"[^\S\n]+")
BLANK_LINES_RE = re.compile(r"\n[ \t]*\n(?:[ \t]*\n)+")


def normalize_text_fragment(value: str) -> str:
    """Remove invisible artifacts and normalize whitespace in one fragment."""

    value = html.unescape(value or "")
    value = value.replace("\xa0", " ")
    value = ZERO_WIDTH_RE.sub("", value)
    value = unicodedata.normalize("NFC", value)
    return SPACE_RE.sub(" ", value).strip()


def normalize_description(value: str) -> str:
    """Normalize description text while retaining meaningful blank lines."""

    value = value.replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = []
    for raw in re.split(r"\n\s*\n|\n", value):
        cleaned = normalize_text_fragment(raw)
        if cleaned and (not paragraphs or cleaned != paragraphs[-1]):
            paragraphs.append(cleaned)
    return "\n\n".join(paragraphs)


def html_to_description(value: str) -> str:
    """Convert HTML blocks to normalized plain-text paragraphs."""

    if not value:
        return ""
    soup = BeautifulSoup(value, "html.parser")
    blocks: list[str] = []
    for tag in soup.find_all(
        ["h1", "h2", "h3", "h4", "h5", "h6", "p", "figcaption", "li"]
    ):
        text = normalize_text_fragment(tag.get_text(" ", strip=True))
        if text and (not blocks or text != blocks[-1]):
            blocks.append(text)
    if not blocks:
        text = normalize_text_fragment(soup.get_text(" ", strip=True))
        if text:
            blocks.append(text)
    return "\n\n".join(blocks)


def description_to_rich_text(value: str, title: str = "") -> str:
    """Convert normalized description paragraphs to conservative HTML."""

    paragraphs = [
        normalize_text_fragment(part)
        for part in re.split(r"\n\s*\n", normalize_description(value))
        if normalize_text_fragment(part)
    ]
    rendered: list[str] = []
    normalized_title = normalize_text_fragment(title).casefold()
    for index, paragraph in enumerate(paragraphs):
        escaped = html.escape(paragraph, quote=False)
        if index == 0 and paragraph.casefold() == normalized_title:
            rendered.append(f"<p><strong>{escaped}</strong></p>")
        else:
            rendered.append(f"<p>{escaped}</p>")
    return "".join(rendered)
