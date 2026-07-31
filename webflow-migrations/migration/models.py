"""Shared typed structures used by migration modules."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Generic, Optional, TypeVar

ImageType = TypeVar("ImageType")


@dataclass(frozen=True)
class ImageCandidate:
    """A source image and the metadata used for duplicate selection."""

    path: Path
    gallery_slug: str
    logical_key: str
    width: int
    height: int
    size_bytes: int
    filename_width: Optional[int] = None
    filename_height: Optional[int] = None
    has_dimension_suffix: bool = False
    is_hero: bool = False

    @property
    def pixel_area(self) -> int:
        """Return the image's total pixel area."""

        return self.width * self.height


@dataclass(frozen=True)
class DuplicateImageGroup:
    """Images determined to represent one logical picture."""

    logical_key: str
    images: tuple[ImageCandidate, ...]


@dataclass(frozen=True)
class ProcessedImage:
    """A standardized gallery image ready for upload."""

    source_path: Path
    path: Path
    gallery_slug: str
    logical_key: str
    width: int
    height: int
    size_bytes: int

    @property
    def pixel_area(self) -> int:
        """Return the processed image's total pixel area."""

        return self.width * self.height


@dataclass(frozen=True)
class ValidationIssue:
    """A preflight or runtime validation problem."""

    message: str
    slug: Optional[str] = None
    path: Optional[Path] = None


@dataclass(frozen=True)
class ClassifiedGalleryImages(Generic[ImageType]):
    """Mutually exclusive hero, special-feature, and gallery image groups."""

    hero_image: Optional[ImageType]
    special_features_picture: Optional[ImageType]
    gallery_images: tuple[ImageType, ...]


@dataclass(frozen=True)
class GalleryPhotoRecord:
    """One clickable photo belonging to a parent TOD gallery."""

    name: str
    slug: str
    gallery_slug: str
    image_filename: str
    image_path: str
    image_url: str
    destination_url: str
    alt_text: str
    caption: str
    description: str
    sort_order: int
    open_in_new_tab: bool
    original_image_url: str
    original_destination_url: str
    original_filename: str
    parse_method: str
    parse_warning: str
    source_row: int = 0
    date_or_century: str = ""
    location: str = ""
    material: str = ""
    dimensions: str = ""
    museum_or_collection: str = ""
    institution_or_owner: str = ""
    accession_number: str = ""
