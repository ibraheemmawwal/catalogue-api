"""RFC 9457 problem responses.

One error shape for the whole service. The rule that makes it useful: `detail`
says what to do differently, not that something went wrong. "Book not found" is
a restatement of the status code; naming the identifier and pointing at search
is an instruction.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

CONTENT_TYPE = "application/problem+json"

# A dereferenceable base would need a docs site to point at; until one exists a
# stable URN is honest, and RFC 9457 explicitly permits a non-resolvable type.
TYPE_BASE = "urn:catalogue-api:problem:"


class ProblemError(Exception):
    """An error that already knows how it should be reported."""

    def __init__(
        self,
        *,
        status_code: int,
        title: str,
        detail: str,
        problem_type: str = "about:blank",
        **extras: Any,
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.title = title
        self.detail = detail
        self.problem_type = problem_type
        self.extras = extras


def not_found(resource: str, identifier: str, *, hint: str) -> ProblemError:
    """A 404 that tells the caller how to find the right identifier."""
    return ProblemError(
        status_code=status.HTTP_404_NOT_FOUND,
        title=f"{resource} not found",
        detail=f"No {resource.lower()} with identifier {identifier!r}. {hint}",
        problem_type=f"{TYPE_BASE}not-found",
    )


def invalid_request(detail: str) -> ProblemError:
    return ProblemError(
        status_code=status.HTTP_400_BAD_REQUEST,
        title="Invalid request",
        detail=detail,
        problem_type=f"{TYPE_BASE}invalid-request",
    )


def problem_response(request: Request, error: ProblemError) -> JSONResponse:
    body: dict[str, Any] = {
        "type": error.problem_type,
        "title": error.title,
        "status": error.status_code,
        "detail": error.detail,
        "instance": str(request.url.path),
        **error.extras,
    }
    return JSONResponse(status_code=error.status_code, content=body, media_type=CONTENT_TYPE)


async def problem_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ProblemError)
    return problem_response(request, exc)


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Render Starlette's own errors in the same shape.

    Without this a 404 from routing looks nothing like a 404 from a handler,
    and a client ends up parsing two error formats from one service.
    """
    assert isinstance(exc, HTTPException)
    return problem_response(
        request,
        ProblemError(
            status_code=exc.status_code,
            title=str(exc.detail),
            detail=str(exc.detail),
        ),
    )


async def validation_handler(request: Request, exc: Exception) -> JSONResponse:
    """Report which parameter was wrong, with its constraint."""
    assert isinstance(exc, RequestValidationError)
    fields = [
        {
            "field": ".".join(str(part) for part in error["loc"][1:]) or str(error["loc"][0]),
            "message": error["msg"],
        }
        for error in exc.errors()
    ]
    named = ", ".join(f"{item['field']} ({item['message']})" for item in fields)
    return problem_response(
        request,
        ProblemError(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            title="Invalid request parameters",
            detail=f"Rejected: {named}.",
            problem_type=f"{TYPE_BASE}invalid-parameters",
            errors=fields,
        ),
    )
