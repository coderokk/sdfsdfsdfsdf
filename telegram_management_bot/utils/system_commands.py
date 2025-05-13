# telegram_management_bot/utils/system_commands.py
import asyncio
import logging
from typing import Tuple, Optional

logger = logging.getLogger(__name__)

async def execute_system_command(command: str, timeout: int = 60) -> Tuple[bool, str, str]:
    """
    Выполняет системную команду и возвращает результат.

    Args:
        command: Команда для выполнения.
        timeout: Таймаут в секундах для выполнения команды.

    Returns:
        Кортеж (success: bool, stdout: str, stderr: str)
    """
    logger.info(f"Executing system command: {command}")
    try:
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        # Ожидаем завершения процесса с таймаутом
        stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=timeout)
        
        stdout = stdout_bytes.decode(errors='replace').strip()
        stderr = stderr_bytes.decode(errors='replace').strip()
        
        if process.returncode == 0:
            logger.info(f"Command '{command}' executed successfully. STDOUT: {stdout[:200]}")
            return True, stdout, stderr
        else:
            logger.error(f"Command '{command}' failed with return code {process.returncode}. STDERR: {stderr}. STDOUT: {stdout[:200]}")
            return False, stdout, stderr

    except asyncio.TimeoutError:
        logger.error(f"Command '{command}' timed out after {timeout} seconds.")
        # Попытка убить процесс, если он завис (может потребовать прав)
        if process and process.returncode is None:
            try:
                process.terminate()
                await asyncio.wait_for(process.wait(), timeout=5) # Даем время на завершение
                if process.returncode is None: # Если terminate не сработал
                    process.kill()
                    await asyncio.wait_for(process.wait(), timeout=5)
                logger.info(f"Process for command '{command}' terminated/killed due to timeout.")
            except Exception as e_kill:
                logger.error(f"Failed to terminate/kill process for command '{command}' after timeout: {e_kill}")
        return False, "", f"Command timed out after {timeout} seconds."
    except Exception as e:
        logger.error(f"Error executing command '{command}': {e}", exc_info=True)
        return False, "", f"Error executing command: {str(e)}"

async def restart_fastapi_service(command: Optional[str] = None) -> Tuple[bool, str]:
    """Перезапускает FastAPI сервис."""
    cmd_to_run = command or bot_config.DEFAULT_RESTART_COMMAND_FASTAPI
    if not cmd_to_run:
        return False, "FastAPI restart command is not configured."
        
    logger.info(f"Attempting to restart FastAPI service with command: {cmd_to_run}")
    success, stdout, stderr = await execute_system_command(cmd_to_run)
    if success:
        return True, f"FastAPI service restart command executed. Output:\n{stdout}\n{stderr}".strip()
    else:
        return False, f"Failed to restart FastAPI service. Output:\n{stdout}\n{stderr}".strip()

async def restart_bot_service(command: Optional[str] = None) -> Tuple[bool, str]:
    """Перезапускает самого бота (требует внешнего менеджера процессов)."""
    cmd_to_run = command or bot_config.DEFAULT_RESTART_COMMAND_BOT
    if not cmd_to_run:
        return False, "Bot restart command is not configured."

    logger.info(f"Attempting to restart Bot service with command: {cmd_to_run}")
    # Эта команда, скорее всего, убьет текущий процесс бота,
    # поэтому ответ может не успеть отправиться пользователю, если команда выполняется синхронно
    # или если бот не запущен под менеджером процессов типа systemd/supervisor.
    # Для systemd/supervisor команда обычно возвращает управление сразу.
    success, stdout, stderr = await execute_system_command(cmd_to_run)
    # Ответ ниже может не дойти, если бот успешно перезапустился и текущий процесс умер
    if success:
        return True, f"Bot service restart command executed. Output:\n{stdout}\n{stderr}".strip()
    else:
        return False, f"Failed to restart Bot service. Output:\n{stdout}\n{stderr}".strip()