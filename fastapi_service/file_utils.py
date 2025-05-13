import asyncio
import mimetypes
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Optional, Tuple, List
from urllib.parse import urlparse, unquote_plus

import aiofiles
import httpx

from . import config
from .models import ContentDisposition
from .utils import get_logger, clean_filename

logger = get_logger(__name__)

# Регулярное выражение для Content-Disposition, учитывающее filename и filename*
# Упрощенное, для filename="...", filename*=UTF-8''...
# Более полный парсер был бы сложнее.
# Это выражение ищет filename="quoted string" или filename=unquoted-string
# А также filename*=charset''encoded-string
RE_CONTENT_DISPOSITION_FILENAME = re.compile(
    r'filename\*=(?P<charset>[\w-]+)\'\'(?P<encoded_value>[^;]+)|filename="(?P<quoted_value>[^"]+)"|filename=(?P<unquoted_value>[^;]+)',
    re.IGNORECASE
)


def parse_content_disposition(header_value: str) -> ContentDisposition:
    """
    Parses the Content-Disposition header to extract filename and filename*.
    Prioritizes filename* if available and decodes it.
    """
    filename = None
    filename_star_decoded = None

    # Ищем все совпадения, так как filename и filename* могут присутствовать вместе
    matches = list(RE_CONTENT_DISPOSITION_FILENAME.finditer(header_value))
    
    for match in matches:
        if match.group('encoded_value'): # filename*
            charset = match.group('charset').lower()
            encoded_value = match.group('encoded_value')
            try:
                # unquote_plus для URL-декодирования (%xx)
                filename_star_decoded = unquote_plus(encoded_value, encoding=charset)
            except Exception as e:
                logger.warning(f"Failed to decode filename* '{encoded_value}' with charset '{charset}': {e}")
        elif match.group('quoted_value'): # filename="value"
            if not filename_star_decoded: # filename* имеет приоритет
                filename = match.group('quoted_value')
        elif match.group('unquoted_value'): # filename=value
            if not filename_star_decoded and not filename: # filename* и filename=".." имеют приоритет
                filename = match.group('unquoted_value').strip()
                
    # Если filename* был успешно декодирован, он имеет приоритет
    final_filename = filename_star_decoded if filename_star_decoded else filename
    
    return ContentDisposition(filename=final_filename if final_filename != filename_star_decoded else filename, 
                              filename_star=filename_star_decoded)


