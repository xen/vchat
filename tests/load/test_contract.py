"""Contract checks: real SSE framing, explicit unsupported protocols, percentiles."""

import json

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from tests.load import emulator
from tests.load.common import loopback_only
from tests.load.run import percentiles


@pytest.mark.asyncio
async def test_emulator_oauth_sse_and_unsupported(tmp_path, monkeypatch):
    monkeypatch.setattr(emulator, "INITIAL_DELAY", 0)
    monkeypatch.setattr(emulator, "CHUNK_DELAY", 0)
    events = tmp_path / "events.jsonl"
    async with TestServer(emulator.create_emulator(events)) as server:
        async with aiohttp.ClientSession() as client:
            async with client.post(
                server.make_url("/oauth"),
                headers={"Authorization": "Basic loadtest-only"},
            ) as response:
                assert response.status == 200
                assert (await response.json())["access_token"] == "loadtest-bearer"
            headers = {"Authorization": "Bearer loadtest-bearer"}
            async with client.post(
                server.make_url("/api/v1/chat/completions"),
                headers=headers,
                json={"model": "GigaChat-2", "stream": True, "messages": []},
            ) as response:
                body = await response.text()
                chunks = [
                    line[6:] for line in body.splitlines() if line.startswith("data: ")
                ]
                assert chunks[-1] == "[DONE]"
                content = "".join(
                    json.loads(chunk)["choices"][0]["delta"]["content"]
                    for chunk in chunks[:-2]
                )
                assert content == emulator.RESPONSE
            for path, payload in (
                ("/api/v1/moderations", {}),
                ("/api/v1/responses", {}),
                ("/api/v1/chat/completions", {"stream": False}),
            ):
                async with client.post(
                    server.make_url(path), headers=headers, json=payload
                ) as response:
                    assert response.status == 501
                    assert (await response.json())["error"][
                        "type"
                    ] == "unsupported_protocol"
    assert (
        sum(
            json.loads(line)["event"] == "unsupported"
            for line in events.read_text().splitlines()
        )
        == 3
    )


def test_empty_percentiles_remain_missing():
    assert percentiles([]) == {"p50": None, "p90": None, "p95": None}
    assert percentiles(list(range(1, 101))) == {"p50": 50, "p90": 90, "p95": 95}


def test_network_guard_rejects_external_before_connect(monkeypatch):
    import socket

    # Restore methods with pytest's monkeypatch after installing the guard.
    monkeypatch.setattr(socket.socket, "connect", socket.socket.connect)
    monkeypatch.setattr(socket.socket, "connect_ex", socket.socket.connect_ex)
    loopback_only()
    with socket.socket() as sock:
        with pytest.raises(RuntimeError, match="prohibits external"):
            sock.connect(("203.0.113.1", 443))
        with pytest.raises(RuntimeError, match="prohibits external"):
            sock.connect_ex(("203.0.113.1", 443))
