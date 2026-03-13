import argparse
import asyncio
import logging
import sys

import aiohttp
import sentry_sdk
from aiohttp.helpers import DEBUG

from core.app import create_app

if not DEBUG:
    sentry_sdk.init(
        dsn="https://95174ed89be636d7a189ce9f60f04261@o4508591956426752.ingest.us.sentry.io/4508591958196224",
        # Set traces_sample_rate to 1.0 to capture 100%
        # of transactions for tracing.
        traces_sample_rate=0.2,
        _experiments={
            # Set continuous_profiling_auto_start to True
            # to automatically start the profiler when
            # possible.
            "continuous_profiling_auto_start": True,
        },
    )

logger = logging.getLogger()
logger.setLevel(logging.INFO)
logging.getLogger("aiohttp").setLevel(logging.INFO)

try:
    import uvloop

    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except ImportError:
    logging.warning("Uvloop is not available")

parser = argparse.ArgumentParser()
parser.add_argument("--host", type=str, default="127.0.0.1")
parser.add_argument("--port", type=int, default=8080)
parser.add_argument(
    "--revision",
    action="store_true",
    help="Create new migration revision",
)
parser.add_argument("--downgrade", action="store_true", help="Downgrade database")

args = parser.parse_args()
if args.revision:
    from alembic import command
    from alembic.config import Config

    alembic_cfg = Config("alembic.ini")
    message = input("Comment revision: ")
    command.revision(alembic_cfg, message, autogenerate=True)
    logging.info("Create database migration")
    sys.exit(0)

if args.downgrade:
    from alembic import command
    from alembic.config import Config

    alembic_cfg = Config("alembic.ini")
    if revision := input("Downgrade revision (-1 for previous, Enter to skip): "):
        logging.info("Downgrade database to revision %s", revision)
        command.downgrade(alembic_cfg, revision)
    else:
        logging.info("Downgrade skipped")
    sys.exit(0)

app = create_app()

# checks
from core import routes  # noqa
from core import views  # noqa
from core.models import *  # noqa

if __name__ == "__main__":
    aiohttp.web.run_app(app, host=args.host, port=args.port, access_log=logger)
