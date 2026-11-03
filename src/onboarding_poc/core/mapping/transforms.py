"""Transformation library and expression engine.

Mappings that are more than "copy a column" are written as expressions over source columns:

    round([sales value ty] / nullif([sales volume ty], 0), 2)
    iif([promo indicator] == "Y", 1, 0)
    concat(upper(trim([store id])), "_Tesco")
    format_date(to_date([calendar date], "dd-MM-yyyy"), "yyyyMMdd")
    case([channel] == "PFS", "Fuel", [channel] == "EXP", "Convenience", "Other")

[column name] refers to a source column (any spelling: 'Sales Value TY', 'sales_value_ty').
Expressions are parsed with Python's ast module and only whitelisted nodes and functions
run, so a mapping can never execute arbitrary code. Every function works column-wise on
pandas Series, so a mapping runs over millions of rows without Python loops where possible.

The same function names are what Veritas operators map onto (config/veritas_operators.yaml),
so adding a Veritas operator is a config change, not a code change.
"""
from __future__ import annotations

import ast
import json
import math
import operator
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable

import numpy as np
import pandas as pd

_REF = re.compile(r"\[([^\[\]]+)\]")
_JAVA = [("yyyy", "%Y"), ("MMMM", "%B"), ("MMM", "%b"), ("EEEE", "%A"), ("EEE", "%a"), ("yy", "%y"),
         ("MM", "%m"), ("dd", "%d"), ("HH", "%H"), ("mm", "%M"), ("ss", "%S"), ("d", "%d"), ("M", "%m")]


class ExpressionError(ValueError):
    pass


def java_fmt(fmt: str) -> str:
    """'dd-MM-yyyy' -> '%d-%m-%Y'; strftime formats pass through unchanged."""
    if "%" in fmt:
        return fmt
    out, i = "", 0
    while i < len(fmt):
        for j, s in _JAVA:
            if fmt.startswith(j, i):
                out += s
                i += len(j)
                break
        else:
            out += fmt[i]
            i += 1
    return out


# ── helpers ─────────────────────────────────────────────────────────────────

def _is_series(x) -> bool:
    return isinstance(x, pd.Series)


def _num(x):
    if _is_series(x):
        if pd.api.types.is_numeric_dtype(x):
            return x.astype(float)
        s = x.astype("string").str.strip()
        neg = s.str.match(r"^\(.*\)$", na=False)
        s = s.str.replace(r"[^\d.\-eE]", "", regex=True)
        out = pd.to_numeric(s, errors="coerce")
        return out.where(~neg, -out)
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return np.nan
    if isinstance(x, (int, float, np.number)):
        return float(x)
    s = str(x).strip()
    neg = s.startswith("(") and s.endswith(")")
    try:
        v = float(re.sub(r"[^\d.\-eE]", "", s))
    except ValueError:
        return np.nan
    return -v if neg else v


def _str(x):
    if _is_series(x):
        return x.astype("string")
    return None if x is None else str(x)


def _null(x):
    if _is_series(x):
        return x.isna()
    return x is None or (isinstance(x, float) and math.isnan(x))


def _where(cond, a, b, index):
    cond = cond if _is_series(cond) else pd.Series([bool(cond)] * len(index), index=index)
    a = a if _is_series(a) else pd.Series([a] * len(index), index=index, dtype=object)
    b = b if _is_series(b) else pd.Series([b] * len(index), index=index, dtype=object)
    return a.astype(object).where(cond.fillna(False).astype(bool), b.astype(object))


def _to_date(x, fmt: str | None = None):
    f = java_fmt(fmt) if fmt else None

    def one(v):
        if v is None or (isinstance(v, float) and math.isnan(v)) or v is pd.NA:
            return None
        if isinstance(v, (date, datetime)):
            return v if isinstance(v, date) and not isinstance(v, datetime) else v.date()
        s = str(v).split("T")[0].strip()
        try:
            return datetime.strptime(s, f).date() if f else pd.to_datetime(s, dayfirst=True).date()
        except (ValueError, TypeError):
            return None
    return x.map(one) if _is_series(x) else one(x)


def _format_date(x, fmt: str):
    f = java_fmt(fmt)

    def one(d):
        return d.strftime(f) if isinstance(d, (date, datetime)) else None
    return x.map(one) if _is_series(x) else one(x)


def _elementwise(fn: Callable, *args):
    """Apply a scalar function row by row when any argument is a Series."""
    series = [a for a in args if _is_series(a)]
    if not series:
        return fn(*args)
    idx = series[0].index
    cols = [a if _is_series(a) else pd.Series([a] * len(idx), index=idx, dtype=object) for a in args]
    frame = pd.concat(cols, axis=1, ignore_index=True)
    return frame.apply(lambda r: fn(*[None if (isinstance(v, float) and math.isnan(v)) or v is pd.NA else v
                                      for v in r]), axis=1)