async def download_file(
    url: str,
    request_id: str,
    prefix: str, # "main" or "license"
    temp_download_dir: Path = config.TEMP_DOWNLOAD_DIR,
    download_timeout: int = config.DOWNLOAD_TIMEOUT
) -> Optional[Tuple[str, str, str, int]]: # (temp_file_path, s3_key_filename_part, human_readable_filename, file_size)
    """
    Asynchronously downloads a file from a URL.

    Args:
        url: The URL to download from.
        request_id: Unique ID for logging.
        prefix: "main" or "license" to help name the file and determine forced extension.
        temp_download_dir: Directory to save temporary downloads.
        download_timeout: Timeout for the download operation.

    Returns:
        A tuple (temp_file_path, s3_key_filename_part, human_readable_filename_for_disposition, file_size_bytes)
        or None if download fails.
    """
    task_logger = get_logger("file_downloader", request_id)
    http_headers = {"User-Agent": config.DEFAULT_USER_AGENT}

    if not url:
        task_logger.error("Download URL is empty.")
        return None

    temp_download_dir.mkdir(parents=True, exist_ok=True) # Убедимся, что директория существует

    try:
        async with httpx.AsyncClient(timeout=download_timeout, follow_redirects=True, headers=http_headers) as client_http:
            task_logger.info(f"Starting download from URL: {url} with prefix: {prefix}")
            async with client_http.stream("GET", url) as response:
                response.raise_for_status() # Вызовет исключение для 4xx/5xx ответов

                # --- Логика определения имени файла и расширения ---
                original_filename_base_candidate = None
                original_filename_ext_candidate = ""

                # 1. Попытка извлечь из Content-Disposition
                content_disposition_header = response.headers.get("content-disposition")
                if content_disposition_header:
                    task_logger.debug(f"Content-Disposition header: {content_disposition_header}")
                    parsed_cd = parse_content_disposition(content_disposition_header)
                    # Приоритет filename* (уже декодирован), затем filename
                    filename_from_cd = parsed_cd.filename_star or parsed_cd.filename
                    if filename_from_cd:
                        task_logger.info(f"Filename from Content-Disposition: '{filename_from_cd}'")
                        # Удаляем возможные пути из имени файла (защита)
                        filename_from_cd = os.path.basename(filename_from_cd)
                        base, ext = os.path.splitext(filename_from_cd)
                        if base: original_filename_base_candidate = base
                        if ext: original_filename_ext_candidate = ext.lower()

                # 2. Если не найдено в Content-Disposition, парсим из URL path
                if not original_filename_base_candidate:
                    parsed_url = urlparse(response.url) # Используем response.url для учета редиректов
                    path_component = unquote_plus(os.path.basename(parsed_url.path))
                    if path_component:
                        task_logger.info(f"Filename from URL path: '{path_component}'")
                        base, ext = os.path.splitext(path_component)
                        if base: original_filename_base_candidate = base
                        if ext: original_filename_ext_candidate = ext.lower()
                
                # 3. Принудительное расширение для лицензии
                forced_extension = ".txt" if prefix == "license" else None
                final_extension_to_use = forced_extension or original_filename_ext_candidate

                # 4. Если расширение все еще не определено, пытаемся угадать по Content-Type
                if not final_extension_to_use or final_extension_to_use == ".":
                    content_type = response.headers.get("content-type")
                    if content_type:
                        guessed_ext = mimetypes.guess_extension(content_type.split(';')[0].strip())
                        if guessed_ext:
                            task_logger.info(f"Guessed extension '{guessed_ext}' from Content-Type '{content_type}'")
                            final_extension_to_use = guessed_ext.lower()

                # 5. Расширение по умолчанию, если все остальное не удалось
                if not final_extension_to_use or final_extension_to_use == ".":
                    final_extension_to_use = ".dat"
                    task_logger.warning(f"Could not determine extension, using default '{final_extension_to_use}'")
                
                # Гарантируем, что расширение начинается с точки
                if not final_extension_to_use.startswith('.'):
                    final_extension_to_use = '.' + final_extension_to_use

                # Очищаем базовое имя файла
                cleaned_base_name = clean_filename(original_filename_base_candidate or f"{prefix}_file")
                
                # Формируем имя файла для Content-Disposition в S3
                # Оно должно быть читаемым и отражать оригинальное имя, если возможно
                human_readable_filename_for_disposition = cleaned_base_name + final_extension_to_use
                # Дополнительная очистка и обрезка для Content-Disposition
                human_readable_filename_for_disposition = clean_filename(human_readable_filename_for_disposition, max_length=220) # 220 to be safe with encoding

                # Формируем имя файла для S3 ключа (уникальное)
                s3_key_filename_part = f"{prefix}_{uuid.uuid4().hex[:16]}{final_extension_to_use}"
                
                temp_file_path = temp_download_dir / s3_key_filename_part
                
                task_logger.info(f"Final determined filenames: human_readable='{human_readable_filename_for_disposition}', s3_key_part='{s3_key_filename_part}'")
                task_logger.info(f"Saving temporary file to: {temp_file_path}")

                file_size_bytes = 0
                async with aiofiles.open(temp_file_path, 'wb') as f_write:
                    async for chunk in response.aiter_bytes():
                        await f_write.write(chunk)
                        file_size_bytes += len(chunk)
                
                content_length_header = response.headers.get("Content-Length")
                if content_length_header and content_length_header.isdigit():
                    expected_size = int(content_length_header)
                    if file_size_bytes != expected_size:
                        task_logger.warning(f"Downloaded file size ({file_size_bytes} bytes) does not match Content-Length header ({expected_size} bytes). URL: {url}")
                elif content_length_header:
                    task_logger.warning(f"Content-Length header is present but not a valid number: '{content_length_header}'. URL: {url}")


                task_logger.info(f"Successfully downloaded {file_size_bytes} bytes to {temp_file_path} from {url}")
                return str(temp_file_path), s3_key_filename_part, human_readable_filename_for_disposition, file_size_bytes

    except httpx.HTTPStatusError as e:
        task_logger.error(f"HTTP Status Error while downloading {url}: {e.response.status_code} - {e.response.text[:200]}")
    except httpx.RequestError as e: # Covers ConnectTimeout, ReadTimeout, etc.
        task_logger.error(f"Request Error (e.g., timeout, network issue) while downloading {url}: {e}")
    except asyncio.TimeoutError: # Should be caught by httpx.RequestError (ReadTimeout) but as a fallback
        task_logger.error(f"Asyncio TimeoutError while downloading {url}. This might indicate an issue with httpx timeout handling.")
    except Exception as e:
        task_logger.error(f"Unexpected error while downloading {url}: {e}", exc_info=True)
    
    return None


