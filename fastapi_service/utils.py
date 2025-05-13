import asyncio
import json
import logging
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, Union
from urllib.parse import unquote_plus, urlparse

import pytz
from fastapi import Request

from . import config # Импортируем наш config

# --- Logging Setup ---
class RequestIdAdapter(logging.LoggerAdapter):
    def process(self, msg: str, kwargs: Any) -> Tuple[str, Any]:
        request_id = self.extra.get("request_id", "N/A")
        # Ensure request_id is always part of the message format
        # This assumes your formatter string includes `%(request_id)s`
        return msg, kwargs


def setup_logging(log_level: str = config.LOG_LEVEL, log_file: Path = config.LOG_FILE_PATH_FOR_DOWNLOAD) -> None:
    """
    Configures logging for the application.
    """
    # Ensure log directory exists
    log_file.parent.mkdir(parents=True, exist_ok=True)

    # Base formatter
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - [%(request_id)s] - %(module)s:%(lineno)d - %(message)s"
    )

    # Console Handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    # File Handler
    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    file_handler.setFormatter(formatter)

    # Get root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    # Set higher log levels for noisy libraries
    noisy_libraries = ["telethon", "httpx", "httpcore", "botocore", "boto3", "s3transfer", "uvicorn.access", "urllib3"]
    for lib_name in noisy_libraries:
        logging.getLogger(lib_name).setLevel(logging.WARNING)

    # Specific uvicorn error logger
    logging.getLogger("uvicorn.error").setLevel(log_level) # Match app's log level for uvicorn errors

    # Initial log message
    initial_logger = get_logger("init")
    initial_logger.info(f"Logging configured. Level: {log_level}. Log file: {log_file}")

def get_logger(name: str, request_id: Optional[str] = None) -> RequestIdAdapter:
    """
    Returns a logger instance wrapped with RequestIdAdapter.
    """
    logger = logging.getLogger(name)
    # If request_id is not provided, a default will be used by the adapter or formatter
    extra = {"request_id": request_id or "system"}
    return RequestIdAdapter(logger, extra)

# --- JSON File Handling ---
async def load_json_data(filepath: Path, lock: asyncio.Lock, default_factory: callable = dict) -> Dict:
    """
    Asynchronously loads JSON data from a file with locking.
    Returns default_factory() if file not found or JSON is invalid.
    """
    async with lock:
        if not await asyncio.to_thread(filepath.exists):
            # logger.warning(f"File not found: {filepath}. Returning default.")
            return default_factory()
        try:
            async with await asyncio.to_thread(open, filepath, "r", encoding="utf-8") as f:
                content = await f.read()
                if not content.strip(): # Handle empty file
                    # logger.warning(f"File is empty: {filepath}. Returning default.")
                    return default_factory()
                return json.loads(content)
        except FileNotFoundError:
            # logger.warning(f"File not found during read (race condition?): {filepath}. Returning default.")
            return default_factory()
        except json.JSONDecodeError as e:
            logger = get_logger("json_utils")
            logger.error(f"Error decoding JSON from {filepath}: {e}. Returning default.")
            # Optionally, create a backup of the corrupted file
            try:
                corrupted_backup_path = filepath.with_suffix(f".corrupted.{datetime.now().strftime('%Y%m%d%H%M%S')}.json")
                await asyncio.to_thread(os.rename, filepath, corrupted_backup_path)
                logger.info(f"Corrupted file {filepath} backed up to {corrupted_backup_path}")
            except Exception as backup_err:
                logger.error(f"Could not back up corrupted file {filepath}: {backup_err}")
            return default_factory()
        except Exception as e:
            logger = get_logger("json_utils")
            logger.error(f"Unexpected error loading JSON from {filepath}: {e}. Returning default.")
            return default_factory()

async def save_json_data(filepath: Path, data: Dict, lock: asyncio.Lock) -> bool:
    """
    Asynchronously saves dictionary data to a JSON file with locking.
    """
    logger = get_logger("json_utils")
    async with lock:
        try:
            # Create a temporary file for atomic write
            temp_filepath = filepath.with_suffix(f"{filepath.suffix}.tmp")
            async with await asyncio.to_thread(open, temp_filepath, "w", encoding="utf-8") as f:
                await f.write(json.dumps(data, indent=4, ensure_ascii=False, default=str)) # default=str for datetime etc.
            
            # Replace the original file with the temporary file
            await asyncio.to_thread(os.replace, temp_filepath, filepath)
            # logger.debug(f"Data successfully saved to {filepath}")
            return True
        except Exception as e:
            logger.error(f"Error saving JSON data to {filepath}: {e}")
            # Attempt to remove temp file if it exists
            if await asyncio.to_thread(temp_filepath.exists):
                try:
                    await asyncio.to_thread(os.remove, temp_filepath)
                except Exception as rm_err:
                    logger.error(f"Could not remove temporary file {temp_filepath} after save error: {rm_err}")
            return False