def _concat(*args):
    def one(*vals):
        if any(v is None for v in vals):
            return None
        return "".join(str(v) for v in vals)
    return _elementwise(one, *args)


def _coalesce(*args):
    out = None
    for a in args:
        if out is None:
            out = a.astype(object) if _is_series(a) else a
            continue
        if _is_series(out):
            out = out.where(~out.isna(), a if not _is_series(a) else a.astype(object))
        elif _null(out):
            out = a
    return out


def _case(*args):
    """case(cond1, value1, cond2, value2, ..., default)"""
    if len(args) < 3 or len(args) % 2 == 0:
        raise ExpressionError("case() takes condition/value pairs and a default")
    default = args[-1]
    result = default
    pairs = list(zip(args[0:-1:2], args[1:-1:2]))
    for cond, val in reversed(pairs):
        idx = next((a.index for a in (cond, val, result) if _is_series(a)), None)
        if idx is None:
            result = val if cond else result
        else:
            result = _where(cond, val, result, idx)
    return result


def _week_ending(x, weekday: str = "Saturday"):
    days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    target = days.index(weekday.lower())

    def one(d):
        if not isinstance(d, date):
            return None
        return d + timedelta(days=(target - d.weekday()) % 7)
    return x.map(one) if _is_series(x) else one(x)


def _round(x, n=0):
    n = int(n)
    return _num(x).round(n) if _is_series(x) else (round(_num(x), n) if not _null(x) else None)


def _nullif(x, v):
    if _is_series(x):
        return x.where(x.astype(object) != v, None) if not pd.api.types.is_numeric_dtype(x) \
            else x.where(x != _num(v))
    return None if x == v else x


def _strfn(fn: Callable) -> Callable:
    def wrapped(x, *a):
        if _is_series(x):
            return fn(_str(x).str, *a)
        return None if x is None else fn(pd.Series([str(x)]).str, *a).iloc[0]
    return wrapped


def _div(a, b):
    b = _num(b)
    if _is_series(b):
        b = b.replace(0, np.nan)
    elif b == 0:
        b = np.nan
    return _num(a) / b


@dataclass
class Fn:
    impl: Callable
    min_args: int
    max_args: int | None
    category: str
    doc: str


