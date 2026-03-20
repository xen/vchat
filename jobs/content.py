import logging
import unicodedata
import openai
import markdown
import sqlalchemy as sa
from anyascii import anyascii
from slugify import slugify
from sqlalchemy.orm import Session

from jobs.celery import app
from jobs.db import create_sync_engine
from vchat.models.support import Chapter, Page
from vchat.settings import config

logger = logging.getLogger(__name__)


def translit_slug(text: str, *, max_length: int = 120) -> str:
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKC", text).strip()
    ascii_text = anyascii(normalized)
    return slugify(
        ascii_text,
        lowercase=True,
        max_length=max_length,
        separator="-",
    )


def get_openai_client():
    api_key = config.get("openai_api_key")
    if not api_key:
        logger.warning("OPENAI_API_KEY not found in config")
        return None
    return openai.OpenAI(api_key=api_key)


def translate_text(client, text, target_lang):
    if not text:
        return ""
    try:
        if target_lang == "en":
            system_prompt = (
                "You are a helpful translator. Translate the following text to English."
            )
        else:
            system_prompt = f"You are a helpful translator. Translate the following text to {target_lang}."  # Simple prompt

        response = client.chat.completions.create(
            model="gpt-4o-mini",  # or config model
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text},
            ],
            temperature=0.3,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"Translation error: {e}")
        return text  # Fallback to original


@app.task
def translate_chapters():
    engine = create_sync_engine()
    client = get_openai_client()
    if not client:
        return

    supported_langs = config.get("lang_supported", {"en": "English"})

    with Session(engine) as session:
        # Get all original chapters (from_id is None)
        originals = (
            session.execute(sa.select(Chapter).where(Chapter.from_id.is_(None)))
            .scalars()
            .all()
        )

        for ch in originals:
            for lang_code in supported_langs.keys():
                if lang_code == ch.lang:
                    continue

                # Check if exists
                existing = session.execute(
                    sa.select(Chapter).where(
                        Chapter.from_id == ch.id, Chapter.lang == lang_code
                    )
                ).scalar_one_or_none()

                if not existing:
                    logger.info(f"Translating chapter {ch.title} to {lang_code}")
                    translated_title = translate_text(client, ch.title, lang_code)
                    slug = translit_slug(translated_title)

                    new_ch = Chapter(
                        title=translated_title,
                        slug=slug,
                        lang=lang_code,
                        is_hidden=ch.is_hidden,
                        order=ch.order,
                        from_id=ch.id,
                        is_translated=True,
                    )
                    session.add(new_ch)
                    session.commit()


@app.task
def translate_page(page_id: int):
    engine = create_sync_engine()
    client = get_openai_client()
    if not client:
        return

    supported_langs = config.get("lang_supported", {"en": "English"})

    with Session(engine) as session:
        page = session.get(Page, page_id)
        if not page:
            logger.error(f"Page {page_id} not found")
            return

        if page.from_id is not None:
            # Don't translate translations? Or keep chain? Usually translate specific original.
            return

        # Translate to all supported languages
        for lang_code in supported_langs.keys():
            if lang_code == page.lang:
                continue

            # Check existence
            existing = session.execute(
                sa.select(Page).where(Page.from_id == page.id, Page.lang == lang_code)
            ).scalar_one_or_none()

            if not existing:
                # Resolve Target Chapter FIRST to save tokens
                source_chapter = session.get(Chapter, page.chapter_id)
                if not source_chapter:
                    logger.warning(
                        f"Source chapter {page.chapter_id} not found for page {page.id}"
                    )
                    continue

                root_chapter_id = (
                    source_chapter.from_id
                    if source_chapter.from_id
                    else source_chapter.id
                )

                # Check valid root
                root_chapter = session.get(Chapter, root_chapter_id)
                if not root_chapter:
                    logger.warning(f"Root chapter {root_chapter_id} not found")
                    continue

                target_chapter = None
                if root_chapter.lang == lang_code:
                    target_chapter = root_chapter
                else:
                    target_chapter = session.execute(
                        sa.select(Chapter).where(
                            Chapter.from_id == root_chapter_id,
                            Chapter.lang == lang_code,
                        )
                    ).scalar_one_or_none()

                if not target_chapter:
                    logger.warning(
                        f"Skipping page translation {page.id} to {lang_code}: Target chapter not found (Root: {root_chapter_id})."
                    )
                    continue

                logger.info(f"Translating page {page.id} ({page.title}) to {lang_code}")

                t_title = translate_text(client, page.title, lang_code)
                t_body = translate_text(client, page.body, lang_code)
                t_slug = translit_slug(t_title)
                t_body_html = markdown.markdown(t_body)

                new_page = Page(
                    title=t_title,
                    slug=t_slug,
                    lang=lang_code,
                    body=t_body,
                    body_html=t_body_html,
                    is_hidden=page.is_hidden,
                    is_favorite=page.is_favorite,
                    favorit_order=page.favorit_order,
                    from_id=page.id,
                    is_translated=True,
                    chapter_id=target_chapter.id,
                    order=page.order,
                )
                session.add(new_page)
                session.commit()
