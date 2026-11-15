# SI-assisted development used for code development, debugging and documentation.
# Tool: ChatGPT, Claude

"""Utilidades comunes: JSON limpio, fechas, parseo de listas en query."""
from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta, timezone
from typing import Iterable

import numpy as np
from fastapi.responses import JSONResponse

from .errors import bad_request

ID_RE = re.compile(r"^[A-Za-z0-9_.:+\-]{1,128}$")


def clean(o):
    """Convierte tipos de numpy y NaN/inf a tipos JSON válidos."""
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    if isinstance(o, np.ndarray):
        return clean(o.tolist())
    if isinstance(o, np.generic):
        o = o.item()
    if isinstance(o, float) and not math.isfinite(o):
        return None
    return o


def json_response(data, status: int = 200, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(clean(data), status_code=status, headers=headers)


def iso_z(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def date_iso(d: date | None) -> str | None:
    return d.isoformat() if d else None


def check_dates(date_from: date | None, date_to: date | None) -> None:
    if date_from and date_to and date_from > date_to:
        raise bad_request("date_from debe ser anterior o igual a date_to")


def parse_list(values: Iterable[str] | None, allowed: set[str], default: list[str], name: str) -> list[str]:
    """Acepta tanto ?x=a&x=b como ?x=a,b."""
    if not values:
        return list(default)
    out: list[str] = []
    for v in values:
        for part in str(v).split(","):
            p = part.strip().lower()
            if not p:
                continue
            if p not in allowed:
                raise bad_request(f"Valor no válido en {name}: '{p}'. Opciones: {', '.join(sorted(allowed))}")
            if p not in out:
                out.append(p)
    return out or list(default)


def split_ids(values: Iterable[str] | None, name: str) -> list[str]:
    out: list[str] = []
    for v in values or []:
        for part in str(v).split(","):
            p = part.strip()
            if not p:
                continue
            if not ID_RE.match(p):
                raise bad_request(f"id no válido en {name}: '{p}'")
            if p not in out:
                out.append(p)
    return out


def val(row, name: str):
    """Lee una columna de una fila de astropy Table devolviendo tipos Python (None si falta)."""
    try:
        v = row[name]
    except (KeyError, IndexError):
        return None
    if v is np.ma.masked or np.ma.is_masked(v):
        return None
    if isinstance(v, bytes):
        v = v.decode("utf-8", "replace")
    if isinstance(v, np.generic):
        v = v.item()
    if isinstance(v, float) and math.isnan(v):
        return None
    if isinstance(v, str):
        v = v.strip()
    return v


def fnum(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())
