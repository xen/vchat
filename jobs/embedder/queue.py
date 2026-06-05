import math
from typing import Any

import sqlalchemy as sa

from vchat.models import Chunk
from vchat.settings import config

PENDING_CHUNKS_INFLIGHT_KEY = "vchat:embed:pending_chunks:inflight"
PENDING_CHUNKS_BATCH_SIZE = max(
    1, int(config.get("embedding_pending_chunks_batch_size", 8) or 8)
)
PENDING_CHUNKS_MAX_INFLIGHT = max(
    1, int(config.get("embedding_pending_chunks_max_inflight", 32) or 32)
)
PENDING_CHUNKS_COUNTER_TTL = max(
    60, int(config.get("embedding_pending_chunks_counter_ttl_seconds", 600) or 600)
)


def count_pending_chunks(session: Any) -> int:
    return int(
        session.execute(
            sa.select(sa.func.count(Chunk.id)).where(Chunk.embedding.is_(None))
        ).scalar_one()
        or 0
    )


def pending_chunks_remain(session: Any) -> bool:
    return (
        session.execute(
            sa.select(Chunk.id).where(Chunk.embedding.is_(None)).limit(1)
        ).first()
        is not None
    )


def pending_chunk_task_target(pending_chunk_count: int) -> int:
    if pending_chunk_count <= 0:
        return 0
    return min(
        PENDING_CHUNKS_MAX_INFLIGHT,
        max(1, math.ceil(pending_chunk_count / PENDING_CHUNKS_BATCH_SIZE)),
    )


def reserve_pending_chunk_slots(redis_client: Any, target: int) -> int:
    if target <= 0:
        return 0

    return int(
        redis_client.eval(
            """
            local key = KEYS[1]
            local target = tonumber(ARGV[1]) or 0
            local ttl = tonumber(ARGV[2]) or 600
            local current = tonumber(redis.call('GET', key) or '0')
            if current >= target then
                if current > 0 and ttl > 0 then
                    redis.call('EXPIRE', key, ttl)
                end
                return 0
            end
            local missing = target - current
            redis.call('INCRBY', key, missing)
            if ttl > 0 then
                redis.call('EXPIRE', key, ttl)
            end
            return missing
            """,
            1,
            PENDING_CHUNKS_INFLIGHT_KEY,
            target,
            PENDING_CHUNKS_COUNTER_TTL,
        )
        or 0
    )


def release_pending_chunk_slots(redis_client: Any, slots: int = 1) -> int:
    slots = max(1, int(slots or 1))
    return int(
        redis_client.eval(
            """
            local key = KEYS[1]
            local release = tonumber(ARGV[1]) or 1
            local ttl = tonumber(ARGV[2]) or 600
            local current = tonumber(redis.call('GET', key) or '0')
            if current <= release then
                redis.call('DEL', key)
                return 0
            end
            local next = current - release
            redis.call('SET', key, tostring(next))
            if ttl > 0 then
                redis.call('EXPIRE', key, ttl)
            end
            return next
            """,
            1,
            PENDING_CHUNKS_INFLIGHT_KEY,
            slots,
            PENDING_CHUNKS_COUNTER_TTL,
        )
        or 0
    )


def ensure_pending_chunk_workers(
    session: Any, redis_client: Any, schedule_tasks
) -> tuple[int, int]:
    pending_chunk_count = count_pending_chunks(session)
    target = pending_chunk_task_target(pending_chunk_count)
    if target == 0:
        return pending_chunk_count, 0

    missing = reserve_pending_chunk_slots(redis_client, target)
    if missing <= 0:
        return pending_chunk_count, 0

    scheduled = 0
    try:
        scheduled = schedule_tasks(missing)
        return pending_chunk_count, scheduled
    finally:
        unscheduled = missing - scheduled
        if unscheduled > 0:
            release_pending_chunk_slots(redis_client, unscheduled)
