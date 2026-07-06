import asyncio
import logging
import sys

import aiohttp

from vchat.views.chat.ai import GigaChatProvider
from vchat.settings import cfg

logger = logging.getLogger("gigatest")


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    basic_key = cfg.gigachat_api_key.strip()
    if not basic_key:
        raise SystemExit(
            "Missing GigaChat key. Set gigachat_api_key in local.yaml or env GIGACHAT_API_KEY."
        )

    base_url = cfg.gigachat_base_url.strip()
    model_fallback = cfg.gigachat_test_model.strip()

    question = (
        cfg.gigachat_test_question
        or "Мы делаем чат-ассистента по базе знаний проекта. Пользователь спрашивает: "
        "'Как подключить виджет чата на сайт и какие минимальные шаги интеграции?'. "
        "Дай короткий практический ответ на русском и добавь 3 следующих шага."
    )

    logger.info(
        "Starting GigaChat diagnostic test: base_url=%s verify_ssl=%s request_timeout=%.1fs",
        base_url,
        cfg.gigachat_verify_ssl_certs,
        cfg.llm_request_timeout_seconds,
    )

    provider = GigaChatProvider()
    async with aiohttp.ClientSession() as session:
        try:
            access_token = await provider.chat_completion_bearer_token(session)
            logger.info("GigaChat access token received successfully")

            async with session.get(
                f"{base_url}/models",
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {access_token}",
                },
                ssl=cfg.gigachat_verify_ssl_certs,
                timeout=aiohttp.ClientTimeout(total=cfg.llm_request_timeout_seconds),
            ) as resp:
                models_text = await resp.text()
                if resp.status >= 400:
                    logger.error(
                        "Models request failed: status=%s detail=%s",
                        resp.status,
                        models_text[:1000],
                    )
                    print(models_text)
                    raise SystemExit(1)

                try:
                    models_data = await resp.json(content_type=None)
                except Exception:
                    models_data = None

                selected_model = model_fallback
                if isinstance(models_data, dict):
                    items = models_data.get("data") or models_data.get("models") or []
                    if isinstance(items, list) and items:
                        first = items[0]
                        if isinstance(first, dict) and isinstance(first.get("id"), str):
                            selected_model = first["id"].strip() or selected_model

                logger.info("Selected model for chat test: %s", selected_model)

            payload = {
                "model": selected_model,
                "messages": [
                    {
                        "role": "system",
                        "content": "Ты дружелюбный ассистент проекта vchat. Отвечай кратко и по делу.",
                    },
                    {"role": "user", "content": question},
                ],
                "temperature": 0.2,
                "max_tokens": 700,
            }

            async with session.post(
                f"{base_url}/chat/completions",
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json=payload,
                ssl=cfg.gigachat_verify_ssl_certs,
                timeout=aiohttp.ClientTimeout(total=cfg.llm_request_timeout_seconds),
            ) as resp:
                response_text = await resp.text()
                if resp.status >= 400:
                    logger.error(
                        "Chat request failed: status=%s detail=%s",
                        resp.status,
                        response_text[:1000],
                    )
                    print(response_text)
                    raise SystemExit(1)

                print("=== GIGACHAT RAW RESPONSE ===")
                print(response_text)

                try:
                    data = await resp.json(content_type=None)
                    answer = (
                        ((data.get("choices") or [{}])[0].get("message") or {}).get(
                            "content"
                        )
                        if isinstance(data, dict)
                        else None
                    )
                    if answer:
                        print("\n=== PARSED ANSWER ===")
                        print(answer)
                except Exception:
                    logger.warning("Response is not JSON, raw text printed above")
        except asyncio.TimeoutError:
            logger.exception("Timeout while calling GigaChat API")
            print(
                "Timeout while calling GigaChat API. Check network and timeout settings."
            )
            raise SystemExit(1)
        except aiohttp.ClientError as exc:
            logger.exception(
                "Network/transport error while calling GigaChat API: %s", exc
            )
            print(f"Network/transport error while calling GigaChat API: {exc}")
            raise SystemExit(1)
        except Exception as exc:
            logger.exception("Unexpected GigaChat test failure: %s", exc)
            print(f"Unexpected GigaChat test failure: {exc}")
            raise SystemExit(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.warning("Interrupted by user")
        print("Interrupted")
        sys.exit(130)
