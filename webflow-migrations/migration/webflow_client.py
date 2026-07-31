"""Shared Webflow schema and API error utilities.

The established ``WebflowClient`` remains orchestrator-owned during this
compatibility refactor; all HTTP calls are still encapsulated by that one
client, while this module provides the public API exception type.
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import re
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

import requests

from .config import (
    API_BASE,
    DEFAULT_MAX_IMAGE_BYTES,
    DEFAULT_MAX_RETRIES,
    DEFAULT_REQUEST_TIMEOUT,
    RETRYABLE_STATUS_CODES,
    SUPPORTED_IMAGE_EXTENSIONS,
)
from .exceptions import MigrationError, WebflowAPIError


class WebflowClient:
    """Reusable Webflow client scoped to one CMS collection."""

    def __init__(
        self,
        token: str,
        site_id: str,
        collection_id: str,
        timeout: int = DEFAULT_REQUEST_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        self._token = token
        self.site_id = site_id
        self.collection_id = collection_id
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )

    def request(
        self,
        method: str,
        url: str,
        *,
        expected: tuple[int, ...] = (200,),
        **kwargs: Any,
    ) -> requests.Response:
        """Execute one retry-aware Webflow request."""

        last_error: str | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.request(
                    method, url, timeout=self.timeout, **kwargs
                )
            except requests.RequestException as exc:
                last_error = f"{method} {url} failed: {exc}"
                if attempt >= self.max_retries:
                    raise WebflowAPIError(last_error) from exc
                delay = min(2**attempt, 30)
                logging.warning("Network error. Retrying in %ss: %s", delay, exc)
                time.sleep(delay)
                continue
            if response.status_code in expected:
                return response
            safe_body = response.text[:4000].replace(self._token, "[REDACTED]")
            safe_body = re.sub(
                r"(?i)Bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", safe_body
            )
            last_error = (
                f"{method} {url} returned {response.status_code}: "
                f"{safe_body}"
            )
            if (
                response.status_code not in RETRYABLE_STATUS_CODES
                or attempt >= self.max_retries
            ):
                raise WebflowAPIError(last_error)
            retry_after = response.headers.get("Retry-After")
            delay = (
                int(retry_after)
                if retry_after and retry_after.isdigit()
                else min(2**attempt, 30)
            )
            logging.warning(
                "Temporary Webflow error %s. Retrying in %ss...",
                response.status_code,
                delay,
            )
            time.sleep(delay)
        raise WebflowAPIError(last_error or "Unknown Webflow request failure.")

    def get_collection_schema(
        self, collection_id: str | None = None
    ) -> dict[str, Any]:
        """Read one collection schema."""

        target_id = collection_id or self.collection_id
        return self.request(
            "GET", f"{API_BASE}/collections/{target_id}"
        ).json()

    def list_collections(self) -> list[dict[str, Any]]:
        """List all collections belonging to the configured site."""

        payload = self.request("GET", f"{API_BASE}/sites/{self.site_id}/collections").json()
        collections = payload.get("collections", payload)
        if not isinstance(collections, list):
            raise WebflowAPIError(f"Unexpected collections response for site {self.site_id}: {payload}")
        return collections

    def create_collection(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create one collection in the configured site."""

        return self.request(
            "POST", f"{API_BASE}/sites/{self.site_id}/collections",
            expected=(200, 201, 202), json=payload,
        ).json()

    def create_collection_field(
        self, collection_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Add one field to an existing collection."""

        return self.request(
            "POST", f"{API_BASE}/collections/{collection_id}/fields",
            expected=(200, 201, 202), json=payload,
        ).json()

    def list_items(
        self,
        collection_id: str | None = None,
        page_limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Read every item from a collection using offset pagination."""

        target_id = collection_id or self.collection_id
        items: list[dict[str, Any]] = []
        offset = 0
        while True:
            payload = self.request(
                "GET",
                f"{API_BASE}/collections/{target_id}/items",
                params={"limit": page_limit, "offset": offset},
            ).json()
            page_items = payload.get("items", [])
            if not isinstance(page_items, list):
                raise WebflowAPIError(
                    "Unexpected list-items response for collection "
                    f"{target_id}: {payload}"
                )
            items.extend(page_items)
            if len(page_items) < page_limit:
                return items
            offset += page_limit

    def create_items(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        """Create one Webflow CMS item or one API batch."""

        if not 1 <= len(items) <= 100:
            raise MigrationError("Each Webflow batch must contain 1-100 items.")
        payload: Any = items[0] if len(items) == 1 else items
        return self.request(
            "POST",
            f"{API_BASE}/collections/{self.collection_id}/items",
            expected=(200, 201, 202),
            params={"skipInvalidFiles": "false"},
            json=payload,
        ).json()

    def update_item(
        self, item_id: str, item: dict[str, Any]
    ) -> dict[str, Any]:
        """Update an existing draft CMS item."""

        return self.request(
            "PATCH",
            f"{API_BASE}/collections/{self.collection_id}/items/{item_id}",
            expected=(200, 202),
            params={"skipInvalidFiles": "false"},
            json=item,
        ).json()

    def upload_asset(
        self,
        file_path: Path,
        alt_text: str = "",
        *,
        max_size_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
    ) -> dict[str, Any]:
        """Upload an image asset and return its Webflow image reference."""

        if not file_path.is_file():
            raise MigrationError(f"Image not found: {file_path}")
        if file_path.suffix.casefold() not in SUPPORTED_IMAGE_EXTENSIONS:
            raise MigrationError(f"Unsupported image type: {file_path.name}")
        size_bytes = file_path.stat().st_size
        if size_bytes > max_size_bytes:
            raise MigrationError(
                f"Image exceeds configured upload limit "
                f"({max_size_bytes / (1024 * 1024):.2f} MB): {file_path}"
            )
        file_bytes = file_path.read_bytes()
        metadata = self.request(
            "POST",
            f"{API_BASE}/sites/{self.site_id}/assets",
            expected=(200, 201, 202),
            json={
                "fileName": file_path.name[:99],
                "fileHash": hashlib.md5(file_bytes).hexdigest(),
            },
        ).json()
        upload_url = metadata.get("uploadUrl")
        upload_details = metadata.get("uploadDetails")
        if not upload_url or not isinstance(upload_details, dict):
            raise WebflowAPIError(
                f"Webflow did not return upload details for {file_path.name}: "
                f"{metadata}"
            )
        content_type = (
            metadata.get("contentType")
            or mimetypes.guess_type(file_path.name)[0]
            or "application/octet-stream"
        )
        try:
            upload_response = requests.post(
                upload_url,
                data=upload_details,
                files={"file": (file_path.name, file_bytes, content_type)},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise WebflowAPIError(
                f"Asset upload failed for {file_path.name}: {exc}"
            ) from exc
        if upload_response.status_code not in (200, 201, 204):
            raise WebflowAPIError(
                f"Storage upload failed for {file_path.name}: "
                f"{upload_response.status_code} {upload_response.text[:2000]}"
            )
        file_id = metadata.get("id") or metadata.get("fileId")
        hosted_url = metadata.get("hostedUrl") or metadata.get("assetUrl")
        if not file_id or not hosted_url:
            raise WebflowAPIError(
                f"Missing file ID or hosted URL after uploading "
                f"{file_path.name}: {metadata}"
            )
        return {"fileId": file_id, "url": hosted_url, "alt": alt_text}


def find_special_features_picture_field(
    collection_fields: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    """Find the actual schema field for Special Features Picture."""

    for field in collection_fields:
        if (
            str(field.get("displayName") or "").casefold()
            == "special features picture"
            and str(field.get("type")) in {"Image", "ImageRef"}
        ):
            return dict(field)
    return None

__all__ = [
    "WebflowClient",
    "WebflowAPIError",
    "find_special_features_picture_field",
]
