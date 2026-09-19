"""Small, non-invasive request middleware.

Authentication is enforced at route dependencies so public health endpoints stay
available. This middleware only adds a correlation identifier for diagnostics.
"""

from uuid import uuid4
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request.state.request_id = request.headers.get("x-request-id") or uuid4().hex
        response = await call_next(request)
        response.headers["x-request-id"] = request.state.request_id
        return response