FUNCTIONS: dict[str, Fn] = {
    # columns and constants (used when converting Veritas trees)
    "col": Fn(lambda x: x, 1, 1, "source", "column value"),
    "text": Fn(lambda x: x, 1, 1, "source", "constant text"),
    # arithmetic
    "add": Fn(lambda *a: sum(_num(x) for x in a), 2, None, "math", "a + b + ..."),
    "sub": Fn(lambda a, b: _num(a) - _num(b), 2, 2, "math", "a - b"),
    "mul": Fn(lambda *a: math.prod(_num(x) for x in a) if not any(_is_series(x) for x in a)
              else _mul_series(a), 2, None, "math", "a * b * ..."),
    "div": Fn(_div, 2, 2, "math", "a / b (empty when b is 0)"),
    "mod": Fn(lambda a, b: _num(a) % _num(b), 2, 2, "math", "remainder"),
    "power": Fn(lambda a, b: _num(a) ** _num(b), 2, 2, "math", "a to the power b"),
    "neg": Fn(lambda a: -_num(a), 1, 1, "math", "-a"),
    "round": Fn(_round, 1, 2, "math", "round(x, digits)"),
    "abs": Fn(lambda x: _num(x).abs() if _is_series(x) else abs(_num(x)), 1, 1, "math", "absolute value"),
    "floor": Fn(lambda x: np.floor(_num(x)), 1, 1, "math", "round down"),
    "ceil": Fn(lambda x: np.ceil(_num(x)), 1, 1, "math", "round up"),
    "sqrt": Fn(lambda x: np.sqrt(_num(x)), 1, 1, "math", "square root"),
    "min_of": Fn(lambda *a: _rowwise(np.nanmin, a), 2, None, "math", "smallest of the arguments"),
    "max_of": Fn(lambda *a: _rowwise(np.nanmax, a), 2, None, "math", "largest of the arguments"),
    "sum_of": Fn(lambda *a: _rowwise(np.nansum, a), 1, None, "math", "sum ignoring empty values"),
    # conversion
    "to_number": Fn(_num, 1, 1, "convert", "text to number ('£1,234.50' -> 1234.5, '(12)' -> -12)"),
    "to_text": Fn(_str, 1, 1, "convert", "value as text"),
    "to_int": Fn(lambda x: np.trunc(_num(x)), 1, 1, "convert", "drop decimals"),
    # empty values
    "coalesce": Fn(_coalesce, 2, None, "null", "first non-empty argument"),
    "nullif": Fn(_nullif, 2, 2, "null", "empty when x equals value"),
    "is_null": Fn(_null, 1, 1, "null", "true when empty"),
    "if_null": Fn(lambda x, v: _coalesce(x, v), 2, 2, "null", "x, or v when x is empty"),
    # logic
    "iif": Fn(lambda c, a, b: _where(c, a, b, c.index) if _is_series(c) else (a if c else b), 3, 3, "logic",
              "iif(condition, value_if_true, value_if_false)"),
    "case": Fn(_case, 3, None, "logic", "case(cond1, v1, cond2, v2, ..., default)"),
    "in_list": Fn(lambda x, *v: x.isin(v) if _is_series(x) else x in v, 2, None, "logic", "x is one of the values"),
    # text
    "concat": Fn(_concat, 1, None, "text", "join values (empty if any part is empty)"),
    "upper": Fn(_strfn(lambda s: s.upper()), 1, 1, "text", "upper case"),
    "lower": Fn(_strfn(lambda s: s.lower()), 1, 1, "text", "lower case"),
    "trim": Fn(_strfn(lambda s: s.strip()), 1, 1, "text", "strip spaces"),
    "length": Fn(_strfn(lambda s: s.len()), 1, 1, "text", "number of characters"),
    "substr": Fn(_strfn(lambda s, start, n=None: s.slice(int(start) - 1, None if n is None else int(start) - 1 + int(n))),
                 2, 3, "text", "substr(x, start (1-based), length)"),
    "left": Fn(_strfn(lambda s, n: s.slice(0, int(n))), 2, 2, "text", "first n characters"),
    "right": Fn(_strfn(lambda s, n: s.slice(-int(n))), 2, 2, "text", "last n characters"),
    "replace": Fn(_strfn(lambda s, a, b: s.replace(a, b, regex=False)), 3, 3, "text", "replace text"),
    "regex_replace": Fn(_strfn(lambda s, p, b: s.replace(p, b, regex=True)), 3, 3, "text", "replace by pattern"),
    "regex_extract": Fn(_strfn(lambda s, p, g=1: s.extract(p, expand=True)[int(g) - 1]), 2, 3, "text",
                        "capture group from a pattern"),
    "lpad": Fn(_strfn(lambda s, n, ch="0": s.pad(int(n), side="left", fillchar=ch)), 2, 3, "text", "pad on the left"),
    "rpad": Fn(_strfn(lambda s, n, ch=" ": s.pad(int(n), side="right", fillchar=ch)), 2, 3, "text", "pad on the right"),
    "split_part": Fn(_strfn(lambda s, sep, n: s.split(sep).str[int(n) - 1]), 3, 3, "text", "n-th part (1-based)"),
    # dates (formats in Java style like Veritas: yyyy-MM-dd, dd/MM/yyyy, yyyyMMdd)
    "to_date": Fn(_to_date, 1, 2, "date", "to_date(x, 'dd-MM-yyyy')"),
    "format_date": Fn(_format_date, 2, 2, "date", "format_date(date, 'yyyyMMdd')"),
    "reformat_date": Fn(lambda x, cur, tgt: _format_date(_to_date(x, cur), tgt), 3, 3, "date",
                        "reformat_date(x, 'yyyy-MM-dd', 'yyyyMMdd')"),
    "add_days": Fn(lambda d, n: _elementwise(lambda a, b: a + timedelta(days=int(b)) if a else None, d, n), 2, 2,
                   "date", "date plus n days"),
    "week_ending": Fn(_week_ending, 1, 2, "date", "roll a date forward to the week-end day"),
    "year": Fn(lambda d: _elementwise(lambda a: a.year if a else None, d), 1, 1, "date", "year"),
    "month": Fn(lambda d: _elementwise(lambda a: a.month if a else None, d), 1, 1, "date", "month"),
    "day": Fn(lambda d: _elementwise(lambda a: a.day if a else None, d), 1, 1, "date", "day of month"),
    "iso_week": Fn(lambda d: _elementwise(lambda a: a.isocalendar()[1] if a else None, d), 1, 1, "date", "ISO week"),
}


def _mul_series(args):
    out = None
    for a in args:
        v = _num(a)
        out = v if out is None else out * v
    return out