async def cleanup_temp_file(file_path: Union[str, Path], request_id: Optional[str] = None) -> None:
    """Safely removes a temporary file."""
    task_logger = get_logger("file_cleanup", request_id or "system")
    try:
        p_file_path = Path(file_path)
        if await asyncio.to_thread(p_file_path.exists): # Используем Path.exists()
            await asyncio.to_thread(os.remove, p_file_path)
            task_logger.info(f"Successfully removed temporary file: {p_file_path}")
        else:
            task_logger.warning(f"Attempted to remove non-existent temporary file: {p_file_path}")
    except Exception as e:
        task_logger.error(f"Error removing temporary file {file_path}: {e}")


async def cleanup_temp_directory(temp_dir: Path = config.TEMP_DOWNLOAD_DIR, older_than_hours: Optional[int] = None) -> None:
    """
    Cleans up the temporary download directory.
    If older_than_hours is specified, only files older than that are removed.
    Otherwise, all files and subdirectories are removed.
    """
    logger.info(f"Starting cleanup of temporary directory: {temp_dir}")
    if not await asyncio.to_thread(temp_dir.exists):
        logger.info(f"Temporary directory {temp_dir} does not exist, nothing to clean.")
        return

    if older_than_hours is not None:
        now = asyncio.get_event_loop().time()
        cutoff_time = now - (older_than_hours * 3600)
        for item in await asyncio.to_thread(list, temp_dir.iterdir()):
            try:
                item_stat = await asyncio.to_thread(item.stat)
                if item_stat.st_mtime < cutoff_time:
                    if await asyncio.to_thread(item.is_file):
                        await cleanup_temp_file(item, "temp_dir_cleanup_old")
                    elif await asyncio.to_thread(item.is_dir):
                        # Рекурсивное удаление старых поддиректорий (если нужно)
                        # Для простоты, можно просто логировать или удалять если пуста
                        logger.warning(f"Found old directory in temp: {item}. Manual cleanup might be needed or implement recursive delete.")
            except Exception as e:
                logger.error(f"Error processing item {item} during old file cleanup: {e}")
    else: # Full cleanup
        try:
            # shutil.rmtree не асинхронный, выполняем в потоке
            await asyncio.to_thread(shutil.rmtree, temp_dir)
            logger.info(f"Successfully removed temporary directory: {temp_dir}")
            # Воссоздаем директорию после полной очистки
            await asyncio.to_thread(temp_dir.mkdir, parents=True, exist_ok=True)
            logger.info(f"Re-created temporary directory: {temp_dir}")
        except Exception as e:
            logger.error(f"Error removing temporary directory {temp_dir}: {e}")