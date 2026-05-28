import asyncio
import sqlalchemy as sa
from vchat.db import async_session_factory
from vchat.models import Document
from vchat.document_shingles import find_repeated_shingles, remove_shingles, visualize_removed_blocks
import json

async def main():
    async with async_session_factory() as db:
        docs = (await db.execute(sa.select(Document))).scalars().all()
        print(f"Loaded {len(docs)} documents")
        markdowns = []
        for doc in docs:
            content = doc.content or ""
            markdowns.append(content)
        shingles = find_repeated_shingles(markdowns, k=10, min_freq=0.5)
        print(f"Found {len(shingles)} repeated shingles")
        for doc, orig_md in zip(docs, markdowns):
            new_md, removed = remove_shingles(orig_md, shingles)
            meta = doc.meta if isinstance(doc.meta, dict) else {}
            if removed:
                meta["removed_shingles"] = visualize_removed_blocks(removed)
            else:
                meta.pop("removed_shingles", None)
            doc.content = new_md
            doc.meta = meta
        await db.commit()
        print("Shingle removal and meta update complete.")

if __name__ == "__main__":
    asyncio.run(main())
