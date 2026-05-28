import asyncio
import sys
import sqlalchemy as sa
from vchat.document_pipeline import extract_url_document
from vchat.db import async_session_factory
from vchat.models import Document

URL = 'https://navigator.vbudushee.ru/direction/sotsialno-emotsionalnoe-razvitie/v-nekotorom-tsarstve/'
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

from difflib import SequenceMatcher

def similarity(a, b):
    return SequenceMatcher(None, a, b).ratio()

async def main():
    # Переиндексация
    content, title, meta = extract_url_document(URL)
    # Проверка результата
    async with async_session_factory() as db:
        doc = await db.scalar(sa.select(Document).where(Document.uri == URL))
        if not doc:
            print('NOT FOUND')
            sys.exit(1)
        print('---NORMALIZED CONTENT---')
        print(content)
        print('---END---')
        score = similarity(content, REFERENCE)
        print(f'Similarity to reference: {score:.3f}')
        if score > 0.85:
            print('OK: Content is close to reference')
        else:
            print('WARNING: Content is still not close to reference!')

if __name__ == "__main__":
    asyncio.run(main())
