"""Parse retailer files into clean string DataFrames, one per sheet.

Every cell is kept as a string so codes keep their leading zeros and long EANs are not
turned into floats; the profiler infers types afterwards.

Header detection: Meridian's SFTP connector reads row 1 as the header (pandas default).
Retailer portal exports often have title rows, blank rows and total rows, so each
candidate row in the first 30 is scored on: fill ratio, share of text cells, uniqueness,
type contrast with the rows below, and hits against the semantic catalogue synonyms.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from rapidfuzz import fuzz, process

from onboarding_poc.core.contracts import Catalogue
from onboarding_poc.core.schemas import HeaderDetection
from onboarding_poc.core.text import cell_to_str, normalise_header

_NUMERIC = re.compile(r"^-?[\d,]*\.?\d+(e[+-]?\d+)?$", re.IGNORECASE)
_DATEISH = re.compile(r"^\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}")
_TOTAL = re.compile(r"^(grand\s+)?(sub\s*)?total\b", re.IGNORECASE)


@dataclass
class ParsedSheet:
    sheet: str | None
    frame: pd.DataFrame
    header: HeaderDetection
    fingerprint: str
    file_format: str = "csv"
    delimiter: str | None = None


def _sniff_delimiter(text: str) -> str:
    sample = "\n".join(text.splitlines()[:50])
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        counts = {d: sample.count(d) for d in (",", ";", "\t", "|")}
        return max(counts, key=counts.get)


def _read_raw(data: bytes, filename: str) -> tuple[dict[str | None, pd.DataFrame], str, str | None]:
    """Return ({sheet: raw frame}, file format, delimiter)."""
    ext = Path(filename).suffix.lower()
    if ext in (".xlsx", ".xlsm", ".xls"):
        sheets = pd.read_excel(io.BytesIO(data), sheet_name=None, header=None, dtype=object)
        return {name: df for name, df in sheets.items()}, ext.lstrip("."), None
    if ext == ".parquet":
        df = pd.read_parquet(io.BytesIO(data))
        header = pd.DataFrame([list(df.columns)])
        df.columns = range(len(df.columns))
        return {None: pd.concat([header, df], ignore_index=True)}, "parquet", None
    last_exc: Exception | None = None
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = data.decode(enc)
            sep = _sniff_delimiter(text)
            df = pd.read_csv(io.StringIO(text), header=None, dtype=str, sep=sep, engine="python",
                             keep_default_na=False, skip_blank_lines=False)
            return {None: df}, "csv", sep
        except (UnicodeDecodeError, pd.errors.ParserError) as exc:
            last_exc = exc
    raise ValueError(f"Could not parse {filename}: {last_exc}")


def _to_strings(raw: pd.DataFrame) -> pd.DataFrame:
    out = raw.apply(lambda col: col.map(cell_to_str))
    # keep the original row labels so header_row refers to the row in the file
    return out.dropna(how="all").dropna(axis=1, how="all")


def _is_texty(v: str) -> bool:
    return bool(v) and not _NUMERIC.match(v.replace(" ", "")) and not _DATEISH.match(v)


class HeaderDetector:
    def __init__(self, catalogue: Catalogue, scan_rows: int = 30):
        self.scan_rows = scan_rows
        self.synonyms = sorted({s for t in catalogue.types.values() for s in t.synonyms})

    def _synonym_hit(self, value: str) -> float:
        n = normalise_header(value)
        if not n:
            return 0.0
        if n in self.synonyms:
            return 1.0
        m = process.extractOne(n, self.synonyms, scorer=fuzz.token_sort_ratio)
        return 1.0 if m and m[1] >= 90 else 0.0

    def detect(self, df: pd.DataFrame) -> HeaderDetection:
        n = min(self.scan_rows, len(df))
        if n == 0:
            return HeaderDetection(header_row=0, confidence=0.0, reasons=["empty sheet"])
        body_cols = df.iloc[: min(len(df), 200)].notna().sum()
        width = int((body_cols > 0).sum()) or 1
        best, best_score, best_reasons = 0, -1.0, []
        for r in range(n):
            row = [v for v in df.iloc[r].tolist() if v is not None and v == v]
            if len(row) < max(2, int(0.4 * width)):
                continue
            fill = len(row) / width
            texty = sum(_is_texty(v) for v in row) / len(row)
            unique = len(set(row)) / len(row)
            below = df.iloc[r + 1: r + 21]
            contrast = 0.0
            if len(below):
                cols_with_numbers = 0
                for c in df.columns:
                    vals = [v for v in below[c].tolist() if v is not None and v == v]
                    if vals and sum(not _is_texty(v) for v in vals) / len(vals) > 0.6:
                        cols_with_numbers += 1
                contrast = min(1.0, cols_with_numbers / max(1, width) * 2)
            syn = sum(self._synonym_hit(v) for v in row) / len(row)
            score = 0.2 * fill + 0.2 * texty + 0.1 * unique + 0.15 * contrast + 0.35 * syn
            if score > best_score:
                best, best_score = r, score
                best_reasons = [f"fill={fill:.2f}", f"text={texty:.2f}", f"unique={unique:.2f}",
                                f"type_contrast={contrast:.2f}", f"catalogue_hits={syn:.2f}"]
        titles = []
        for r in range(best):
            vals = [v for v in df.iloc[r].tolist() if v is not None and v == v]
            if vals:
                titles.append(" | ".join(vals))
        return HeaderDetection(header_row=int(df.index[best]), confidence=round(max(0.0, min(1.0, best_score)), 3),
                               title_rows=titles, reasons=best_reasons)


def _unique_headers(values: list) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for i, v in enumerate(values):
        name = (v or "").strip() or f"column_{i + 1}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 1
        out.append(name)
    return out


def template_fingerprint(sheet: str | None, columns: list[str]) -> str:
    """Stable id of a file layout: same headers (any order) + same sheet name -> same template."""
    key = (sheet or "") + "|" + "|".join(sorted(normalise_header(c) for c in columns))
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def parse_file(data: bytes, filename: str, catalogue: Catalogue,
               header_overrides: dict[str | None, int] | None = None) -> list[ParsedSheet]:
    detector = HeaderDetector(catalogue)
    out: list[ParsedSheet] = []
    raw_sheets, fmt, delimiter = _read_raw(data, filename)
    for sheet, raw in raw_sheets.items():
        df = _to_strings(raw)
        if df.empty:
            continue
        if header_overrides and header_overrides.get(sheet) in df.index:
            hd = HeaderDetection(header_row=header_overrides[sheet], confidence=1.0, reasons=["approved mapping"])
        else:
            hd = detector.detect(df)
        pos = df.index.get_loc(hd.header_row)
        headers = _unique_headers(df.iloc[pos].tolist())
        body = df.iloc[pos + 1:].copy()
        body.columns = headers
        # drop trailing total / summary rows
        dropped = 0
        while len(body):
            first = next((v for v in body.iloc[-1].tolist() if v is not None and v == v), "")
            if _TOTAL.match(str(first)):
                body = body.iloc[:-1]
                dropped += 1
            else:
                break
        body = body.dropna(how="all").dropna(axis=1, how="all").reset_index(drop=True)
        hd.footer_rows_dropped = dropped
        out.append(ParsedSheet(sheet=sheet, frame=body, header=hd,
                               fingerprint=template_fingerprint(sheet, list(body.columns)),
                               file_format=fmt, delimiter=delimiter))
    return out
