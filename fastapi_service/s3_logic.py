import asyncio
import functools
import mimetypes
import os
from typing import Optional, Tuple
from urllib.parse import quote as url_quote

import boto3
import botocore.exceptions
from botocore.config import Config as BotoConfig

from . import config
from .utils import get_logger

logger = get_logger(__name__)

# --- S3 Client Initialization ---
s3_client = None
if config.S3_CONFIGURED:
    try:
        # Настройки для boto3, включая таймауты
        # connect_timeout и read_timeout для S3 операций
        # Увеличение max_pool_connections может быть полезно при высокой нагрузке
        boto_config = BotoConfig(
            connect_timeout=config.UPLOAD_TIMEOUT / 2, # Таймаут на соединение
            read_timeout=config.UPLOAD_TIMEOUT,      # Таймаут на чтение ответа
            retries={'max_attempts': config.MAX_UPLOAD_RETRIES + 1} # Общее количество попыток
        )

        session = boto3.session.Session()
        s3_client = session.client(
            service_name='s3',
            aws_access_key_id=config.S3_ACCESS_KEY_ID,
            aws_secret_access_key=config.S3_SECRET_ACCESS_KEY,
            endpoint_url=config.S3_ENDPOINT_URL,
            region_name=config.S3_REGION_NAME, # Может быть None, если не требуется
            config=boto_config
        )
        # Проверка доступности бакета (опционально, может замедлить старт)
        # try:
        #     s3_client.head_bucket(Bucket=config.S3_BUCKET_NAME)
        #     logger.info(f"Successfully connected to S3 bucket: {config.S3_BUCKET_NAME}")
        # except botocore.exceptions.ClientError as e:
        #     logger.error(f"Failed to connect to S3 bucket {config.S3_BUCKET_NAME}: {e}. S3 uploads might fail.")
        #     s3_client = None # Сбрасываем клиент, если бакет недоступен
        logger.info(f"S3 client initialized for bucket: {config.S3_BUCKET_NAME}")

    except Exception as e:
        logger.error(f"Failed to initialize S3 client: {e}")
        s3_client = None
else:
    logger.warning("S3 client not initialized because S3 is not configured.")


