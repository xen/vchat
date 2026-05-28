import asyncio
import sqlalchemy as sa
from vchat.db import async_session_factory
from vchat.models import Document
from difflib import SequenceMatcher

TARGET_URI = '%navigator.vbudushee.ru/direction/sotsialno-emotsionalnoe-razvitie/v-nekotorom-tsarstve%'
REFERENCE = '''В некотором царстве
Автор: Hobby World
«В некотором царстве» - это очень добрая и позитивная игра, в которой участники сочиняют настоящие сказки. Но не просто так, а с помощью направляющих карточек. Игроки по очереди выдумывают кусочки сказки, перехватывая инициативу друг у друга, и так их истории становятся всё запутаннее и удивительнее.
Данная страница является информационной и не содержит ссылки сайт продавца/поставщика услуги.

Мнение эксперта
Семейная игра, в которой ребенок учится сочинять сказочные истории, разыгрывая карты с классическими элементами сказок: волками, феями, тёмными пещерами и колдовскими чарами. Можно использовать карты помех, чтобы вмешаться в чужую сказку и перехватить инициативу.
Ребенок учится выстраивать связанный сюжет и видеть причинно-следственные связи событий, развивает память, логику, воображение, устную речь.

Светлана Овчинникова

Соответствие в структуре УКНГ: Понимаю себя, Понимаю других, Управляю собой, Действую в команде
Соответствует требованиям ФГОС: Регулятивные, Коммуникативные, Личностные'''

WINDOW = 30  # строка окна для поиска


def find_best_match(text, reference):
    text_lines = text.splitlines()
    ref_lines = reference.splitlines()
    best_score = 0
    best_start = 0
    best_end = 0
    for i in range(len(text_lines) - len(ref_lines) + 1):
        window = '\n'.join(text_lines[i:i+len(ref_lines)])
        score = SequenceMatcher(None, window, reference).ratio()
        if score > best_score:
            best_score = score
            best_start = i
            best_end = i + len(ref_lines)
    return '\n'.join(text_lines[best_start:best_end]), best_score

async def main():
    async with async_session_factory() as db:
        doc = await db.scalar(sa.select(Document).where(Document.uri.ilike(TARGET_URI)))
        if not doc:
            print('NOT FOUND')
            return
        best, score = find_best_match(doc.content, REFERENCE)
        print(f'Best match score: {score:.3f}')
        print('---EXTRACTED BLOCK---')
        print(best)
        print('---END---')
        # Optionally, save to meta or elsewhere
        doc.meta = doc.meta or {}
        doc.meta['reference_extracted'] = best
        await db.commit()
        print('Saved to meta')

if __name__ == "__main__":
    asyncio.run(main())
