from __future__ import annotations

import logging
from typing import Any, Dict

from aiohttp import web
from itsdangerous import URLSafeTimedSerializer
from redis.asyncio.client import Redis

ConfigDict = Dict[str, Any]

CONFIG_KEY: web.AppKey[ConfigDict] = web.AppKey("config", dict)
LOGGER_KEY: web.AppKey[logging.Logger] = web.AppKey("logger", logging.Logger)
REDIS_KEY: web.AppKey[Redis] = web.AppKey("redis", Redis)
SIGNER_KEY: web.AppKey[URLSafeTimedSerializer] = web.AppKey(
    "signer", URLSafeTimedSerializer
)
SETTINGS_KEY: web.AppKey[ConfigDict] = web.AppKey("settings", dict)
STATIC_VERSION_KEY: web.AppKey[str] = web.AppKey("static_version", str)