async def upload_file_to_s3(
    file_path: str,
    request_id: str,
    s3_filename_component: str, # Часть имени файла для S3 ключа (e.g., "main_uuid.zip")
    original_human_readable_filename: str # Полное имя файла для Content-Disposition
) -> Optional[str]:
    """
    Uploads a file to S3-compatible storage.

    Args:
        file_path: Path to the local file to upload.
        request_id: Unique ID for logging.
        s3_filename_component: The unique part of the S3 key (e.g., "main_uuid.zip" or "license_uuid.txt").
        original_human_readable_filename: The desired filename for download by the end-user.

    Returns:
        The S3 key if upload was successful, otherwise None.
    """
    task_logger = get_logger("s3_uploader", request_id)

    if not config.S3_CONFIGURED or s3_client is None:
        task_logger.error("S3 is not configured or S3 client is not available. Cannot upload.")
        return None

    if not os.path.exists(file_path):
        task_logger.error(f"File not found for S3 upload: {file_path}")
        return None

    # Формируем полный S3 ключ
    if config.S3_ENVATO_FOLDER_PATH:
        # Убираем возможные / в начале s3_filename_component, так как S3_ENVATO_FOLDER_PATH уже обработан
        actual_s3_key_for_upload = f"{config.S3_ENVATO_FOLDER_PATH.strip('/')}/{s3_filename_component.lstrip('/')}"
    else:
        actual_s3_key_for_upload = s3_filename_component.lstrip('/')
    
    # Убираем возможные двойные // в ключе
    actual_s3_key_for_upload = actual_s3_key_for_upload.replace('//', '/')


    # Определяем Content-Type
    content_type, _ = mimetypes.guess_type(original_human_readable_filename)
    if not content_type:
        content_type = 'application/octet-stream'
        task_logger.warning(f"Could not guess Content-Type for {original_human_readable_filename}, using {content_type}.")

    # Формируем Content-Disposition
    # RFC 5987 для не-ASCII символов в filename*
    try:
        original_human_readable_filename.encode('ascii')
        content_disposition_header = f'attachment; filename="{original_human_readable_filename}"'
    except UnicodeEncodeError:
        encoded_filename = url_quote(original_human_readable_filename, encoding='utf-8')
        content_disposition_header = f"attachment; filename*=UTF-8''{encoded_filename}"

    extra_args = {
        'ContentType': content_type,
        'ContentDisposition': content_disposition_header
    }
    # Можно добавить другие ExtraArgs, например, 'ACL': 'public-read', если нужно

    task_logger.info(f"Attempting to upload '{file_path}' to S3 key '{actual_s3_key_for_upload}' in bucket '{config.S3_BUCKET_NAME}'.")
    task_logger.debug(f"Upload ExtraArgs: {extra_args}")

    for attempt in range(config.MAX_UPLOAD_RETRIES + 1):
        try:
            loop = asyncio.get_event_loop()
            # Используем functools.partial для передачи аргументов в s3_client.upload_file
            upload_callable = functools.partial(
                s3_client.upload_file,
                Filename=file_path,
                Bucket=config.S3_BUCKET_NAME,
                Key=actual_s3_key_for_upload,
                ExtraArgs=extra_args
            )
            
            # Выполняем блокирующую операцию в отдельном потоке
            await loop.run_in_executor(None, upload_callable)
            
            task_logger.info(f"Successfully uploaded '{file_path}' to S3 key '{actual_s3_key_for_upload}'. Attempt {attempt + 1}.")
            return actual_s3_key_for_upload

        except FileNotFoundError:
            task_logger.error(f"S3 Upload Error (Attempt {attempt + 1}): File {file_path} not found during upload operation (should not happen if pre-check passed).")
            break # Нет смысла повторять, если файл исчез
        except (botocore.exceptions.NoCredentialsError, botocore.exceptions.PartialCredentialsError) as e:
            task_logger.error(f"S3 Upload Error (Attempt {attempt + 1}): Credentials error. {e}")
            break # Нет смысла повторять при проблемах с авторизацией
        except botocore.exceptions.ClientError as e:
            error_code = e.response.get('Error', {}).get('Code')
            error_message = e.response.get('Error', {}).get('Message')
            task_logger.error(f"S3 Upload Error (Attempt {attempt + 1}): ClientError - Code: {error_code}, Message: {error_message}. Full error: {e}")
            # Некоторые ошибки могут быть временными (e.g., 'InternalError', 'SlowDown')
            # Другие, как 'AccessDenied', не стоит повторять
            if error_code in ['AccessDenied', 'NoSuchBucket', 'InvalidAccessKeyId']:
                 break # Не повторяем для критических ошибок конфигурации/доступа
            if attempt < config.MAX_UPLOAD_RETRIES:
                task_logger.info(f"Retrying S3 upload in {config.UPLOAD_RETRY_DELAY} seconds...")
                await asyncio.sleep(config.UPLOAD_RETRY_DELAY)
            else:
                task_logger.error("Max S3 upload retries reached.")
                break
        except Exception as e:
            task_logger.error(f"S3 Upload Error (Attempt {attempt + 1}): Unexpected error during S3 upload of {file_path}: {e}", exc_info=True)
            # Неизвестная ошибка, возможно, стоит сделать одну попытку повтора
            if attempt < config.MAX_UPLOAD_RETRIES:
                task_logger.info(f"Retrying S3 upload due to unexpected error in {config.UPLOAD_RETRY_DELAY} seconds...")
                await asyncio.sleep(config.UPLOAD_RETRY_DELAY)
            else:
                break # Прекращаем попытки после неизвестной ошибки на последней попытке

    task_logger.error(f"Failed to upload '{file_path}' to S3 after all attempts.")
    return None


def construct_s3_public_url(s3_key: str) -> Optional[str]:
    """
    Constructs a full public URL for an S3 object if S3_PUBLIC_BASE_URL is set.
    Otherwise, returns the S3 key.
    """
    if not s3_key:
        return None
        
    if config.S3_PUBLIC_BASE_URL:
        base_url = config.S3_PUBLIC_BASE_URL.rstrip('/')
        # Убедимся, что ключ не начинается со слеша, если base_url уже его содержит
        # или что он начинается со слеша, если base_url его не содержит.
        # Проще всего - base_url/key.lstrip('/')
        return f"{base_url}/{s3_key.lstrip('/')}"
    else:
        # Если базовый URL не задан, возвращаем просто ключ S3
        return s3_key