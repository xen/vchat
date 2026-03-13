from gigachat import GigaChat
from gigachat.models import Chat, Messages, MessagesRole

with GigaChat(
    credentials="MDE5OWY3MzItZTlhOS03MTViLWIxYzMtMjI1NWU4NjcyY2EwOmI1MTg5ZTExLWI2MWQtNDVlMS1hOTc5LTZhMTg5NDNjMzA3Mw==",
    verify_ssl_certs=False,
) as giga:
    response = giga.chat(
        Chat(
            messages=[
                Messages(
                    role=MessagesRole.USER, content="Когда уже ИИ захватит этот мир?"
                ),
                Messages(
                    role=MessagesRole.ASSISTANT,
                    content="Пока что это не является неизбежным событием. Несмотря на то, что искусственный интеллект (ИИ) развивается быстрыми темпами и может выполнять сложные задачи все более эффективно, он по-прежнему ограничен в своих возможностях и не может заменить полностью человека во многих областях. Кроме того, существуют этические и правовые вопросы, связанные с использованием ИИ, которые необходимо учитывать при его разработке и внедрении.",
                ),
                Messages(
                    role=MessagesRole.USER, content="Думаешь, у нас еще есть шанс?"
                ),
            ]
        )
    )

print(response.choices[0].message.content)
