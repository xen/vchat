import logging
from typing import List

import numpy as np
import requests
import sqlalchemy as sa
from scipy.cluster.vq import kmeans
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from jobs.celery import app
from jobs.db import create_sync_engine
from vchat.ai_providers import resolve_ai_settings
from vchat.models import Chunk, Settings
from vchat.utils import json

logger = logging.getLogger(__name__)

# Fallbacks for prompts
OPENAI_BASE_URL = "https://api.openai.com/v1"
OPENAI_MODEL = "gpt-4o-mini"


def _settings_map(session: Session) -> dict[str, str | None]:
    rows = session.execute(sa.select(Settings.key, Settings.value)).all()
    return {row.key: row.value for row in rows}


def _fetch_embedding_sample(session: Session, limit: int = 5000) -> np.ndarray:
    """Fetch a random sample of embeddings as a numpy array."""
    stmt = (
        sa.select(Chunk.embedding)
        .where(Chunk.embedding.isnot(None))
        .order_by(sa.func.random())
        .limit(limit)
    )
    rows = session.execute(stmt).scalars().all()
    if not rows:
        return np.array([])
    return np.array(rows)


def _fetch_representative_chunks_for_centroids(
    session: Session,
    centroids: np.ndarray,
    chunks_per_cluster: int = 3,
) -> List[str]:
    """For each centroid, find the closest chunks in the database."""
    all_chunks = []

    for centroid in centroids:
        centroid_list = centroid.tolist()
        stmt = (
            sa.select(Chunk.text)
            .where(Chunk.embedding.isnot(None))
            .order_by(Chunk.embedding.cosine_distance(centroid_list))
            .limit(chunks_per_cluster)
        )
        cluster_chunks = list(session.execute(stmt).scalars().all())
        all_chunks.extend(cluster_chunks)

    unique_chunks = []
    seen = set()
    for chunk_text in all_chunks:
        if chunk_text not in seen:
            unique_chunks.append(chunk_text)
            seen.add(chunk_text)
    return unique_chunks


def summarize_clustered_topics(
    settings_map: dict[str, str | None],
    representative_chunks: List[str],
) -> dict:
    """Send clustered representative chunks to LLM for summarization."""
    if not representative_chunks:
        return {}

    provider_id = settings_map.get("project.provider") or "openai"
    model_id = settings_map.get("project.model") or None
    project_title = settings_map.get("project.title") or "vchat"

    provider, model = resolve_ai_settings(provider_id, model_id)

    meta = provider.request_meta()
    api_key = meta.get("api_key")
    base_url = meta.get("base_url") or OPENAI_BASE_URL
    model_name = model.id or OPENAI_MODEL

    if not api_key:
        logger.warning("No API key found for configured provider")
        return {}

    chunks_str = "\n\n---\n\n".join(representative_chunks)

    prompt = f"""You are a senior data analyst. I have analyzed a large knowledge base for the project "{project_title}"
and grouped the content into semantic clusters. Below are representative samples from each cluster.

Your task is to identify the core themes (topics) and likely user intents.
Since this project might cover multiple distinct areas, ensure your list is diverse and covers different facets of the content.

REPRESENTATIVE SAMPLES FROM SEMANTIC CLUSTERS:
{chunks_str}

Return ONLY a raw JSON object with two fields:
- "topics": a list of 5-10 strings (each a concise, professional topic)
- "intents": a list of 5-10 strings (each a concise user intent or common question)

Example format:
{{
  "topics": ["Municipal Services", "Document Registration", "Social Support"],
  "intents": ["how to apply for housing benefit", "where to register a birth"]
}}
"""

    try:
        resp = requests.post(
            f"{base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model_name,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
            },
            timeout=45,
        )
        resp.raise_for_status()
        result = resp.json()
        content = result["choices"][0]["message"]["content"].strip()

        if content.startswith("```"):
            lines = content.splitlines()
            content = "\n".join(lines[1:-1])

        return json.loads(content)
    except Exception as exc:
        logger.error("Failed to summarize clustered topics: %s", exc)
        return {}


@app.task(name="jobs.suggestions.generate_project_topics")
def generate_project_topics():
    """Generate global topics/intents and save them to settings."""
    engine = create_sync_engine()
    try:
        with Session(bind=engine) as session:
            logger.info("Starting clustered topic generation")
            settings_map = _settings_map(session)

            embeddings = _fetch_embedding_sample(session, limit=5000)
            if embeddings.size == 0:
                logger.info("No embeddings found")
                return

            num_clusters = min(8, len(embeddings))
            centroids, _ = kmeans(embeddings.astype(float), num_clusters)
            chunks = _fetch_representative_chunks_for_centroids(session, centroids)
            summary = summarize_clustered_topics(settings_map, chunks)

            if not summary:
                logger.warning("No summary generated")
                return

            topics = summary.get("topics", [])
            intents = summary.get("intents", [])
            stmt = insert(Settings).values(
                [
                    {"key": "project.topics", "value": json.dumps(topics)},
                    {"key": "project.intents", "value": json.dumps(intents)},
                ]
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[Settings.key],
                set_={"value": stmt.excluded.value},
            )
            session.execute(stmt)
            session.commit()
            logger.info("Successfully updated clustered topics")

    except Exception as exc:
        logger.exception("Error in generate_project_topics: %s", exc)
    finally:
        engine.dispose()