def _rowwise(fn, args):
    if not any(_is_series(a) for a in args):
        return float(fn([_num(a) for a in args]))
    idx = next(a.index for a in args if _is_series(a))
    mat = np.column_stack([(_num(a).to_numpy(dtype=float) if _is_series(a) else np.full(len(idx), _num(a)))
                           for a in args])
    with np.errstate(all="ignore"):
        return pd.Series(fn(mat, axis=1), index=idx)


_BIN = {ast.Add: "add", ast.Sub: "sub", ast.Mult: "mul", ast.Div: "div", ast.Mod: "mod", ast.Pow: "power"}
_CMP = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt, ast.LtE: operator.le,
        ast.Gt: operator.gt, ast.GtE: operator.ge}
_NAMES = {"null": None, "none": None, "true": True, "false": False}


def parse(expr: str) -> ast.Expression:
    src = _REF.sub(lambda m: f"col({json.dumps(m.group(1))})", expr.strip())
    try:
        tree = ast.parse(src, mode="eval")
    except SyntaxError as exc:
        raise ExpressionError(f"syntax error: {exc.msg}") from exc
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS:
                raise ExpressionError(f"unknown function {ast.unparse(node.func)}")
            if node.keywords:
                raise ExpressionError("use positional arguments only")
            f = FUNCTIONS[node.func.id]
            n = len(node.args)
            if n < f.min_args or (f.max_args is not None and n > f.max_args):
                raise ExpressionError(f"{node.func.id}() takes {f.min_args}"
                                      + (f"-{f.max_args}" if f.max_args != f.min_args else "") + f" arguments, got {n}")
        elif isinstance(node, ast.Name):
            if node.id.lower() not in _NAMES and node.id not in FUNCTIONS:
                raise ExpressionError(f"unknown name '{node.id}': wrap column names in [brackets]")
        elif not isinstance(node, (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Compare, ast.BoolOp, ast.Constant,
                                   ast.Load, ast.operator, ast.unaryop, ast.cmpop, ast.boolop)):
            raise ExpressionError(f"not allowed in a mapping: {type(node).__name__}")
    return tree


def columns_used(expr: str) -> list[str]:
    return list(dict.fromkeys(_REF.findall(expr)))


def evaluate(expr: str, frame: pd.DataFrame, resolve: Callable[[str], str]) -> pd.Series:
    """Evaluate over a source frame. `resolve` maps a [column] reference to the frame's column."""
    tree = parse(expr)
    idx = frame.index

    def ev(n) -> Any:
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant):
            return n.value
        if isinstance(n, ast.Name):
            return _NAMES.get(n.id.lower())
        if isinstance(n, ast.Call):
            name = n.func.id
            args = [ev(a) for a in n.args]
            if name == "col":
                key = resolve(args[0])
                if key not in frame.columns:
                    raise ExpressionError(f"column [{args[0]}] not in file")
                return frame[key]
            return FUNCTIONS[name].impl(*args)
        if isinstance(n, ast.BinOp):
            return FUNCTIONS[_BIN[type(n.op)]].impl(ev(n.left), ev(n.right))
        if isinstance(n, ast.UnaryOp):
            v = ev(n.operand)
            if isinstance(n.op, ast.USub):
                return -_num(v)
            if isinstance(n.op, ast.Not):
                return ~v.fillna(False).astype(bool) if _is_series(v) else not v
            return v
        if isinstance(n, ast.Compare):
            left = ev(n.left)
            result = None
            for op, right_node in zip(n.ops, n.comparators):
                right = ev(right_node)
                l2, r2 = left, right
                if isinstance(right, (int, float)) or isinstance(left, (int, float)):
                    l2, r2 = _num(left), _num(right)
                elif _is_series(left) and not _is_series(right) and right is not None:
                    l2 = _str(left)
                part = _CMP[type(op)](l2, r2)
                result = part if result is None else (result & part)
                left = right
            return result
        if isinstance(n, ast.BoolOp):
            vals = [ev(v) for v in n.values]
            out = vals[0]
            for v in vals[1:]:
                if isinstance(n.op, ast.And):
                    out = (out & v) if (_is_series(out) or _is_series(v)) else (out and v)
                else:
                    out = (out | v) if (_is_series(out) or _is_series(v)) else (out or v)
            return out
        raise ExpressionError(f"cannot evaluate {type(n).__name__}")

    out = ev(tree)
    if not _is_series(out):
        out = pd.Series([out] * len(idx), index=idx, dtype=object)
    return out.astype(object).where(out.notna(), None)


def catalogue() -> list[dict]:
    """Function list for the workbench and for LLM prompts."""
    return [{"name": k, "category": f.category, "doc": f.doc} for k, f in FUNCTIONS.items() if k not in ("col", "text")]
