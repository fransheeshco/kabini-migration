"""Image discovery, deduplication, standardization, and validation."""

from __future__ import annotations

import hashlib
import logging
import re
from collections import defaultdict
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

from PIL import Image, ImageOps, UnidentifiedImageError

from .config import (
    ALLOW_IMAGE_UPSCALING,
    DEFAULT_HERO_PATTERN,
    PROCESSED_IMAGES_DIRECTORY,
    SUPPORTED_IMAGE_EXTENSIONS,
    TARGET_IMAGE_HEIGHT,
    TARGET_IMAGE_WIDTH,
)
from .models import (
    ClassifiedGalleryImages,
    DuplicateImageGroup,
    ImageCandidate,
    ProcessedImage,
)

LOGGER = logging.getLogger(__name__)
LEADING_SEQUENCE_PREFIX_RE = re.compile(r"^\d{1,7}[-_ ]+")
DIMENSION_SUFFIX_RE = re.compile(
    r"-(?P<width>\d{2,6})x(?P<height>\d{2,6})$", re.IGNORECASE
)
WORDPRESS_GENERATED_SUFFIX_RE = re.compile(
    r"-(scaled|rotated|edited)$", re.IGNORECASE
)


def normalize_gallery_slug(value: str) -> str:
    """Normalize a gallery or folder slug for exact ownership matching."""

    normalized = re.sub(r"[\s_]+", "-", str(value).casefold().strip())
    return re.sub(r"-+", "-", normalized).strip("-")


def source_path_belongs_to_gallery(path: Path, gallery_slug: str) -> bool:
    """Return whether a source path is contained by its exact gallery folder."""

    expected = normalize_gallery_slug(gallery_slug)
    return bool(expected) and expected in {
        normalize_gallery_slug(part) for part in path.parent.parts
    }


def calculate_pixel_area(width: int, height: int) -> int:
    """Return a width/height pair's pixel area."""

    return width * height


def normalize_image_orientation(image: Image.Image) -> Image.Image:
    """Apply EXIF orientation and return the effective image."""

    return ImageOps.exif_transpose(image)


def get_image_dimensions(path: Path) -> tuple[int, int]:
    """Read effective dimensions after applying EXIF orientation."""

    with Image.open(path) as opened:
        oriented = normalize_image_orientation(opened)
        return int(oriented.width), int(oriented.height)


def strip_extraction_prefix(stem: str) -> str:
    """Remove the extraction tool's ordered filename prefix."""

    return LEADING_SEQUENCE_PREFIX_RE.sub("", stem)


def parse_dimension_suffix(
    stem: str,
) -> tuple[str, Optional[int], Optional[int], bool]:
    """Remove a trailing WordPress WxH suffix and return its dimensions."""

    match = DIMENSION_SUFFIX_RE.search(stem)
    if not match:
        return stem, None, None, False
    return (
        stem[: match.start()],
        int(match.group("width")),
        int(match.group("height")),
        True,
    )


def logical_image_key(path: Path) -> str:
    """Build the conservative filename-family key used by the old script."""

    stem = strip_extraction_prefix(path.stem)
    stem, _, _, _ = parse_dimension_suffix(stem)
    stem = WORDPRESS_GENERATED_SUFFIX_RE.sub("", stem)
    stem = re.sub(r"[\s_]+", "-", stem.casefold().strip())
    return re.sub(r"-+", "-", stem).strip("-")


def is_hero_image(
    path: Path, hero_pattern_text: str = DEFAULT_HERO_PATTERN
) -> bool:
    """Return whether a filename matches the existing G<number> hero rule."""

    pattern = re.compile(hero_pattern_text, re.IGNORECASE)
    return bool(pattern.search(strip_extraction_prefix(path.stem)))


def normalize_reserved_image_stem(path: Path) -> str:
    """Normalize a reserved-image stem independently of its extension."""

    stem = strip_extraction_prefix(path.stem)
    stem, _, _, _ = parse_dimension_suffix(stem)
    stem = WORDPRESS_GENERATED_SUFFIX_RE.sub("", stem)
    stem = re.sub(r"[\s_]+", "-", stem.casefold().strip())
    return re.sub(r"-+", "-", stem).strip("-")


def is_special_features_picture(
    path: Path,
    filename: str = "Special-Features-Picture.png",
) -> bool:
    """Match normalized special-feature filenames, ignoring extension and case."""

    return normalize_reserved_image_stem(path) == normalize_reserved_image_stem(
        Path(filename)
    )


