# telegram_management_bot/utils/fastapi_interaction.py
import httpx
import logging
from typing import Optional, Dict, Any, List

from .. import bot_config # Импортируем конфигурацию бота

logger = logging.getLogger(__name__)

async def _make_fastapi_request(
    method: str,
    endpoint: str,
    params: Optional[Dict[str, Any]] = None,
    json_data: Optional[Dict[str, Any]] = None,
    files: Optional[Dict[str, Any]] = None, # Для загрузки файлов
    timeout: int = 30 # Таймаут по умолчанию для запросов к FastAPI
) -> Optional[Dict[str, Any]]:
    """
    Универсальная функция для выполнения запросов к FastAPI сервису.
    """
    url = f"{bot_config.FASTAPI_URL.rstrip('/')}/{endpoint.lstrip('/')}"
    headers = {
        "X-API-Key": bot_config.FASTAPI_API_KEY,
        "User-Agent": "TelegramManagementBot/1.0" # Можно добавить версию бота
    }

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response: httpx.Response
            if method.upper() == "GET":
                response = await client.get(url, headers=headers, params=params)
            elif method.upper() == "POST":
                response = await client.post(url, headers=headers, params=params, json=json_data, files=files)
            # Добавить другие методы (PUT, DELETE) при необходимости
            else:
                logger.error(f"Unsupported HTTP method: {method}")
                return None

            response.raise_for_status() # Вызовет исключение для 4xx/5xx
            
            # Попытка декодировать JSON, если Content-Type позволяет
            if "application/json" in response.headers.get("content-type", "").lower():
                return response.json()
            else: # Если не JSON, возвращаем как текст (или None, если пусто)
                return {"raw_content": response.text} if response.text else None

    except httpx.HTTPStatusError as e:
        error_content = "No content"
        try:
            error_content = e.response.json() # Попытка получить JSON из ошибки
        except Exception:
            error_content = e.response.text # Если не JSON, то текст
        logger.error(f"FastAPI request to {url} failed with status {e.response.status_code}. Response: {error_content}")
        return {"error": f"HTTP Error: {e.response.status_code}", "detail": error_content, "status_code": e.response.status_code}
    except httpx.RequestError as e:
        logger.error(f"FastAPI request to {url} failed due to network/request error: {e}")
        return {"error": f"Request Error: {str(e)}", "detail": str(e)}
    except Exception as e:
        logger.error(f"Unexpected error during FastAPI request to {url}: {e}", exc_info=True)
        return {"error": f"Unexpected Error: {str(e)}", "detail": str(e)}

# --- Функции для конкретных эндпоинтов FastAPI ---

async def get_fastapi_health() -> Optional[Dict[str, Any]]:
    """Получает статус здоровья FastAPI сервиса."""
    logger.info("Requesting FastAPI health status...")
    return await _make_fastapi_request("GET", "/health")

async def get_fastapi_account_stats() -> Optional[Dict[str, Any]]:
    """Получает статистику аккаунтов от FastAPI сервиса."""
    logger.info("Requesting FastAPI account stats...")
    return await _make_fastapi_request("GET", "/stats/accounts")

async def download_fastapi_logs() -> Optional[str]: # Возвращает содержимое лог-файла как строку
    """Запрашивает лог-файл FastAPI сервиса."""
    logger.info("Requesting FastAPI log file content...")
    # Этот эндпоинт возвращает FileResponse, поэтому _make_fastapi_request вернет raw_content
    response_data = await _make_fastapi_request("GET", "/logs/download", timeout=60) # Увеличенный таймаут для логов
    if response_data and "raw_content" in response_data:
        return response_data["raw_content"]
    elif response_data and "error" in response_data:
        logger.error(f"Failed to download FastAPI logs: {response_data.get('detail')}")
    return None

# Функции для управления сессиями через API FastAPI (если такие эндпоинты будут добавлены)
# async def freeze_fastapi_session(phone_number: str, duration_hours: Optional[int] = None) -> Optional[Dict[str, Any]]:
#     logger.info(f"Requesting to freeze FastAPI session: {phone_number}")
#     payload = {"phone_number": phone_number}
#     if duration_hours:
#         payload["duration_hours"] = duration_hours
#     return await _make_fastapi_request("POST", "/sessions/freeze", json_data=payload)

# async def unfreeze_fastapi_session(phone_number: str) -> Optional[Dict[str, Any]]:
#     logger.info(f"Requesting to unfreeze FastAPI session: {phone_number}")
#     return await _make_fastapi_request("POST", "/sessions/unfreeze", json_data={"phone_number": phone_number})

# async def trigger_fastapi_session_reload() -> Optional[Dict[str, Any]]:
#     logger.info("Requesting FastAPI to reload sessions...")
#     return await _make_fastapi_request("POST", "/sessions/reload")