# --- URL Extraction ---
def extract_url_from_text(text: str, request_id: Optional[str] = None) -> Optional[str]:
    """
    Extracts the first well-formed URL from a given text.
    Cleans up common trailing punctuation if the URL has no query/fragment.
    Handles URLs enclosed in parentheses if they are not part of a balanced pair.
    """
    logger = get_logger("url_extractor", request_id)
    if not text:
        return None

    # Regex to find URLs, including those starting with www.
    # It's a bit more permissive to catch various forms.
    pattern = r'https?://[^\s<>"]+|www\.[^\s<>"]+'
    match = re.search(pattern, text, re.IGNORECASE)

    if not match:
        logger.debug(f"No URL found in text: '{text[:100]}...'")
        return None

    url = match.group(0)
    original_url_for_log = url

    # Cleanup logic
    parsed_url = urlparse(url)

    # Only apply punctuation stripping if there's no query or fragment,
    # as punctuation might be part of them.
    if not parsed_url.query and not parsed_url.fragment:
        # Strip common trailing punctuation
        url = re.sub(r'[.,;:!?]+$', '', url)

    # Handle URLs enclosed in parentheses, e.g., (http://example.com)
    # This is a common pattern in some messages.
    # We check if the parenthesis is likely part of the message structure rather than the URL itself.
    # Example: "Link: (http://example.com)" -> "http://example.com"
    # But not: "http://example.com/path(info)"
    
    # Find the match start and end in the original text
    match_start, match_end = match.span()

    # Check for preceding '(' and succeeding ')'
    has_preceding_paren = match_start > 0 and text[match_start - 1] == '('
    has_succeeding_paren = match_end < len(text) and text[match_end] == ')'

    if url.endswith(')') and not url.endswith('())') and not parsed_url.path.endswith(')') and not parsed_url.query and not parsed_url.fragment:
        # More careful check: if the URL itself ends with ')' but it's not part of a query/fragment
        # and it's not like `http://domain.com/file(1).zip)`
        # This is tricky. A simpler approach might be to just remove it if it's the absolute last char.
        # Let's refine the parenthesis removal:
        # If the extracted URL ends with ')' and this ')' is immediately after the match in the original text,
        # and there was a '(' immediately before the match.
        if has_preceding_paren and has_succeeding_paren and url.endswith(')'):
             # Check if the character before the opening parenthesis is not a closing one (to avoid ((url)))
            if not (match_start > 1 and text[match_start - 2] == ')'):
                url = url[:-1]


    logger.info(f"Extracted URL: '{url}' from text (original matched: '{original_url_for_log}')")
    return url


# --- Datetime Helpers ---
def get_utc_now() -> datetime:
    """Returns the current time in UTC."""
    return datetime.now(pytz.utc)

def get_utc_today_str() -> str:
    """Returns the current date in UTC as 'YYYY-MM-DD' string."""
    return get_utc_now().strftime('%Y-%m-%d')

# --- Request ID Helper ---
def get_request_id(request: Optional[Request] = None) -> str:
    """
    Generates or retrieves a request ID.
    If a request object is provided, it tries to get 'X-Request-ID' header.
    Otherwise, generates a new UUID.
    """
    if request and request.headers.get("X-Request-ID"):
        return request.headers.get("X-Request-ID")
    return uuid.uuid4().hex[:12] # Shorter UUID for readability

# --- String Cleaning for Filenames ---
def clean_filename(filename: str, max_length: int = 200) -> str:
    """
    Cleans a filename by removing disallowed characters, replacing spaces,
    and truncating to a maximum length.
    """
    if not filename:
        return ""
    
    # Remove or replace characters not typically allowed or problematic in filenames
    # Allow alphanumeric, spaces, dots, hyphens, underscores
    cleaned = re.sub(r'[^\w\s\.\-_]', '', filename)
    
    # Replace multiple spaces/underscores/hyphens with a single underscore
    cleaned = re.sub(r'[\s_]+', '_', cleaned)
    cleaned = re.sub(r'[-]+', '-', cleaned) # Keep hyphens if user wants them

    # Remove leading/trailing underscores/hyphens/dots
    cleaned = cleaned.strip('._-')

    if not cleaned: # If all characters were removed
        cleaned = "file"

    # Truncate if too long, preserving extension if possible
    if len(cleaned) > max_length:
        name_part, ext_part = os.path.splitext(cleaned)
        # Max length for name part, considering a dot and extension (e.g., up to 5 chars for ext)
        max_name_len = max_length - (len(ext_part) if len(ext_part) < 6 else 5)
        if len(name_part) > max_name_len:
            name_part = name_part[:max_name_len]
        cleaned = name_part + ext_part
        
    return cleaned