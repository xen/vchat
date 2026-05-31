from __future__ import annotations

from .config import IdleWsProfileConfig
from .scenarios import run_idle_ws_scenario


async def run_idle_ws_profile(config: IdleWsProfileConfig):
    return await run_idle_ws_scenario(config)
