"""Normalisation for product descriptions and outlet addresses."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_SIZE = re.compile(r"(\d+(?:\.\d+)?)\s*(ml|l|ltr|litre|liter|cl|g|gm|gms|gr|kg)\b")
_COUNT = re.compile(
    r"(?:x\s*(\d+)\b)|(\d+)\s*(?:x\b|s\b|n\b|pk\b|pack\b|pcs\b|pc\b|ct\b|count\b|tablets\b|tabs\b|tab\b|"
    r"capsules\b|caps\b|cap\b|wipes\b|lozenges\b|sachets\b)")
_STOP = {"the", "and", "with", "for", "of", "in", "a", "pack", "x", "n", "s", "pcs", "pc", "ml", "g", "l", "ltr",
         "new", "rb"}

_OUTLET_ABBREV = {
    "rd": "road", "st": "street", "ctr": "centre", "cntr": "centre", "center": "centre", "shop": "shopping",
    "pkwy": "parkway", "ave": "avenue", "av": "avenue", "ln": "lane", "hwy": "highway", "exp": "express",
    "ext": "extra", "sup": "superstore", "nr": "near", "opp": "opposite", "mkt": "market",
    "blvd": "boulevard", "sq": "square", "pl": "place",
}


@dataclass
class ProductAttrs:
    text: str                       # normalised description
    tokens: set[str] = field(default_factory=set)
    size: float | None = None       # in ml or g
    size_unit: str | None = None    # ml | g
    count: int | None = None


def _expand(text: str, abbreviations: dict[str, str]) -> str:
    s = f" {text} "
    for k in sorted(abbreviations, key=len, reverse=True):
        s = re.sub(rf"(?<=[\s]){re.escape(k)}(?=[\s])", abbreviations[k], s)
    return s.strip()


def normalise_product(desc: str | None, abbreviations: dict[str, str]) -> ProductAttrs:
    if not desc:
        return ProductAttrs(text="")
    s = str(desc).lower().replace("&", " and ")
    s = re.sub(r"(\d)\s*(ltr|litre|liter|ml|l|cl|kg|gms|gm|gr|g)\b", r"\1 \2", s)
    s = re.sub(r"(\d)(s|n|pk|pcs|pc|tabs|caps)\b", r"\1 \2", s)
    s = re.sub(r"[^a-z0-9.\s]", " ", s)
    s = re.sub(r"\s+", " ", s)
    s = _expand(s, abbreviations)
    size = unit = None
    m = _SIZE.search(s)
    if m:
        v, u = float(m.group(1)), m.group(2)
        if u in ("l", "ltr", "litre", "liter"):
            v, u = v * 1000, "ml"
        elif u == "cl":
            v, u = v * 10, "ml"
        elif u == "kg":
            v, u = v * 1000, "g"
        elif u in ("gm", "gms", "gr", "g"):
            u = "g"
        size, unit = v, u
    count = None
    if size is None:
        c = _COUNT.search(s)
        if c:
            count = int(c.group(1) or c.group(2))
    tokens = {t for t in s.split() if t not in _STOP and not re.fullmatch(r"[\d.]+", t)}
    return ProductAttrs(text=s, tokens=tokens, size=size, size_unit=unit, count=count)


def normalise_address(text: str | None) -> str:
    if not text:
        return ""
    s = re.sub(r"[^a-z0-9\s]", " ", str(text).lower())
    return " ".join(_OUTLET_ABBREV.get(t, t) for t in s.split())


def normalise_postcode(pc: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(pc).upper()) if pc else ""


def outward_code(pc_norm: str) -> str:
    """UK outward code (LS146JD -> LS14); first 3 digits for numeric PIN/ZIP codes."""
    if not pc_norm:
        return ""
    if pc_norm.isdigit():
        return pc_norm[:3]
    return pc_norm[:-3] if len(pc_norm) > 4 else pc_norm
