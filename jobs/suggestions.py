import logging
import requests
import numpy as np
from typing import List
from scipy.cluster.vq import kmeans

import sqlalchemy as sa
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from jobs.celery import app
from jobs.db import create_sync_engine
from core.models import Project, Chunk
from core.ai_providers import resolve_ai_settings
from core.utils import json

logger = logging.getLogger(__name__)

# Fallbacks for prompts
OPENAI_BASE_URL = "https://api.openai.com/v1"
OPENAI_MODEL = "gpt-4o-mini"


def _fetch_embedding_sample(
    session: Session, project_id: int, limit: int = 5000
) -> np.ndarray:
    """Fetch a random sample of embeddings as a numpy array."""
    stmt = (
        sa.select(Chunk.embedding)
        .where(Chunk.project_id == project_id)
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
    project_id: int,
    centroids: np.ndarray,
    chunks_per_cluster: int = 3,
) -> List[str]:
    """For each centroid, find the closest chunks in the database."""
    all_chunks = []

    for centroid in centroids:
        # Convert numpy array back to list for pgvector compatibility
        centroid_list = centroid.tolist()

        stmt = (
            sa.select(Chunk.content)
            .where(Chunk.project_id == project_id)
            .order_by(Chunk.embedding.cosine_distance(centroid_list))
            .limit(chunks_per_cluster)
        )
        cluster_chunks = list(session.execute(stmt).scalars().all())
        all_chunks.extend(cluster_chunks)

    # Deduplicate while preserving order (optional, but good for prompt clarity)
    unique_chunks = []
    seen = set()
    for c in all_chunks:
        if c not in seen:
            unique_chunks.append(c)
            seen.add(c)
    return unique_chunks


def summarize_clustered_topics(
    project: Project,
    representative_chunks: List[str],
) -> dict:
    """Send clustered representative chunks to LLM for summarization."""
    if not representative_chunks:
        return {}

    provider, model = resolve_ai_settings(
        getattr(project, "provider", "openai"),
        getattr(project, "model", None),
    )

    meta = provider.request_meta()
    api_key = meta.get("api_key")
    base_url = meta.get("base_url") or OPENAI_BASE_URL
    model_id = model.id or OPENAI_MODEL

    if not api_key:
        logger.warning(f"No API key found for project {project.id}")
        return {}

    chunks_str = "\n\n---\n\n".join(representative_chunks)

    prompt = f"""You are a senior data analyst. I have analyzed a large knowledge base for the project "{project.title}"
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
                "model": model_id,
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
            if lines[0].startswith("```json"):
                content = "\n".join(lines[1:-1])
            else:
                content = "\n".join(lines[1:-1])

        return json.loads(content)
    except Exception as e:
        logger.error(
            f"Failed to summarize clustered topics for project {project.id}: {e}"
        )
        return {}


@app.task(name="jobs.suggestions.generate_project_topics")
def generate_project_topics(project_id: int):
    """Execution entry point for the Clustered Celery task."""
    engine = create_sync_engine()
    try:
        with Session(bind=engine) as session:
            project = session.get(Project, project_id)
            if not project:
                logger.warning(f"Project {project_id} not found")
                return

            logger.info(f"Starting clustered topic generation for Project {project_id}")
            # 1. Fetch a sample of embeddings
            embeddings = _fetch_embedding_sample(session, project_id, limit=5000)
            if embeddings.size == 0:
                logger.info(f"No embeddings found for project {project_id}")
                return

            # 2. Perform K-Means Clustering
            # We target 8 clusters as a good balance for multi-themed projects.
            # If we have fewer than 8 samples, use the sample size.
            num_clusters = min(8, len(embeddings))

            # Whiten the observations (standardize features) for better K-Means performance
            # However, for embeddings, whitening can sometimes distort semantic relative distances.
            # We use kmeans directly on raw embeddings as they are usually already normalized.
            centroids, _ = kmeans(embeddings.astype(float), num_clusters)

            # 3. Fetch representative chunks for each centroid
            chunks = _fetch_representative_chunks_for_centroids(
                session, project_id, centroids
            )

            # 4. LLM Analysis
            summary = summarize_clustered_topics(project, chunks)

            if summary:
                meta = project.meta or {}
                meta["topics"] = summary.get("topics", [])
                meta["intents"] = summary.get("intents", [])
                project.meta = meta
                flag_modified(project, "meta")
                session.commit()
                logger.info(
                    f"Successfully updated clustered topics for project {project_id}"
                )
            else:
                logger.warning(f"No summary generated for project {project_id}")

    except Exception as e:
        logger.exception(
            f"Error in clustered generate_project_topics for project {project_id}: {e}"
        )
    finally:
        engine.dispose()
