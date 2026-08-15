"""Startup helpers for the bot process."""

import logging
import os
import signal
import sys
from pathlib import Path

from telegram.error import Conflict, InvalidToken

logger = logging.getLogger(__name__)

# sysexits.h EX_CONFIG — the configuration is wrong, so retrying cannot help.
EXIT_INVALID_TOKEN = 78
EXIT_ALREADY_RUNNING = 77

ALLOWED_UPDATES = ["message", "callback_query"]
LOCK_FILENAME = ".bot.pid"


def _is_pid_running(pid: int) -> bool:
    """Check if a process with the given PID is still alive."""
    try:
        os.kill(pid, 0)  # Signal 0 doesn't kill, just checks existence
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we don't have permission to signal it
        return True


def acquire_lock(download_path: str) -> Path:
    """Acquire a PID-file lock to prevent duplicate bot instances.

    If a valid lock file exists and the owning process is still alive,
    the current process exits with an error.  Stale locks (process dead)
    are silently removed.

    Returns the path to the lock file (for cleanup on shutdown).
    """
    lock_path = Path(download_path) / LOCK_FILENAME

    if lock_path.exists():
        try:
            old_pid = int(lock_path.read_text().strip())
            if _is_pid_running(old_pid):
                logger.error(
                    "Another bot instance is already running (PID %d). "
                    "Shutting down to avoid Telegram API conflicts. "
                    "If the old process is dead, delete %s and try again.",
                    old_pid, lock_path,
                )
                sys.exit(EXIT_ALREADY_RUNNING)
            else:
                logger.warning(
                    "Stale lock file found (PID %d is dead). Removing it.",
                    old_pid,
                )
                lock_path.unlink()
        except (ValueError, OSError):
            logger.warning("Corrupt lock file %s — removing it.", lock_path)
            try:
                lock_path.unlink()
            except OSError:
                pass

    # Write our PID
    lock_path.write_text(str(os.getpid()))
    logger.info("Lock acquired: %s (PID %d)", lock_path, os.getpid())
    return lock_path


def release_lock(lock_path: Path) -> None:
    """Release the PID-file lock on clean shutdown."""
    try:
        if lock_path.exists():
            lock_path.unlink()
            logger.info("Lock released: %s", lock_path)
    except OSError:
        logger.exception("Failed to release lock file %s", lock_path)


def run_bot(app, lock_path: Path | None = None) -> None:
    """Run polling, turning a rejected token into one readable fatal error.

    An invalid token is the one startup failure that no amount of restarting
    will fix, so it is reported as a single actionable line rather than being
    allowed to crash the process with a traceback.

    A Conflict error means another bot instance is polling the same token;
    we exit instead of retrying in a loop that would spam the API.
    """
    try:
        app.run_polling(allowed_updates=ALLOWED_UPDATES)
    except InvalidToken:
        # The exception is deliberately neither re-raised nor passed to
        # logger.exception: its message quotes the token verbatim, and a
        # traceback adds nothing to a cause that is already known.
        logger.error(
            "Telegram rejected TELEGRAM_BOT_TOKEN (HTTP 401 Unauthorized). "
            "The token was most likely revoked in @BotFather. Issue a new one "
            "(/mybots -> pick the bot -> API Token), update TELEGRAM_BOT_TOKEN "
            "and start again. Restarting with the same token will not help."
        )
        sys.exit(EXIT_INVALID_TOKEN)
    except Conflict:
        logger.error(
            "Telegram API conflict: another bot instance is already polling "
            "with the same token. Shutting down to avoid conflicts. "
            "Make sure only ONE container/process is running per bot token."
        )
        sys.exit(EXIT_ALREADY_RUNNING)
    finally:
        # Always clean up the lock file on exit
        if lock_path:
            release_lock(lock_path)
