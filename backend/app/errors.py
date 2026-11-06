"""One error format for every failure: {"error": {"code": ..., "message": ...}}.

`code` is for program logic (an MCP server can branch on it), and `message` is a full
sentence for the agent to read and relay. Handlers below convert FastAPI's own errors,
and unexpected crashes, into the same shape.
"""

import logging
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

ErrorCode = Literal[
    "unauthorized",
    "not_found",
    "method_not_allowed",
    "already_refunded",
    "refund_pending_approval",
    "idempotency_key_reused",
    "validation_error",
    "policy_violation",
    "server_misconfigured",
    "internal_error",
    "http_error",  # any other HTTP error raised by FastAPI or Starlette itself
]


class FieldError(BaseModel):
    location: str  # e.g. "body.amount_paise" or "header.idempotency-key"
    message: str


class ErrorBody(BaseModel):
    code: ErrorCode
    message: str
    fields: list[FieldError] | None = None  # only for validation_error


class ErrorResponse(BaseModel):
    error: ErrorBody


class ApiError(Exception):
    """Raise this from endpoints and dependencies to send an error in the standard format."""

    def __init__(self, status_code: int, code: ErrorCode, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def error_response(status_code: int, body: ErrorBody) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=ErrorResponse(error=body).model_dump(exclude_none=True),
    )


async def handle_api_error(request: Request, error: ApiError) -> JSONResponse:
    return error_response(error.status_code, ErrorBody(code=error.code, message=error.message))


async def handle_validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
    fields = [
        FieldError(location=".".join(str(part) for part in item["loc"]), message=item["msg"])
        for item in error.errors()
    ]
    summary = "; ".join(f"{field.location}: {field.message}" for field in fields)
    body = ErrorBody(
        code="validation_error", message=f"The request is invalid: {summary}", fields=fields
    )
    return error_response(422, body)


async def handle_http_error(request: Request, error: StarletteHTTPException) -> JSONResponse:
    # Errors FastAPI raises itself, such as 404 for an unknown URL or 405 for a wrong method.
    # Its own messages ("Not Found") are terse, so say what was wrong in a full sentence.
    path = request.url.path
    if error.status_code == 404:
        code, message = "not_found", f"There is no endpoint at {path}."
    elif error.status_code == 405:
        code, message = "method_not_allowed", f"{request.method} is not allowed on {path}."
    else:
        code, message = "http_error", str(error.detail)
    return error_response(error.status_code, ErrorBody(code=code, message=message))


async def handle_unexpected_error(request: Request, error: Exception) -> JSONResponse:
    # Log the full stack trace for the operator; never send it to the client.
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    body = ErrorBody(
        code="internal_error", message="An unexpected server error occurred. Please try again later."
    )
    return error_response(500, body)


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, handle_api_error)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, handle_http_error)
    app.add_exception_handler(Exception, handle_unexpected_error)


# Shown in Swagger for every endpoint, replacing FastAPI's default 422 schema.
ERROR_RESPONSES = {
    401: {"model": ErrorResponse, "description": "Missing or invalid API key"},
    422: {"model": ErrorResponse, "description": "Invalid request, or the policy forbids it"},
    500: {"model": ErrorResponse, "description": "Server error"},
}
