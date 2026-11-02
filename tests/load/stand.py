"""Real VChat application with measurement wrappers, no replacement dependencies."""

import argparse
import asyncio
import functools
import time
from pathlib import Path

import sqlalchemy as sa
from aiohttp import web

from tests.load.common import loopback_only, write_event


def validate_isolation():
    from vchat.settings import cfg

    if (
        cfg.database_uri
        != "postgresql+asyncpg://xen@127.0.0.1:5432/vchat_loadtest_20261102"
    ):
        raise RuntimeError("Use the dedicated vchat_loadtest_20261102 database")
    if cfg.redis_uri != "redis://127.0.0.1:6379/40":
        raise RuntimeError("Use the dedicated Redis DB 40")
    if (cfg.gigachat_base_url, cfg.gigachat_oauth_url, cfg.gigachat_api_key) != (
        "http://127.0.0.1:18192/api/v1",
        "http://127.0.0.1:18192/oauth",
        "loadtest-only",
    ):
        raise RuntimeError("GigaChat must point exclusively to the local emulator")
    if not cfg.openai_guardrails_enabled or not cfg.guardrails_ru_pii_enabled:
        raise RuntimeError("Guardrails must stay enabled")


async def seed():
    from jobs.embedder.client import embed_texts
    from vchat.db import async_session_factory, engine
    from vchat.models import Chat, Chunk, Page, Source, WidgetIntegration

    async with async_session_factory() as db:
        for model in (Chat, Chunk, Page, Source, WidgetIntegration):
            if await db.scalar(sa.select(sa.func.count()).select_from(model)):
                raise RuntimeError(
                    "Seed requires empty test tables; never delete existing data"
                )
        source = Source(
            title="Синтетический учебный корпус",
            uri="https://example.invalid/loadtest/",
        )
        db.add(source)
        db.add(
            WidgetIntegration(
                name="Load test", code="loadtest", suggestions_enabled=True
            )
        )
        await db.flush()
        texts = [
            f"Учебная программа «Команда {ix}». Участники развивают навыки командной работы, обсуждают задания и получают обратную связь преподавателя. Занятия проходят еженедельно, материалы доступны в личном кабинете. Для регистрации нужно выбрать программу и заполнить заявку."
            for ix in range(24)
        ]
        vectors = await asyncio.to_thread(embed_texts, texts)
        for ix, (text, vector) in enumerate(zip(texts, vectors, strict=True)):
            page = Page(
                uri=f"https://example.invalid/loadtest/{ix}",
                source_id=source.id,
                content=text,
                title=f"Учебная программа Команда {ix}",
                status="ready",
                meta={},
            )
            page.hash_value = text
            db.add(page)
            await db.flush()
            db.add(
                Chunk(
                    page_id=page.id,
                    user_uid="loadtest",
                    chunk_ix=0,
                    text=text,
                    embedding=vector,
                    kind="text",
                )
            )
        db.add_all(
            [
                Chat(id=f"loadtest-{ix:04d}", title="Load test", user_uid="loadtest")
                for ix in range(1000)
            ]
        )
        await db.commit()
    await engine.dispose()


async def instrumented_app(events: Path):
    from vchat.app import create_app
    from vchat.views.chat import ctx, views

    active = 0
    peak = 0
    original_handle = views.handle_chat_message

    @functools.wraps(original_handle)
    async def handle(**kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        chat_id = ctx.chat_id_ctx.get()
        write_event(events, "request_start", active=active, peak=peak, chat_id=chat_id)
        try:
            return await original_handle(**kwargs)
        finally:
            active -= 1
            write_event(events, "request_end", active=active, chat_id=chat_id)

    views.handle_chat_message = handle
    original_stage = views.ChatRequestStats.finish_stage

    def finish_stage(self, name, started_at):
        original_stage(self, name, started_at)
        write_event(
            events,
            "stage",
            chat_id=ctx.chat_id_ctx.get(),
            name=name,
            ms=self.stage_durations_ms[name],
        )

    views.ChatRequestStats.finish_stage = finish_stage
    original_rerank = ctx.crossrerank

    def rerank(query, snippets):
        started = time.monotonic()
        result = original_rerank(query, snippets)
        write_event(
            events,
            "rerank",
            chat_id=ctx.chat_id_ctx.get(),
            candidates=len(snippets),
            ms=(time.monotonic() - started) * 1000,
        )
        return result

    ctx.crossrerank = rerank
    for name in (
        "record_request_embedding_encode",
        "record_request_embedding_queue_wait",
    ):
        original = getattr(ctx, name)

        def record(value, event_name=name, callback=original):
            callback(value)
            write_event(
                events, event_name, chat_id=ctx.chat_id_ctx.get(), seconds=value
            )

        setattr(ctx, name, record)
    app = await create_app()

    async def stats(request):
        del request
        return web.json_response({"active": active, "peak": peak})

    app.router.add_get("/__load/stats", stats)
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("seed", "serve"))
    parser.add_argument("--events", type=Path)
    args = parser.parse_args()
    loopback_only()
    validate_isolation()
    if args.mode == "seed":
        asyncio.run(seed())
    else:
        if args.events is None:
            parser.error("serve requires --events")
        web.run_app(
            instrumented_app(args.events),
            host="127.0.0.1",
            port=18193,
            access_log=None,
            shutdown_timeout=1,
        )
