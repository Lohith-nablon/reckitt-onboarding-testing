"""Text normalisation helpers shared by discovery, mapping and harmonisation."""
from __future__ import annotations

import math
import re
from datetime import date, datetime
from typing import Any

_CURRENCY_SYMBOLS = {"£": "GBP", "€": "EUR", "$": "USD", "₹": "INR"}
_CURRENCY_CODES = {"gbp", "eur", "usd", "inr", "aed", "sar", "zar", "aud", "cad", "brl", "mxn", "cny", "jpy"}


def normalise_header(h: str) -> str:
    s = str(h).lower()
    for sym in _CURRENCY_SYMBOLS:
        s = s.replace(sym, " ")
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    tokens = [t for t in s.split() if t not in _CURRENCY_CODES]
    return " ".join(tokens)


def canon_header(h: str) -> str:
    """Key used to match column names across spellings of the same header.

    'Sales % Var LW (Vol)', 'sales % var lw (vol)' and 'sales_%_var_lw__vol_' (how the landing
    zone writes it) all become 'sales_%_var_lw_vol'.
    """
    return re.sub(r"[^a-z0-9%]+", "_", str(h).lower()).strip("_")


def currency_in_header(h: str) -> str | None:
    s = str(h)
    for sym, code in _CURRENCY_SYMBOLS.items():
        if sym in s:
            return code
    for tok in re.findall(r"[A-Za-z]{3}", s):
        if tok.lower() in _CURRENCY_CODES:
            return tok.upper()
    return None


def safe_identifier(s: str) -> str:
    name = re.sub(r"[^a-z0-9]+", "_", str(s).lower()).strip("_")
    if name and name[0].isdigit():
        name = f"c_{name}"
    return name or "unnamed"


def cell_to_str(v: Any) -> str | None:
    """Turn a raw cell into a string without losing codes (leading zeros, long EANs)."""
    if v is None:
        return None
    if isinstance(v, float):
        if math.isnan(v):
            return None
        if v.is_integer() and abs(v) < 1e15:
            return str(int(v))
        return repr(v)
    if isinstance(v, (datetime, date)):
        if isinstance(v, datetime) and (v.hour, v.minute, v.second) != (0, 0, 0):
            return v.isoformat()
        return (v.date() if isinstance(v, datetime) else v).isoformat()
    s = str(v).strip()
    if s == "" or s.lower() in {"nan", "nat", "none", "null"}:
        return None
    return s


def gtin_check_ok(code: str) -> bool:
    if not code.isdigit() or len(code) not in (8, 12, 13, 14):
        return False
    digits = [int(d) for d in code]
    body, check = digits[:-1], digits[-1]
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return (10 - total % 10) % 10 == check


def normalise_gtin(raw: str | None) -> str | None:
    """Return a GTIN-14 string if `raw` is a valid GTIN-8/12/13/14, else None.

    Scientific notation (Excel damage, e.g. '5.00016E+12') is rejected: digits are lost.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or "e" in s.lower():
        return None
    if s.endswith(".0"):
        s = s[:-2]
    s = re.sub(r"[\s-]", "", s)
    if not s.isdigit():
        return None
    for candidate in (s, s.zfill(13), s.zfill(14)):
        if gtin_check_ok(candidate):
            return candidate.zfill(14)
    return None
