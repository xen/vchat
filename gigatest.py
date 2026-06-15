import asyncio
import logging
import sys

import aiohttp

from vchat.views.chat.oauth import get_gigachat_access_token
from vchat.settings import config

logger = logging.getLogger("gigatest")


def _cfg_float(key: str, default: float) -> float:
    try:
        return float(config.get(key, default))
    except (TypeError, ValueError):
        return default


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    basic_key = (config.get("gigachat_api_key") or "").strip()
    if not basic_key:
        raise SystemExit(
            "Missing GigaChat key. Set gigachat_api_key in local.yaml or env GIGACHAT_API_KEY."
        )

    base_url = (config.get("gigachat_base_url") or "").strip()
    if not base_url:
        base_url = "https://gigachat.devices.sberbank.ru/api/v1"

    verify_ssl = bool(config.get("gigachat_verify_ssl_certs", True))
    oauth_timeout_seconds = _cfg_float("gigachat_oauth_timeout_seconds", 15.0)
    request_timeout_seconds = _cfg_float("gigachat_request_timeout_seconds", 60.0)
    model_fallback = (config.get("gigachat_test_model") or "GigaChat-Pro").strip()

    question = (
        config.get("gigachat_test_question")
        or "Мы делаем чат-ассистента по базе знаний проекта. Пользователь спрашивает: "
        "'Как подключить виджет чата на сайт и какие минимальные шаги интеграции?'. "
        "Дай короткий практический ответ на русском и добавь 3 следующих шага."
    )

    logger.info(
        "Starting GigaChat diagnostic test: base_url=%s verify_ssl=%s oauth_timeout=%.1fs request_timeout=%.1fs",
        base_url,
        verify_ssl,
        oauth_timeout_seconds,
        request_timeout_seconds,
    )

    async with aiohttp.ClientSession() as session:
        try:
            access_token = await get_gigachat_access_token(
                session,
                basic_auth_key=basic_key,
            )
            logger.info("OAuth token received successfully")

            async with session.get(
                f"{base_url}/models",
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {access_token}",
                },
                ssl=verify_ssl,
                timeout=aiohttp.ClientTimeout(total=request_timeout_seconds),
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
                ssl=verify_ssl,
                timeout=aiohttp.ClientTimeout(total=request_timeout_seconds),
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
