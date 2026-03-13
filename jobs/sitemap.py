import logging
from datetime import datetime
from pathlib import Path
from typing import List, Dict

import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker
from yarl import URL

from jobs.celery import app
from jobs.db import create_sync_engine
from core.models import Post, PostCategory
from core.models.support import Page
from core.settings import config

logger = logging.getLogger(__name__)


def get_static_pages_urls() -> List[Dict]:
    """
    Collect static pages URLs by scanning the docs directory.
    Route: /about/{page}
    """
    urls = []
    # core/docs is at project_root/docs, currently we are in jobs/sitemap.py
    # jobs/sitemap.py -> project_root/jobs/sitemap.py
    # docs is at project_root/docs
    docs_path = Path(__file__).parent.parent / "docs"
    supported_langs = config.get("lang_supported", {})
    base_url = URL(config["public_url"])

    page_names = set()
    for lang in supported_langs:
        lang_dir = docs_path / lang
        if lang_dir.exists():
            for file in lang_dir.glob("*.md"):
                page_names.add(file.stem)

    if "index" in page_names:
        page_names.remove("index")

    all_pages = page_names.union({"index"})

    for page in all_pages:
        path = "/about/" if page == "index" else f"/about/{page}"
        urls.append(
            {
                "loc": base_url.with_path(path),
                "changefreq": "monthly",
                "priority": "0.5",
            }
        )

    return urls


def generate_xml_sync(urls: List[Dict]) -> str:
    lines = ['<?xml version="1.0" encoding="UTF-8"?>']
    lines.append(
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:xhtml="http://www.w3.org/1999/xhtml">'
    )

    for u in urls:
        lines.append("  <url>")
        lines.append(f"    <loc>{u['loc']}</loc>")
        if "lastmod" in u and u["lastmod"]:
            dt = u["lastmod"]
            if isinstance(dt, datetime):
                dt = dt.strftime("%Y-%m-%d")
            lines.append(f"    <lastmod>{dt}</lastmod>")
        if "changefreq" in u:
            lines.append(f"    <changefreq>{u['changefreq']}</changefreq>")
        if "priority" in u:
            lines.append(f"    <priority>{u['priority']}</priority>")

        if "alternates" in u:
            for lang, link in u["alternates"].items():
                lines.append(
                    f'    <xhtml:link rel="alternate" hreflang="{lang}" href="{link}"/>'
                )

        lines.append("  </url>")

    lines.append("</urlset>")
    return "\n".join(lines)


@app.task(name="seo.sitemap_generate")
def generate_sitemap_task():
    """
    Celery task to generate sitemap.xml
    """
    logger.info("Starting sitemap generation task.")
    engine = create_sync_engine()
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        base_url = URL(config["public_url"])
        supported_langs = config.get("lang_supported", {}).keys()
        final_urls = []

        # 1. Static Fixed Pages
        final_urls.append(
            {"loc": base_url.with_path("/"), "changefreq": "daily", "priority": "1.0"}
        )
        final_urls.append(
            {
                "loc": base_url.with_path("/prices"),
                "changefreq": "monthly",
                "priority": "0.8",
            }
        )

        # 2. Static Docs Pages
        final_urls.extend(get_static_pages_urls())

        # 3. Blog Posts
        query = sa.select(Post).where(Post.is_published == True)
        posts = session.execute(query).scalars().all()

        for post in posts:
            path = f"/blog/{post.short_id}_{post.slug}"
            final_urls.append(
                {
                    "loc": base_url.with_path(path),
                    "lastmod": post.updated_at or post.published_at,
                    "changefreq": "weekly",
                    "priority": "0.7",
                }
            )

        # Blog Categories
        query = sa.select(PostCategory)
        cats = session.execute(query).scalars().all()
        for cat in cats:
            path = f"/blog/category/{cat.slug}"
            final_urls.append(
                {
                    "loc": base_url.with_path(path),
                    "changefreq": "weekly",
                    "priority": "0.6",
                }
            )

        # Blog Index
        final_urls.append(
            {
                "loc": base_url.with_path("/blog/"),
                "changefreq": "daily",
                "priority": "0.8",
            }
        )

        # 4. Support Pages
        final_urls.append(
            {
                "loc": base_url.with_path("/support/"),
                "changefreq": "weekly",
                "priority": "0.8",
            }
        )

        query = sa.select(Page).where(Page.is_hidden == False)
        pages = session.execute(query).scalars().all()

        groups = {}
        for p in pages:
            source_id = p.from_id if p.from_id else p.id
            if source_id not in groups:
                groups[source_id] = []
            groups[source_id].append(p)

        for source_id, group in groups.items():
            alternates = {}
            for p in group:
                path = f"/{p.lang}/support/{p.slug}"
                alternates[p.lang] = str(base_url.with_path(path))

            for p in group:
                path = f"/{p.lang}/support/{p.slug}"
                final_urls.append(
                    {
                        "loc": base_url.with_path(path),
                        "lastmod": p.updated_at,
                        "priority": "0.8",
                        "alternates": alternates,
                    }
                )

        # Main Support Page Multi-lang
        alternates_main = {}
        for lang in supported_langs:
            alternates_main[lang] = str(base_url.with_path(f"/{lang}/support"))

        for lang in supported_langs:
            final_urls.append(
                {
                    "loc": base_url.with_path(f"/{lang}/support"),
                    "priority": "0.8",
                    "changefreq": "weekly",
                    "alternates": alternates_main,
                }
            )

        xml_content = generate_xml_sync(final_urls)

        # Save file
        # config.media_root is "media". Parent is usually project root.
        # But wait, static folder is distinct.
        # Let's use relative path from this file to static.
        # jobs/sitemap.py -> parent -> jobs -> parent -> root -> static
        output_path = Path(__file__).parent.parent / "static" / "sitemap.xml"
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with open(output_path, "w") as f:
            f.write(xml_content)

        logger.info(f"Sitemap generated at {output_path} with {len(final_urls)} URLs.")

    except Exception as e:
        logger.exception("Error generating sitemap")
        raise
    finally:
        session.close()
        # Engine disposal? usually kept alive for app but for task maybe fine to close or keep?
        # sync_engine is created fresh here? No create_sync_engine creates new one.
        # So we should dispose it.
        engine.dispose()