def find_special_features_picture(
    images: Sequence[ImageCandidate],
    *,
    filename: str = "Special-Features-Picture.png",
) -> Optional[ImageCandidate]:
    """Select the highest-resolution normalized special-picture candidate."""

    candidates = [
        image
        for image in images
        if is_special_features_picture(image.path, filename)
    ]
    if not candidates:
        LOGGER.info("No Special Features Picture found.")
        return None
    if len(candidates) > 1:
        LOGGER.warning(
            "Multiple Special Features Picture candidates found: %s",
            ", ".join(str(image.path) for image in candidates),
        )
    selected = select_highest_resolution_image(candidates)
    LOGGER.info(
        "Special Features Picture selected: %s (%sx%s)",
        selected.path,
        selected.width,
        selected.height,
    )
    return selected


def find_hero_image(
    images: Sequence[ImageCandidate],
) -> Optional[ImageCandidate]:
    """Select the highest-resolution candidate matching the hero rule."""

    candidates = [image for image in images if image.is_hero]
    return (
        select_highest_resolution_image(candidates)
        if candidates
        else None
    )


def classify_gallery_images(
    images: Sequence[ImageCandidate],
    *,
    special_features_filename: str = "Special-Features-Picture.png",
) -> ClassifiedGalleryImages[ImageCandidate]:
    """Classify reserved images, then deduplicate the remaining gallery photos."""

    hero = find_hero_image(images)
    special = find_special_features_picture(
        images,
        filename=special_features_filename,
    )
    reserved_paths = {
        image.path
        for image in (hero, special)
        if image is not None
    }
    reserved_keys = {
        image.logical_key
        for image in (hero, special)
        if image is not None
    }
    gallery_candidates = [
        image
        for image in images
        if image.path not in reserved_paths
        and image.logical_key not in reserved_keys
        and not image.is_hero
        and not is_special_features_picture(
            image.path, special_features_filename
        )
    ]
    gallery = select_highest_resolution_duplicates(gallery_candidates)
    classification = ClassifiedGalleryImages(
        hero_image=hero,
        special_features_picture=special,
        gallery_images=tuple(gallery),
    )
    validate_image_classification(classification)
    return classification


def validate_image_classification(
    classification: ClassifiedGalleryImages[ImageCandidate],
) -> None:
    """Reject overlap between hero, special-feature, and gallery categories."""

    gallery_paths = {image.path for image in classification.gallery_images}
    if len(gallery_paths) != len(classification.gallery_images):
        raise ValueError("A gallery image appears more than once.")
    for label, reserved in (
        ("Hero Image", classification.hero_image),
        ("Special Features Picture", classification.special_features_picture),
    ):
        if reserved is not None and reserved.path in gallery_paths:
            raise ValueError(f"{label} also appears in Gallery Images.")
    if (
        classification.hero_image is not None
        and classification.special_features_picture is not None
        and classification.hero_image.path
        == classification.special_features_picture.path
    ):
        raise ValueError(
            "Hero Image and Special Features Picture resolve to the same file."
        )


def read_image_candidate(
    path: Path,
    gallery_slug: str,
    *,
    hero_pattern_text: str = DEFAULT_HERO_PATTERN,
) -> Optional[ImageCandidate]:
    """Read one supported image without allowing a bad file to stop migration."""

    if path.suffix.casefold() not in SUPPORTED_IMAGE_EXTENSIONS:
        LOGGER.warning("Unsupported image file skipped: %s", path)
        return None
    cleaned = strip_extraction_prefix(path.stem)
    _, filename_width, filename_height, has_suffix = parse_dimension_suffix(cleaned)
    try:
        width, height = get_image_dimensions(path)
        size_bytes = path.stat().st_size
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        LOGGER.warning("Unsupported or corrupted image %s: %s", path, exc)
        return None
    LOGGER.info("Original image dimensions: %s (%sx%s)", path, width, height)
    return ImageCandidate(
        path=path,
        gallery_slug=gallery_slug,
        logical_key=logical_image_key(path),
        width=width,
        height=height,
        size_bytes=size_bytes,
        filename_width=filename_width,
        filename_height=filename_height,
        has_dimension_suffix=has_suffix,
        is_hero=is_hero_image(path, hero_pattern_text),
    )


