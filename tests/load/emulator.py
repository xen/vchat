"""GigaChat transport emulator. Unsupported guardrail protocols fail explicitly."""

import argparse
import asyncio
import json
import time
from pathlib import Path

from aiohttp import web

from tests.load.common import write_event

RESPONSE = "Учебная программа помогает развивать навыки командной работы. " * 32
INITIAL_DELAY = 0.5
CHUNK_DELAY = 0.05
CHUNK_CHARS = 64


def create_emulator(events: Path):
    async def oauth(request):
        if request.headers.get("Authorization") != "Basic loadtest-only":
            raise web.HTTPUnauthorized()
        write_event(events, "oauth")
        return web.json_response(
            {
                "access_token": "loadtest-bearer",
                "expires_at": int((time.time() + 3600) * 1000),
            }
        )

    async def completion(request):
        if request.headers.get("Authorization") != "Bearer loadtest-bearer":
            raise web.HTTPUnauthorized()
        payload = await request.json()
        write_event(
            events,
            "completion_start",
            stream=payload.get("stream"),
            model=payload.get("model"),
        )
        if not payload.get("stream"):
            # Do not fabricate classifier JSON or claim to emulate the guardrail model.
            write_event(
                events,
                "unsupported",
                path=request.path,
                protocol="non-stream completion",
            )
            return web.json_response(
                {
                    "error": {
                        "message": "Classifier protocol is outside GigaChat SSE emulator",
                        "type": "unsupported_protocol",
                    }
                },
                status=501,
            )
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        await asyncio.sleep(INITIAL_DELAY)
        for offset in range(0, len(RESPONSE), CHUNK_CHARS):
            chunk = {
                "id": "loadtest",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": payload["model"],
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": RESPONSE[offset : offset + CHUNK_CHARS]},
                        "finish_reason": None,
                    }
                ],
            }
            await response.write(
                ("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode()
            )
            await asyncio.sleep(CHUNK_DELAY)
        await response.write(
            b'data: {"choices":[],"usage":{"total_tokens":512}}\n\ndata: [DONE]\n\n'
        )
        await response.write_eof()
        write_event(
            events, "completion_end", chars=len(RESPONSE), bytes=len(RESPONSE.encode())
        )
        return response

    async def unsupported(request):
        write_event(events, "unsupported", path=request.path, method=request.method)
        return web.json_response(
            {
                "error": {
                    "message": f"Unsupported GigaChat emulator endpoint: {request.path}",
                    "type": "unsupported_protocol",
                }
            },
            status=501,
        )

    app = web.Application()
    app.router.add_post("/oauth", oauth)
    app.router.add_post("/api/v1/chat/completions", completion)
    app.router.add_route("*", "/{path:.*}", unsupported)
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18192)
    args = parser.parse_args()
    web.run_app(
        create_emulator(args.events), host="127.0.0.1", port=args.port, access_log=None
    )
