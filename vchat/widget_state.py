WIDGET_STATE_TTL_SECONDS = 60 * 60

WIDGET_STATE_ENABLED = "enabled"
WIDGET_STATE_DISABLED = "disabled"
WIDGET_STATE_MISSING = "missing"


def widget_state_key(code: str) -> str:
    return f"widget:{code}:state"


async def cache_widget_state(redis, code: str, state: str) -> None:
    await redis.set(widget_state_key(code), state, ex=WIDGET_STATE_TTL_SECONDS)


def decode_widget_state(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value or "")
