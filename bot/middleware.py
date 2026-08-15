import logging
from collections.abc import Callable, Coroutine
from functools import wraps
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

from bot.keyboards import ACCESS_PREFIX, ADMIN_PREFIX, request_access_keyboard
from bot.user_service import user_service
from config import get_config

logger = logging.getLogger(__name__)


def whitelist_only(func: Callable[[Update, ContextTypes.DEFAULT_TYPE], Coroutine[Any, Any, Any]]):
    """Decorator to restrict bot access to whitelisted users only.

    Uses hybrid authorization:
    1. First checks env-based allowed_user_ids (always takes priority)
    2. Then checks DB for approved users
    3. Auto-approves new users on first access
    4. Shows "Request Access" button for unauthorized users

    Note: ACCESS_PREFIX callbacks are always allowed (so users can request access).
    ADMIN_PREFIX callbacks require the user to be the admin.
    """

    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        config = get_config()
        user = update.effective_user

        if user is None:
            logger.warning("Received update without user information")
            return

        # Allow ACCESS_PREFIX callbacks from anyone (so they can request access)
        if update.callback_query:
            callback_data = update.callback_query.data or ""
            if callback_data.startswith(ACCESS_PREFIX):
                return await func(update, context)
            # ADMIN_PREFIX requires being the admin
            if callback_data.startswith(ADMIN_PREFIX):
                if config.admin_user_id and user.id == config.admin_user_id:
                    return await func(update, context)
                else:
                    await update.callback_query.answer("فقط ادمین می‌تواند این کار را انجام دهد", show_alert=True)
                    return

        # Check if user is allowed (env whitelist or DB approved)
        if user_service.is_user_allowed(user.id):
            return await func(update, context)

        # Auto-approve new users on first access
        user_status = user_service.get_user_status(user.id)
        if user_status is None or user_status == "":
            # New user - auto-approve them
            user_service.approve_user(user.id, approved_by=None)
            logger.info(f"Auto-approved new user {user.id} (@{user.username})")
            return await func(update, context)

        # User has a status but isn't approved
        logger.warning(f"Unauthorized access attempt from user {user.id} (@{user.username}), status: {user_status}")

        if update.message:
            if user_status == "pending":
                await update.message.reply_text(
                    "⏳ درخواست دسترسی شما در انتظار تایید است.\n"
                    "وقتی ادمین درخواست شما را بررسی کند، مطلع می‌شوید."
                )
            elif user_status == "denied":
                await update.message.reply_text(
                    "⛔ درخواست دسترسی شما رد شد.\n"
                    "این ربات خصوصی است و فقط برای استفاده شخصی است."
                )
            else:
                # No existing request - show request access button
                await update.message.reply_text(
                    "⛔ شما مجاز به استفاده از این ربات نیستید.\n"
                    "این ربات خصوصی است و فقط برای استفاده شخصی است.\n\n"
                    "می‌توانید از ادمین درخواست دسترسی کنید:",
                    reply_markup=request_access_keyboard(),
                )
        return

    return wrapper
