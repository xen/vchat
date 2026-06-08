from typing import Any, Mapping

import msgspec
from aiohttp import web
from multidict import CIMultiDict, CIMultiDictProxy, istr


def encode_json(data: Any) -> bytes:
    return msgspec.json.encode(data)


def json_response(
    data: Any,
    *,
    status: int = 200,
    reason: str | None = None,
    headers: (
        Mapping[str | istr, str] | CIMultiDict[str] | CIMultiDictProxy[str] | None
    ) = None,
    content_type: str = "application/json",
) -> web.Response:
    return web.Response(
        body=encode_json(data),
        status=status,
        reason=reason,
        headers=headers,
        content_type=content_type,
    )
