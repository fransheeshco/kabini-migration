"""Pattern-first parsing for structured TOD gallery-photo descriptions."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Iterable

from .description_parser import normalize_description, normalize_text_fragment
from .models import GalleryPhotoRecord

DATE_RE = re.compile(
    r"^(?:(?:early|mid|middle|late)\s+)?\d{1,2}(?:st|nd|rd|th)\s+centur(?:y|ies)"
    r"(?:\s*(?:to|-|–|and)\s*(?:(?:early|mid|middle|late)\s+)?"
    r"\d{1,2}(?:st|nd|rd|th)\s+centur(?:y|ies))?$"
    r"|^(?:circa|ca\.?|c\.)\s*\d{3,4}$"
    r"|^\d{4}(?:s)?$|^undated$|^approximate age unknown$",
    re.IGNORECASE,
)
ACCESSION_RE = re.compile(
    r"^(?:accession(?:ed)?\s*(?:number|no\.?)?|acc\.?\s*no\.?|"
    r"catalog(?:ue)?\s*(?:number|no\.?)?)\s*:?\s*(.*)$",
    re.IGNORECASE,
)
DIMENSION_RE = re.compile(
    r"\b(?:height|width|length|depth|diameter|dimension(?:s)?|"
    r"circumference|circ\.?|overall|with(?:out)?\s+the\s+base|"
    r"\d+(?:\.\d+)?\s*(?:cm|mm|m|inches?|in\.|[”\"]))\b|"
    r"^(?:h|w|l|d)\s*(?:\\([^)]*\\))?\s*:",
    re.IGNORECASE,
)
MATERIAL_RE = re.compile(
    r"\b(?:wood|wooden|ivory|metal|brass|silver|gold|gilded|"
    r"polychrom|painted|oil\s+on|canvas|glass|stone|marble|ceramic|"
    r"textile|fabric|velvet|silk|cotton|linen|wax|jade|relic|"
    r"de\s+(?:tallado|bulto|bastidor)|pasta\s+de\s+madera)\b",
    re.IGNORECASE,
)
MUSEUM_RE = re.compile(
    r"\b(?:museum|collection|archive|heritage\s+(?:site|center|centre|museum))\b",
    re.IGNORECASE,
)
INSTITUTION_RE = re.compile(
    r"\b(?:church|parish|shrine|cathedral|basilica|chapel|convent|"
    r"monastery|college|university|school|foundation|organization|"
    r"organisation|archdiocese|diocese|private\s+collection|owned\s+by|"
    r"donated\s+by|solicited\s+by|house)\b",
    re.IGNORECASE,
)
LOCATION_RE = re.compile(
    r"\b(?:cebu|mandaue|lapu-?lapu|carcar|barili|cordova|panay|"
    r"negros|bohol|leyte|city|province|municipality|barangay)\b",
    re.IGNORECASE,
)
ACCESSION_VALUE_RE = re.compile(
    r"^[A-Z0-9][A-Z0-9./]*(?:-[A-Z0-9./]+)+(?:\s+(?:and|or)\s+"
    r"[A-Z0-9][A-Z0-9./]*(?:-[A-Z0-9./]+)+)?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ParsedPhotoDescription:
    """Structured fields derived without discarding the source description."""

    full_description: str
    date_or_century: str = ""
    location: str = ""
    material: str = ""
    dimensions: str = ""
    museum_or_collection: str = ""
    institution_or_owner: str = ""
    accession_number: str = ""
    parse_method: str = "label-and-pattern"
    parse_warning: str = ""


def _same_title(value: str, photo_name: str) -> bool:
    def key(text: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", text.casefold())

    return bool(key(value)) and key(value) == key(photo_name)


def _join(values: Iterable[str]) -> str:
    return "\n\n".join(value.strip() for value in values if value.strip())


def parse_photo_description(
    description: str,
    photo_name: str = "",
) -> ParsedPhotoDescription:
    """Parse recognizable metadata and warn about every unresolved block."""

    full_description = description
    normalized = normalize_description(description)
    if not normalized:
        return ParsedPhotoDescription(
            full_description=full_description,
            parse_method="missing",
            parse_warning="No photo description was found.",
        )
    blocks = [
        normalize_text_fragment(block)
        for block in re.split(r"\n\s*\n", normalized)
        if normalize_text_fragment(block)
    ]
    if blocks and photo_name and _same_title(blocks[0], photo_name):
        blocks.pop(0)

    values: dict[str, list[str]] = {
        "date_or_century": [],
        "location": [],
        "material": [],
        "dimensions": [],
        "museum_or_collection": [],
        "institution_or_owner": [],
        "accession_number": [],
    }
    unresolved: list[str] = []
    accession_empty_index: int | None = None

    for index, block in enumerate(blocks):
        accession = ACCESSION_RE.match(block)
        if accession:
            value = accession.group(1).strip(" :")
            if value:
                values["accession_number"].append(value)
            else:
                accession_empty_index = index
            continue
        if DATE_RE.match(block):
            values["date_or_century"].append(block)
        elif DIMENSION_RE.search(block):
            values["dimensions"].append(block)
        elif MATERIAL_RE.search(block):
            values["material"].append(block)
        elif MUSEUM_RE.search(block):
            values["museum_or_collection"].append(block)
        elif INSTITUTION_RE.search(block):
            values["institution_or_owner"].append(block)
        elif LOCATION_RE.search(block):
            values["location"].append(block)
        else:
            unresolved.append(block)

    if accession_empty_index is not None and unresolved:
        candidate = unresolved[-1]
        if ACCESSION_VALUE_RE.match(candidate):
            values["accession_number"].append(candidate)
            unresolved.pop()

    warnings: list[str] = []
    if unresolved:
        warnings.append("Unresolved description block(s): " + " | ".join(unresolved))
    if accession_empty_index is not None and not values["accession_number"]:
        warnings.append("Accession label was present without a value.")
    if len(values["museum_or_collection"]) > 1:
        warnings.append("Multiple museum or collection values were preserved.")
    if len(values["institution_or_owner"]) > 1:
        warnings.append("Multiple institution or owner values were preserved.")
    if len(values["accession_number"]) > 1:
        warnings.append("Multiple accession numbers were preserved.")

    return ParsedPhotoDescription(
        full_description=full_description,
        date_or_century=_join(values["date_or_century"]),
        location=_join(values["location"]),
        material=_join(values["material"]),
        dimensions=_join(values["dimensions"]),
        museum_or_collection=_join(values["museum_or_collection"]),
        institution_or_owner=_join(values["institution_or_owner"]),
        accession_number=_join(values["accession_number"]),
        parse_warning=" ".join(warnings),
    )


def enrich_photo_record(record: GalleryPhotoRecord) -> GalleryPhotoRecord:
    """Return a photo record with structured description fields populated."""

    parsed = parse_photo_description(record.description, record.name)
    warnings = " ".join(
        value for value in (record.parse_warning, parsed.parse_warning) if value
    )
    return replace(
        record,
        date_or_century=parsed.date_or_century,
        location=parsed.location,
        material=parsed.material,
        dimensions=parsed.dimensions,
        museum_or_collection=parsed.museum_or_collection,
        institution_or_owner=parsed.institution_or_owner,
        accession_number=parsed.accession_number,
        parse_method=f"{record.parse_method}+{parsed.parse_method}".strip("+"),
        parse_warning=warnings,
    )
