"""Request-size limit: bodies over the limit are rejected with 413 and the standard envelope.

Pure ASGI so it works before routing and for streamed bodies: a declared Content-Length over the
limit is refused immediately; without one (chunked) the bytes are counted as the handler reads
them and the request is aborted as soon as the limit is crossed."""

import json

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from supplier_reliability.api.errors import envelope

DEFAULT_MAX_BYTES = 1024 * 1024


class _BodyTooLarge(Exception):
    pass


class RequestSizeLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None:
            try:
                too_big = int(declared) > self.max_bytes
            except ValueError:
                too_big = False  # malformed header: let the server layer deal with it
            if too_big:
                await self._reject(send)
                return

        received = 0
        exceeded = False
        started = False

        async def counting_receive() -> Message:
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    exceeded = True
                    raise _BodyTooLarge
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if exceeded and not started:
                return  # whatever the app made of the aborted read is replaced by the 413
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except Exception:
            # frameworks may wrap the aborted read (e.g. FastAPI turns body errors into a 400)
            if not exceeded or started:
                raise
        if exceeded and not started:
            await self._reject(send)

    async def _reject(self, send: Send) -> None:
        body = json.dumps(
            envelope(
                "payload_too_large", f"Request body exceeds the {self.max_bytes} byte limit", None
            )
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
