"""Errores con la forma única del contrato: {"error": {"code", "message"}}."""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("skyapi.errors")

STATUS_CODES = {
    400: "bad_request",
    404: "not_found",
    405: "method_not_allowed",
    429: "rate_limited",
    500: "internal_error",
    502: "upstream_error",
    504: "upstream_timeout",
}


class APIError(Exception):
    def __init__(self, status: int, code: str, message: str, headers: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers

    def payload(self) -> dict:
        return {"code": self.code, "message": self.message}

    def response(self) -> JSONResponse:
        return JSONResponse({"error": self.payload()}, status_code=self.status, headers=self.headers)


class UpstreamError(Exception):
    """Falló una fuente externa (SIMBAD, NED, Gaia, WISE, IRSA, Horizons)."""

    def __init__(self, source: str, message: str, timeout: bool = False):
        super().__init__(f"{source}: {message}")
        self.source = source
        self.message = message
        self.timeout = timeout


def bad_request(message: str) -> APIError:
    return APIError(400, "bad_request", message)


def not_found(message: str) -> APIError:
    return APIError(404, "not_found", message)


def upstream_failure(errors: list[UpstreamError]) -> APIError:
    """Todas las fuentes pedidas fallaron → 504 si todas fueron timeout, 502 si no."""
    detail = "; ".join(f"{e.source}: {e.message}" for e in errors)
    if errors and all(e.timeout for e in errors):
        return APIError(504, "upstream_timeout", f"Se agotó el tiempo en todas las fuentes pedidas ({detail}).")
    return APIError(502, "upstream_error", f"Fallaron todas las fuentes pedidas ({detail}).")


def _validation_message(exc: RequestValidationError) -> str:
    errs = exc.errors()
    if not errs:
        return "Parámetros inválidos."
    e = errs[0]
    loc = [str(x) for x in e.get("loc", []) if x not in ("query", "path", "body")]
    name = ".".join(loc) if loc else "parámetro"
    etype = e.get("type", "")
    ctx = e.get("ctx") or {}
    if etype == "missing":
        return f"Falta el parámetro obligatorio '{name}'."
    def fmt(v):
        return f"{v:g}" if isinstance(v, (int, float)) else str(v)

    if etype == "less_than_equal":
        return f"{name} supera el máximo ({fmt(ctx.get('le'))})"
    if etype == "less_than":
        return f"{name} debe ser menor que {fmt(ctx.get('lt'))}"
    if etype == "greater_than_equal":
        return f"{name} es menor que el mínimo ({fmt(ctx.get('ge'))})"
    if etype == "greater_than":
        return f"{name} debe ser mayor que {fmt(ctx.get('gt'))}"
    if etype == "json_invalid":
        return "El cuerpo de la petición no es JSON válido."
    if etype == "literal_error":
        return f"Valor no válido para '{name}'. Opciones: {ctx.get('expected')}."
    return f"Parámetro '{name}' inválido: {e.get('msg')}"


def install(app: FastAPI) -> None:
    @app.exception_handler(APIError)
    async def _api_error(_: Request, exc: APIError):
        return exc.response()

    @app.exception_handler(UpstreamError)
    async def _upstream(_: Request, exc: UpstreamError):
        return upstream_failure([exc]).response()

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        return bad_request(_validation_message(exc)).response()

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException):
        code = STATUS_CODES.get(exc.status_code, "error")
        msg = exc.detail if isinstance(exc.detail, str) else "Error"
        if exc.status_code == 404 and msg == "Not Found":
            msg = "Ruta no encontrada."
        return APIError(exc.status_code, code, msg, getattr(exc, "headers", None)).response()

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception):
        log.exception("Error no controlado")
        # Los 500 se generan FUERA de CORSMiddleware (Starlette): sin estas cabeceras el navegador mostraría
        # "CORS error" en lugar del mensaje.
        return APIError(500, "internal_error", f"Error interno: {type(exc).__name__}",
                        {"Access-Control-Allow-Origin": "*", "Access-Control-Expose-Headers": "*"}).response()