def discover_image_candidates(
    images_root: Path,
    gallery_slug: str,
    *,
    hero_pattern_text: str = DEFAULT_HERO_PATTERN,
) -> list[ImageCandidate]:
    """Discover supported images recursively for one gallery slug."""

    gallery_slug = normalize_gallery_slug(gallery_slug)
    folder = images_root / gallery_slug
    if not folder.is_dir():
        return []
    paths = sorted(
        (
            path
            for path in folder.rglob("*")
            if path.is_file() and path.suffix.casefold() in SUPPORTED_IMAGE_EXTENSIONS
        ),
        key=lambda path: str(path.relative_to(folder)).casefold(),
    )
    return [
        candidate
        for path in paths
        if (
            candidate := read_image_candidate(
                path, gallery_slug, hero_pattern_text=hero_pattern_text
            )
        )
        is not None
    ]


def group_duplicate_images(
    images: Sequence[ImageCandidate],
) -> list[DuplicateImageGroup]:
    """Group only images sharing the established normalized filename key."""

    grouped: dict[str, list[ImageCandidate]] = defaultdict(list)
    for image in images:
        grouped[image.logical_key].append(image)
    groups = [
        DuplicateImageGroup(key, tuple(sorted(values, key=lambda x: str(x.path))))
        for key, values in sorted(grouped.items())
    ]
    for group in groups:
        if len(group.images) > 1:
            LOGGER.info(
                "Detected duplicate group '%s': %s",
                group.logical_key,
                ", ".join(str(image.path) for image in group.images),
            )
    return groups


def select_highest_resolution_image(
    images: Sequence[ImageCandidate],
) -> ImageCandidate:
    """Select by area, width, file size, then alphabetically first path."""

    if not images:
        raise ValueError("Cannot select from an empty image group.")
    return sorted(
        images,
        key=lambda image: (
            -image.pixel_area,
            -image.width,
            -image.size_bytes,
            str(image.path).casefold(),
        ),
    )[0]


def select_highest_resolution_duplicates(
    images: Sequence[ImageCandidate],
) -> list[ImageCandidate]:
    """Keep at most one deterministic winner from every duplicate group."""

    selected: list[ImageCandidate] = []
    for group in group_duplicate_images(images):
        retained = select_highest_resolution_image(group.images)
        selected.append(retained)
        if len(group.images) > 1:
            LOGGER.info(
                "Duplicate group '%s': retained %s (%sx%s)",
                group.logical_key,
                retained.path,
                retained.width,
                retained.height,
            )
            for excluded in group.images:
                if excluded.path != retained.path:
                    LOGGER.info(
                        "Duplicate group '%s': excluded %s (%sx%s)",
                        group.logical_key,
                        excluded.path,
                        excluded.width,
                        excluded.height,
                    )
    return sorted(selected, key=lambda image: str(image.path).casefold())


def crop_image_to_target(
    image: Image.Image,
    *,
    target_width: int = TARGET_IMAGE_WIDTH,
    target_height: int = TARGET_IMAGE_HEIGHT,
) -> Image.Image:
    """Apply orientation and center-crop to exact target dimensions."""

    oriented = normalize_image_orientation(image)
    return ImageOps.fit(
        oriented,
        (target_width, target_height),
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),
    )


def build_processed_image_path(
    output_root: Path,
    gallery_slug: str,
    index: int,
    source_path: Path,
) -> Path:
    """Build a deterministic, collision-resistant output filename."""

    suffix = ".png" if source_path.suffix.casefold() == ".png" else ".jpg"
    digest = hashlib.sha256(
        f"{source_path.resolve()}::{source_path.stat().st_size}".encode()
    ).hexdigest()[:10]
    return output_root / gallery_slug / f"image-{index:03d}-{digest}{suffix}"


def _source_fingerprint(path: Path) -> str:
    stat = path.stat()
    return f"{stat.st_size}:{stat.st_mtime_ns}"


