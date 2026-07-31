"""Typed access to WordPress WXR posts, attachments, and post metadata."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

WP_NS = "http://wordpress.org/export/1.2/"
CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"
EXCERPT_NS = "http://wordpress.org/export/1.2/excerpt/"


@dataclass
class WordPressItem:
    """One post, page, or attachment from a WXR export."""

    post_id: int
    post_type: str
    status: str
    title: str
    slug: str
    link: str
    content: str
    excerpt: str
    attachment_url: str
    metadata: dict[str, list[str]] = field(default_factory=dict)

    def first_meta(self, key: str) -> str:
        """Return the first value for a postmeta key."""

        values = self.metadata.get(key, [])
        return values[0] if values else ""


def parse_wordpress_items(xml_path: Path) -> list[WordPressItem]:
    """Parse a WordPress export into typed items."""

    root = ET.parse(xml_path).getroot()
    channel = root.find("channel")
    if channel is None:
        raise ValueError("WordPress XML has no RSS channel.")
    result: list[WordPressItem] = []
    for element in channel.findall("item"):
        metadata: dict[str, list[str]] = {}
        for postmeta in element.findall(f"{{{WP_NS}}}postmeta"):
            key = postmeta.findtext(f"{{{WP_NS}}}meta_key", default="")
            value = postmeta.findtext(f"{{{WP_NS}}}meta_value", default="")
            metadata.setdefault(key, []).append(value)
        raw_id = element.findtext(f"{{{WP_NS}}}post_id", default="0")
        try:
            post_id = int(raw_id)
        except ValueError:
            post_id = 0
        result.append(
            WordPressItem(
                post_id=post_id,
                post_type=element.findtext(
                    f"{{{WP_NS}}}post_type", default=""
                ).strip(),
                status=element.findtext(
                    f"{{{WP_NS}}}status", default=""
                ).strip(),
                title=element.findtext("title", default="").strip(),
                slug=element.findtext(
                    f"{{{WP_NS}}}post_name", default=""
                ).strip(),
                link=element.findtext("link", default="").strip(),
                content=element.findtext(
                    f"{{{CONTENT_NS}}}encoded", default=""
                ),
                excerpt=element.findtext(
                    f"{{{EXCERPT_NS}}}encoded", default=""
                ),
                attachment_url=element.findtext(
                    f"{{{WP_NS}}}attachment_url", default=""
                ).strip(),
                metadata=metadata,
            )
        )
    return result
