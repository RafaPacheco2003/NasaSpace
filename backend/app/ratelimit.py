# SI-assisted development used for code development, debugging and documentation.
# Tool: ChatGPT, Claude

"""Límite de peticiones por IP (token bucket) → 429 con Retry-After.

El sondeo de trabajos y las imágenes PNG no cuentan: una secuencia de 60 frames
dispara 60 PNG y el polling de /api/jobs es cada ~800 ms.
"""
from __future__ import annotations

import math
import time

from fastapi import Request

from .config import settings
from .errors import APIError

EXEMPT_PREFIXES = ("/api/jobs/", "/api/frame/", "/api/stack/", "/api/status", "/docs", "/openapi.json", "/healthz", "/redoc")


class TokenBucket:
    def __init__(self, per_min: int, burst: int) -> None:
        self.rate = per_min / 60.0
        self.burst = float(burst)
        self.buckets: dict[str, tuple[float, float]] = {}

    def check(self, ip: str) -> tuple[bool, int]:
        now = time.monotonic()
        if len(self.buckets) > 20000:
            self.buckets = {k: v for k, v in self.buckets.items() if now - v[1] < 300}
        tokens, ts = self.buckets.get(ip, (self.burst, now))
        tokens = min(self.burst, tokens + (now - ts) * self.rate)
        if tokens >= 1:
            self.buckets[ip] = (tokens - 1, now)
            return True, 0
        self.buckets[ip] = (tokens, now)
        return False, max(1, math.ceil((1 - tokens) / self.rate))


limiter = TokenBucket(settings.rate_limit_per_min, settings.rate_limit_burst)


def client_ip(request: Request) -> str:
    if settings.trust_proxy:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def rate_limit_middleware(request: Request, call_next):
    path = request.url.path
    if request.method == "OPTIONS" or settings.rate_limit_per_min <= 0 or not path.startswith("/api/") or path.startswith(EXEMPT_PREFIXES) or path.endswith(".png"):
        return await call_next(request)
    ok, retry = limiter.check(client_ip(request))
    if not ok:
        return APIError(
            429, "rate_limited", f"Demasiadas peticiones desde tu IP. Reintenta en {retry} s.", {"Retry-After": str(retry)}
        ).response()
    return await call_next(request)