def process_single_image(
    candidate: ImageCandidate,
    output_path: Path,
    *,
    target_width: int = TARGET_IMAGE_WIDTH,
    target_height: int = TARGET_IMAGE_HEIGHT,
    allow_upscaling: bool = ALLOW_IMAGE_UPSCALING,
) -> Optional[ProcessedImage]:
    """Standardize one image, skipping it when disabled upscaling is required."""

    if not allow_upscaling and (
        candidate.width < target_width or candidate.height < target_height
    ):
        LOGGER.warning(
            "Image below target resolution skipped: %s (%sx%s; target %sx%s)",
            candidate.path,
            candidate.width,
            candidate.height,
            target_width,
            target_height,
        )
        return None
    metadata_path = output_path.with_suffix(output_path.suffix + ".source")
    fingerprint = _source_fingerprint(candidate.path)
    if output_path.is_file() and metadata_path.is_file():
        try:
            if metadata_path.read_text(encoding="utf-8") == fingerprint:
                width, height = get_image_dimensions(output_path)
                if (width, height) == (target_width, target_height):
                    LOGGER.info("Reusing processed gallery image: %s", output_path)
                    return ProcessedImage(
                        candidate.path,
                        output_path,
                        candidate.gallery_slug,
                        candidate.logical_key,
                        width,
                        height,
                        output_path.stat().st_size,
                    )
        except OSError:
            pass
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with Image.open(candidate.path) as opened:
            LOGGER.info(
                "Center-cropping %s from %sx%s to %sx%s",
                candidate.path,
                candidate.width,
                candidate.height,
                target_width,
                target_height,
            )
            processed = crop_image_to_target(
                opened, target_width=target_width, target_height=target_height
            )
            if output_path.suffix.casefold() in {".jpg", ".jpeg"}:
                processed = processed.convert("RGB")
                processed.save(output_path, format="JPEG", quality=92, optimize=True)
            else:
                processed.save(output_path, format="PNG", optimize=True)
        metadata_path.write_text(fingerprint, encoding="utf-8")
        width, height = get_image_dimensions(output_path)
        LOGGER.info("Processed image dimensions: %s (%sx%s)", output_path, width, height)
        return ProcessedImage(
            candidate.path,
            output_path,
            candidate.gallery_slug,
            candidate.logical_key,
            width,
            height,
            output_path.stat().st_size,
        )
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        LOGGER.warning("Image processing failed for %s: %s", candidate.path, exc)
        return None


def process_gallery_images(
    images: Sequence[ImageCandidate],
    *,
    output_root: Path = PROCESSED_IMAGES_DIRECTORY,
    target_width: int = TARGET_IMAGE_WIDTH,
    target_height: int = TARGET_IMAGE_HEIGHT,
    allow_upscaling: bool = ALLOW_IMAGE_UPSCALING,
) -> list[ProcessedImage]:
    """Deduplicate and standardize all usable images for a single gallery."""

    selected = select_highest_resolution_duplicates(images)
    outputs: list[ProcessedImage] = []
    used_paths: set[Path] = set()
    for index, candidate in enumerate(selected, start=1):
        output = build_processed_image_path(
            output_root, candidate.gallery_slug, index, candidate.path
        )
        if output in used_paths:
            LOGGER.error("Output filename collision: %s", output)
            continue
        used_paths.add(output)
        processed = process_single_image(
            candidate,
            output,
            target_width=target_width,
            target_height=target_height,
            allow_upscaling=allow_upscaling,
        )
        if processed is not None:
            outputs.append(processed)
    validate_processed_images(
        outputs, target_width=target_width, target_height=target_height
    )
    return outputs


def validate_processed_images(
    images: Sequence[ProcessedImage],
    *,
    target_width: int = TARGET_IMAGE_WIDTH,
    target_height: int = TARGET_IMAGE_HEIGHT,
) -> None:
    """Raise ValueError when processed image invariants are violated."""

    paths: set[Path] = set()
    logical_keys: set[tuple[str, str]] = set()
    target_ratio = target_width / target_height
    for image in images:
        if image.path in paths:
            raise ValueError(f"Duplicate processed output path: {image.path}")
        paths.add(image.path)
        identity = (image.gallery_slug, image.logical_key)
        if identity in logical_keys:
            raise ValueError(
                f"Duplicate image family in gallery {image.gallery_slug}: "
                f"{image.logical_key}"
            )
        logical_keys.add(identity)
        if image.gallery_slug != image.path.parent.name:
            raise ValueError(f"Processed image has wrong gallery directory: {image.path}")
        if not image.path.is_file():
            raise ValueError(f"Processed image does not exist: {image.path}")
        if (image.width, image.height) != (target_width, target_height):
            raise ValueError(f"Processed image has invalid dimensions: {image.path}")
        if abs((image.width / image.height) - target_ratio) > 1e-9:
            raise ValueError(f"Processed image has invalid aspect ratio: {image.path}")
