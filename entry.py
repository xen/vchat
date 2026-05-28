import argparse
import asyncio
import getpass
import logging
import re
import sys

import aiohttp
import aiohttp.web
import sqlalchemy as sa
from passlib.hash import pbkdf2_sha512

from vchat.app import create_app
from vchat.db import async_session_factory
from vchat.logging_utils import configure_json_logging
from vchat.models import User
from vchat.settings import config

logger = logging.getLogger()
logger.setLevel(logging.INFO)
logging.getLogger("aiohttp").setLevel(logging.INFO)
configure_json_logging(logging.INFO)

try:
    import uvloop

    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except ImportError:
    logging.warning("Uvloop is not available")

parser = argparse.ArgumentParser()
parser.add_argument("--host", type=str, default="127.0.0.1")
parser.add_argument("--port", type=int, default=9080)
parser.add_argument(
    "--revision",
    action="store_true",
    help="Create new migration revision",
)
parser.add_argument("--downgrade", action="store_true", help="Downgrade database")
parser.add_argument(
    "--create-user",
    action="store_true",
    help="Create an admin user (interactive)",
)
parser.add_argument(
    "--model",
    action="store_true",
    help="Download the embedding model into data/",
)

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


async def create_user() -> int:
    email = input("Email: ").strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        print("Invalid email format")
        return 1

    password = getpass.getpass("Password: ")
    if password != getpass.getpass("Confirm password: "):
        print("Passwords do not match")
        return 1

    async with async_session_factory() as db:
        existing = await db.scalar(sa.select(User.id).where(User.email == email))
        if existing:
            print(f"User already exists: {email}")
            return 1

        user = User(
            email=email,
            name=email,
            password=pbkdf2_sha512.hash(password),
            is_active=True,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        print(f"User created: id={user.id}, email={user.email}")
        return 0


if args.create_user:
    exit_code = asyncio.run(create_user())
    sys.exit(exit_code)

if args.model:
    from huggingface_hub import snapshot_download

    embedding_model_id = config["embedding_model_id"]
    embedding_model_dir = config["embedding_model_dir"]
    reranker_model_id = config.get(
        "reranker_model_id", "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"
    )

    logging.info("Downloading embedding model %s", embedding_model_id)
    snapshot_download(
        repo_id=embedding_model_id,
        local_dir=embedding_model_dir,
    )

    logging.info("Downloading reranker model %s", reranker_model_id)
    snapshot_download(
        repo_id=reranker_model_id,
        ignore_patterns=[
            "onnx/*",
            "openvino/*",
            "*.onnx",
            "*.xml",
            "*.bin",
        ],
    )
    sys.exit(0)

app = create_app()

# checks
from vchat import routes  # noqa
from vchat import views  # noqa
from vchat.models import *  # noqa

if __name__ == "__main__":
    aiohttp.web.run_app(app, host=args.host, port=args.port, access_log=logger)
