import logging
import platform
import time
from pathlib import Path

from telegram import InputFile, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot.downloader import (
    Downloader,
    DownloadQuality,
    DownloadTask,
    DynamicQuality,
    download_queue,
)
from bot.file_server_client import file_server_client
from bot.keyboards import (
    ACCESS_PREFIX,
    ADMIN_PREFIX,
    CANCEL_PREFIX,
    CONFIRM_PREFIX,
    DELETE_PREFIX,
    FORMAT_PREFIX,
    QUALITY_PREFIX,
    admin_access_decision_keyboard,
    audio_quality_keyboard,
    dynamic_audio_quality_keyboard,
    dynamic_video_quality_keyboard,
    file_delete_keyboard,
    format_selection_keyboard,
    parse_callback_data,
    playlist_confirmation_keyboard,
    video_quality_keyboard,
)
from bot.llm_service import llm_service
from bot.middleware import whitelist_only
from bot.stats_service import stats_service
from bot.storage import detect_platform, is_file_within_limit
from bot.user_service import user_service
from config import get_config

logger = logging.getLogger(__name__)

# Captured at module import time so /about can report uptime since the
# bot process started, not since the first message was handled.
_BOT_START_TIME = time.time()

# Store active downloads by message_id
active_downloads: dict[int, dict] = {}


def _format_uptime(seconds: float) -> str:
    """Format a duration in seconds as e.g. '2d 4h 17m' or '47s' for short uptimes."""
    total = int(seconds)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    if minutes or hours or days:
        parts.append(f"{minutes}m")
    if not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)


def _get_local_ip() -> str:
    """Return the primary outbound LAN IP, or 'unavailable' if not determinable.

    Uses a UDP socket connect to a public address — no packets are actually
    sent, but the kernel picks the source IP of the route to that address.
    """
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "unavailable"
    finally:
        s.close()


async def _get_public_ip() -> str:
    """Fetch the public IPv4 from api.ipify.org, or 'unavailable' on failure."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get("https://api.ipify.org")
            response.raise_for_status()
            return response.text.strip() or "unavailable"
    except Exception:  # noqa: BLE001 — best-effort boundary: an optional feature must not take the bot down
        return "unavailable"


@whitelist_only
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    await update.message.reply_text(
        "👋 به ربات دانلودر خوش آمدید!\n\n"
        "یک لینک از این سایت‌ها بفرستید:\n"
        "• یوتیوب\n"
        "• اینستاگرام\n"
        "• توییتر/ایکس\n"
        "• فیسبوک\n"
        "• تیک‌تاک\n"
        "• و بیش از ۱۰۰۰ سایت دیگر!\n\n"
        "همچنین می‌توانید از زبان طبیعی استفاده کنید:\n"
        "• «دانلود صدا از <link>»\n"
        "• «این ویدیو رو بگیر <link>»\n\n"
        "دستورات:\n"
        "/help - نمایش راهنما\n"
        "/status - وضعیت صف\n"
        "/stats - آمار دانلودها"
    )


@whitelist_only
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command."""
    await update.message.reply_text(
        "📖 *راهنمای استفاده:*\n\n"
        "*۱. ارسال لینک*\n"
        "کافیه لینک مورد نظر رو بفرستید تا فرمت و کیفیت رو انتخاب کنید.\n\n"
        "*۲. زبان طبیعی*\n"
        "می‌توانید مثلاً بگویید:\n"
        "• «صدا رو از یوتیوب دانلود کن»\n"
        "• «این ویدیو رو با بهترین کیفیت بگیر»\n\n"
        "*۳. تنظیمات کیفیت*\n"
        "🎵 صدا: ۱۲۸kbps، ۱۹۲kbps، ۳۲۰kbps، بهترین\n"
        "🎬 ویدیو: ۴۸۰p، ۷۲۰p، ۱۰۸۰p، بهترین\n\n"
        "*۴. محدودیت فایل*\n"
        "فایل‌های تا ۵۰MB مستقیماً در تلگرام ارسال می‌شوند.\n"
        "فایل‌های بزرگتر در سرور ذخیره می‌شوند.\n\n"
        "*پلتفرم‌های پشتیبانی‌شده:*\n"
        "یوتیوب، اینستاگرام، توییتر/ایکس، فیسبوک، تیک‌تاک، ویمئو، ردیت، توئیچ و بیش از ۱۰۰۰ سایت دیگر!\n\n"
        "*سایر دستورات:*\n"
        "/status - وضعیت صف\n"
        "/stats - آمار دانلودها\n"
        "/health - سلامت سیستم\n"
        "/about - نسخه ربات و اطلاعات میزبان",
        parse_mode="Markdown"
    )


@whitelist_only
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /status command."""
    active, queued = await download_queue.get_queue_status()

    await update.message.reply_text(
        f"📊 *وضعیت صف*\n\n"
        f"دانلودهای فعال: {active}\n"
        f"آیتم‌های در صف: {queued}",
        parse_mode="Markdown"
    )


@whitelist_only
async def health_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /health command - show system health status."""
    import shutil
    from pathlib import Path

    config = get_config()
    text = "🏥 *سلامت سیستم*\n\n"

    # Queue status
    active, queued = await download_queue.get_queue_status()
    text += "*صف دانلود:*\n"
    text += f"• فعال: {active}\n"
    text += f"• در صف: {queued}\n\n"

    # Disk space
    download_path = Path(config.download_path)
    if download_path.exists():
        total, used, free = shutil.disk_usage(download_path)
        free_gb = free / (1024**3)
        used_gb = used / (1024**3)
        total_gb = total / (1024**3)
        usage_percent = (used / total) * 100

        text += "*فضای دیسک:*\n"
        text += f"• آزاد: {free_gb:.1f} GB\n"
        text += f"• استفاده‌شده: {used_gb:.1f} GB ({usage_percent:.0f}%)\n"
        text += f"• کل: {total_gb:.1f} GB\n\n"
    else:
        text += "*فضای دیسک:* ⚠️ مسیر دانلود پیدا نشد\n\n"

    # Ollama status
    text += "*Ollama LLM:*\n"
    try:
        ollama_available = await llm_service.is_available()
        if ollama_available:
            text += "• وضعیت: ✅ متصل\n"
            text += f"• Model: {config.ollama_model}\n\n"
        else:
            text += "• وضعیت: ❌ قطع\n\n"
    except Exception:  # noqa: BLE001 — best-effort boundary: an optional feature must not take the bot down
        text += "• وضعیت: ❌ خطا در بررسی\n\n"

    # File server status
    text += "*File Server:*\n"
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{config.file_server_url}/health")
            if response.status_code == 200:
                text += "• وضعیت: ✅ متصل\n"
            else:
                text += f"• Status: ⚠️ Error ({response.status_code})\n"
    except Exception:  # noqa: BLE001 — best-effort boundary: an optional feature must not take the bot down
        text += "• وضعیت: ❌ قطع\n"

    await update.message.reply_text(text, parse_mode="Markdown")


@whitelist_only
async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /about command - show bot metadata and host details."""
    import yt_dlp

    from config import __version__

    uptime = _format_uptime(time.time() - _BOT_START_TIME)
    local_ip = _get_local_ip()
    public_ip = await _get_public_ip()

    text = (
        "ℹ️ *درباره این ربات*\n\n"
        "*🤖 ربات*\n"
        f"• نام: ytdlp-telegram\n"
        f"• نسخه: `{__version__}`\n"
        "• مجوز: MIT\n"
        "• Source: https://github.com/driversti/ytdlp-telegram\n\n"
        "*🖥️ سرور*\n"
        f"• میزبان: `{platform.node() or 'نامشخص'}`\n"
        f"• سیستم‌عامل: {platform.system()} {platform.release()}\n"
        f"• معماری: {platform.machine()}\n"
        f"• IP محلی: `{local_ip}`\n"
        f"• IP عمومی: `{public_ip}`\n\n"
        "*🐍 محیط اجرا*\n"
        f"• پایتون: {platform.python_version()}\n"
        f"• yt-dlp: {yt_dlp.version.__version__}\n\n"
        f"*⏱️ زمان کارکرد:* {uptime}"
    )

    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


@whitelist_only
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /stats command - show download statistics."""
    user_id = update.effective_user.id
    overall = stats_service.get_overall_stats()
    user_stats = stats_service.get_user_stats(user_id)

    text = "📈 *آمار دانلودها*\n\n"

    # Overall stats
    text += "*کلی:*\n"
    text += f"• کل دانلودها: {overall.total_downloads}\n"
    text += f"• حجم کل: {overall.total_size_mb:.1f} MB\n"
    text += f"• این ماه: {overall.downloads_this_month} دانلود ({overall.size_this_month_mb:.1f} MB)\n\n"

    # Platform breakdown
    if overall.platforms:
        text += "*بر اساس پلتفرم:*\n"
        for platform, count in list(overall.platforms.items())[:5]:
            text += f"• {platform.capitalize()}: {count}\n"
        text += "\n"

    # User stats
    if user_stats:
        text += "*آمار شما:*\n"
        text += f"• دانلودها: {user_stats.total_downloads}\n"
        text += f"• حجم کل: {user_stats.total_size_mb:.1f} MB\n"
        text += f"• صدا: {user_stats.audio_downloads} | ویدیو: {user_stats.video_downloads}\n"
        text += f"• پلتفرم محبوب: {user_stats.favorite_platform.capitalize()}\n"
    else:
        text += "_شما هنوز چیزی دانلود نکرده‌اید!_"

    await update.message.reply_text(text, parse_mode="Markdown")


@whitelist_only
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming text messages."""
    message = update.message
    text = message.text

    if not text:
        return

    # Try to parse intent using LLM or heuristics
    intent = await llm_service.parse_intent(text)

    if not intent.urls:
        # No URLs found
        if intent.is_download_request:
            await message.reply_text(
                "🔗 هیچ لینکی در پیام شما پیدا نکردم.\n"
                "لطفاً یک لینک معتبر برای دانلود وارد کنید."
            )
        else:
            await message.reply_text(
                "👋 یک لینک برای دانلود بفرستید، یا از /help برای اطلاعات بیشتر استفاده کنید."
            )
        return

    # Use the first URL and store it in user_data
    url = intent.urls[0]
    context.user_data['pending_url'] = url

    # Check if it's a playlist
    status_msg = await message.reply_text("⏳ در حال بررسی لینک...")
    downloader = Downloader()
    media_info = await downloader.get_info(url)

    if media_info and media_info.is_playlist and media_info.playlist_count > 1:
        # It's a playlist - ask for confirmation
        context.user_data['pending_playlist_count'] = media_info.playlist_count
        await status_msg.edit_text(
            f"📋 *پلی‌لیست شناسایی شد!*\n\n"
            f"*عنوان:* {media_info.title}\n"
            f"*ویدیوها:* {media_info.playlist_count}\n\n"
            f"چه کاری می‌خواهید انجام دهید؟",
            reply_markup=playlist_confirmation_keyboard(media_info.playlist_count),
            parse_mode="Markdown"
        )
        return

    # If user explicitly requested audio or video, skip format selection
    if intent.wants_audio and not intent.wants_video:
        await show_audio_quality(status_msg)
    elif intent.wants_video and not intent.wants_audio:
        await show_video_quality(status_msg)
    else:
        # Show format selection
        await status_msg.edit_text(
            "🎯 *انتخاب فرمت:*",
            reply_markup=format_selection_keyboard(),
            parse_mode="Markdown"
        )


async def show_audio_quality(status_msg):
    """Show audio quality selection."""
    await status_msg.edit_text(
        "🎵 *انتخاب کیفیت صدا:*",
        reply_markup=audio_quality_keyboard(),
        parse_mode="Markdown"
    )


async def show_video_quality(status_msg):
    """Show video quality selection."""
    await status_msg.edit_text(
        "🎬 *انتخاب کیفیت ویدیو:*",
        reply_markup=video_quality_keyboard(),
        parse_mode="Markdown"
    )


@whitelist_only
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard callbacks."""
    query = update.callback_query
    await query.answer()

    prefix, action = parse_callback_data(query.data)

    # Handle callbacks that don't need pending_url first
    if prefix == CANCEL_PREFIX:
        context.user_data.pop('pending_url', None)
        context.user_data.pop('pending_formats', None)
        await query.edit_message_text("❌ دانلود لغو شد.")
        return

    if prefix == DELETE_PREFIX:
        token = action
        success = await file_server_client.delete_file(token)
        if success:
            await query.edit_message_text(
                query.message.text + "\n\n🗑️ فایل از سرور حذف شد."
            )
        else:
            await query.answer("حذف فایل ناموفق بود", show_alert=True)
        return

    if prefix == ACCESS_PREFIX:
        await handle_access_callback(query, context, action)
        return

    if prefix == ADMIN_PREFIX:
        await handle_admin_callback(query, context, action)
        return

    # Now check for pending_url (only needed for format/quality/confirm)
    url = context.user_data.get('pending_url')
    if not url:
        await query.edit_message_text("❌ نشست منقضی شد. لطفاً لینک را دوباره بفرستید.")
        return

    if prefix == FORMAT_PREFIX:
        if action == "audio":
            await _handle_format_selection(query, context, url, is_audio=True)
        elif action == "video":
            await _handle_format_selection(query, context, url, is_audio=False)
        elif action == "back":
            context.user_data.pop('pending_formats', None)
            await query.edit_message_text(
                "🎯 *انتخاب فرمت:*",
                reply_markup=format_selection_keyboard(),
                parse_mode="Markdown"
            )

    elif prefix == QUALITY_PREFIX:
        quality = _parse_quality_action(action)
        if quality:
            is_playlist = context.user_data.get('is_playlist_download', False)
            # Clear pending data after starting download
            context.user_data.pop('pending_url', None)
            context.user_data.pop('pending_formats', None)
            context.user_data.pop('is_playlist_download', None)
            context.user_data.pop('pending_playlist_count', None)

            if is_playlist:
                await start_playlist_download(query, url, quality, context)
            else:
                await start_download(query, url, quality, context)

    elif prefix == CONFIRM_PREFIX:
        if action == "playlist":
            # Store that this is a playlist download, then show format selection
            context.user_data['is_playlist_download'] = True
            await query.edit_message_text(
                "🎯 *انتخاب فرمت برای همه ویدیوها:*",
                reply_markup=format_selection_keyboard(),
                parse_mode="Markdown"
            )
        elif action == "single":
            # User wants only the first item
            context.user_data['is_playlist_download'] = False
            await query.edit_message_text(
                "🎯 *انتخاب فرمت:*",
                reply_markup=format_selection_keyboard(),
                parse_mode="Markdown"
            )


async def handle_access_callback(query, context: ContextTypes.DEFAULT_TYPE, action: str):
    """Handle access request callbacks."""
    config = get_config()
    user = query.from_user

    if action == "request":
        # Check if user already has a pending request
        existing_status = user_service.get_user_status(user.id)
        if existing_status == "pending":
            await query.edit_message_text(
                "⏳ شما از قبل یک درخواست دسترسی در انتظار دارید.\n"
                "وقتی ادمین درخواست شما را بررسی کند، مطلع می‌شوید."
            )
            return

        # Create access request
        created = user_service.create_access_request(
            telegram_id=user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
        )

        if created:
            await query.edit_message_text(
                "✅ درخواست دسترسی ارسال شد!\n\n"
                "وقتی ادمین درخواست شما را بررسی کند، مطلع می‌شوید."
            )

            # Notify admin
            if config.admin_user_id:
                user_info = f"*User:* {user.first_name or ''} {user.last_name or ''}".strip()
                if user.username:
                    user_info += f" (@{user.username})"
                user_info += f"\n*ID:* `{user.id}`"

                try:
                    await context.bot.send_message(
                        chat_id=config.admin_user_id,
                        text=(
                            "🔔 *درخواست دسترسی جدید*\n\n"
                            f"{user_info}\n\n"
                            "این درخواست را بررسی کنید:"
                        ),
                        reply_markup=admin_access_decision_keyboard(user.id),
                        parse_mode="Markdown",
                    )
                except Exception:
                    logger.exception(f"Failed to notify admin about access request from {user.id}")
        else:
            await query.edit_message_text(
                "⚠️ شما از قبل یک درخواست دسترسی ثبت کرده‌اید.\n"
                "لطفاً منتظر بررسی ادمین باشید."
            )


async def handle_admin_callback(query, context: ContextTypes.DEFAULT_TYPE, action: str):
    """Handle admin decision callbacks (approve/deny)."""
    config = get_config()
    admin_user = query.from_user

    # Verify that the user clicking is the admin
    if admin_user.id != config.admin_user_id:
        await query.answer("شما مجاز به انجام این عمل نیستید.", show_alert=True)
        return

    # Parse action: approve:{telegram_id} or deny:{telegram_id}
    parts = action.split(":", 1)
    if len(parts) != 2:
        await query.answer("عمل نامعتبر", show_alert=True)
        return

    decision, telegram_id_str = parts
    try:
        telegram_id = int(telegram_id_str)
    except ValueError:
        await query.answer("شناسه کاربر نامعتبر", show_alert=True)
        return

    if decision == "approve":
        success = user_service.approve_user(telegram_id, admin_user.id)
        if success:
            await query.edit_message_text(
                query.message.text + "\n\n✅ *تایید‌شده* توسط شما"
            , parse_mode="Markdown")

            # Notify the user
            try:
                await context.bot.send_message(
                    chat_id=telegram_id,
                    text=(
                        "🎉 *دسترسی تایید شد!*\n\n"
                        "درخواست شما تایید شده است. اکنون می‌توانید از ربات استفاده کنید.\n\n"
                        "یک لینک برای دانلود بفرستید، یا از /help برای اطلاعات بیشتر استفاده کنید."
                    ),
                    parse_mode="Markdown",
                )
            except Exception:
                logger.exception(f"Failed to notify user {telegram_id} about approval")
        else:
            await query.answer("تایید کاربر ناموفق بود", show_alert=True)

    elif decision == "deny":
        success = user_service.deny_user(telegram_id, admin_user.id)
        if success:
            await query.edit_message_text(
                query.message.text + "\n\n❌ *رد‌شده* توسط شما"
            , parse_mode="Markdown")

            # Notify the user
            try:
                await context.bot.send_message(
                    chat_id=telegram_id,
                    text=(
                        "⛔ *دسترسی رد شد*\n\n"
                        "درخواست دسترسی شما تایید نشد.\n"
                        "این ربات خصوصی است و فقط برای استفاده شخصی است."
                    ),
                    parse_mode="Markdown",
                )
            except Exception:
                logger.exception(f"Failed to notify user {telegram_id} about denial")
        else:
            await query.answer("رد کاربر ناموفق بود", show_alert=True)


async def _handle_format_selection(query, context, url: str, is_audio: bool):
    """Handle audio/video format selection with dynamic quality detection."""
    format_type = "audio" if is_audio else "video"
    emoji = "🎵" if is_audio else "🎬"

    # Show waiting message
    await query.edit_message_text("⏳ لطفاً صبر کنید، در حال تحلیل کیفیت‌های موجود...")

    # Get available formats
    downloader = Downloader()
    formats_result = await downloader.get_available_formats(url)

    # Cache result for potential reuse
    context.user_data['pending_formats'] = formats_result

    # Check for errors or empty results
    if formats_result.error:
        logger.warning(f"Format detection failed for {url}: {formats_result.error}")
        await _show_fallback_keyboard(query, is_audio, emoji, formats_result.error)
        return

    if is_audio:
        if not formats_result.audio_formats:
            await _show_fallback_keyboard(query, is_audio, emoji, "هیچ فرمت صوتی‌ای شناسایی نشد")
            return
        keyboard = dynamic_audio_quality_keyboard(formats_result.audio_formats)
    else:
        if not formats_result.video_formats:
            await _show_fallback_keyboard(query, is_audio, emoji, "هیچ فرمت ویدیویی شناسایی نشد")
            return
        keyboard = dynamic_video_quality_keyboard(formats_result.video_formats)

    await query.edit_message_text(
        f"{emoji} *Choose {format_type} quality:*",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )


async def _show_fallback_keyboard(query, is_audio: bool, emoji: str, error_reason: str):
    """Show fallback keyboard with default options when dynamic detection fails."""
    format_type = "audio" if is_audio else "video"
    keyboard = audio_quality_keyboard() if is_audio else video_quality_keyboard()

    await query.edit_message_text(
        f"{emoji} *Choose {format_type} quality:*\n"
        f"_استفاده از تنظیمات پیش‌فرض ({error_reason})_",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )


def _parse_quality_action(action: str):
    """Parse quality action string into DownloadQuality or DynamicQuality."""
    # First check if it's a standard quality
    standard_qualities = {
        "audio_128": DownloadQuality.AUDIO_128,
        "audio_192": DownloadQuality.AUDIO_192,
        "audio_320": DownloadQuality.AUDIO_320,
        "audio_best": DownloadQuality.AUDIO_BEST,
        "video_480": DownloadQuality.VIDEO_480,
        "video_720": DownloadQuality.VIDEO_720,
        "video_1080": DownloadQuality.VIDEO_1080,
        "video_best": DownloadQuality.VIDEO_BEST,
    }

    if action in standard_qualities:
        return standard_qualities[action]

    # Try to parse as dynamic quality (e.g., video_1440, audio_256)
    if action.startswith("video_"):
        try:
            height = int(action.split("_")[1])
            return DynamicQuality(is_audio=False, value=height)
        except (ValueError, IndexError):
            pass
    elif action.startswith("audio_"):
        try:
            bitrate = int(action.split("_")[1])
            return DynamicQuality(is_audio=True, value=bitrate)
        except (ValueError, IndexError):
            pass

    return None


async def start_download(query, url: str, quality, context: ContextTypes.DEFAULT_TYPE):
    """Start a download task."""
    chat_id = query.message.chat_id
    message_id = query.message.message_id
    user_id = query.from_user.id

    # Determine format type and quality string
    is_audio = isinstance(quality, DynamicQuality) and quality.is_audio
    if not isinstance(quality, DynamicQuality):
        is_audio = quality.value.startswith("audio")
    format_type = "audio" if is_audio else "video"
    quality_str = str(quality.value) if hasattr(quality, "value") else str(quality)

    # Update message to show queuing
    await query.edit_message_text("⏳ در حال افزودن به صف...")

    # Create progress callback
    async def progress_callback(percent: float, status: str):
        try:
            if status == "downloading":
                progress_bar = create_progress_bar(percent)
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=f"⬇️ در حال دانلود...\n{progress_bar} {percent:.0f}%"
                )
            elif status == "processing":
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text="🔄 در حال پردازش..."
                )
            elif status.startswith("complete|"):
                # Split from right first to get filesize (last field, no pipes)
                # Then split remaining from left to handle titles containing |
                main_part, filesize_str = status.rsplit("|", maxsplit=1)
                parts = main_part.split("|", maxsplit=2)
                filepath = Path(parts[1])
                title = parts[2]
                filesize_mb = float(filesize_str)

                # Record download in stats
                platform = detect_platform(url)
                stats_service.record_download(
                    url=url,
                    platform=platform,
                    format_type=format_type,
                    quality=quality_str,
                    filesize_mb=filesize_mb,
                    title=title,
                    user_id=user_id,
                )

                await handle_download_complete(
                    context.bot, chat_id, message_id, filepath, title, filesize_mb
                )
            elif status.startswith("error|"):
                error_msg = status.split("|", 1)[1]
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=f"❌ دانلود ناموفق:\n{error_msg}"
                )
        except Exception:
            logger.exception("Error in progress callback")

    # Create and queue task
    task = DownloadTask(
        url=url[:50],
        quality=quality,
        chat_id=chat_id,
        message_id=message_id,
        progress_callback=progress_callback,
        original_url=url,
    )

    position = await download_queue.add(task)

    if position > 0:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=f"📋 به صف اضافه شد (جایگاه #{position})"
        )


async def start_playlist_download(query, url: str, quality, context: ContextTypes.DEFAULT_TYPE):
    """Start downloading all items in a playlist."""
    chat_id = query.message.chat_id
    message_id = query.message.message_id
    user_id = query.from_user.id

    # Determine format type and quality string
    is_audio = isinstance(quality, DynamicQuality) and quality.is_audio
    if not isinstance(quality, DynamicQuality):
        is_audio = quality.value.startswith("audio")
    format_type = "audio" if is_audio else "video"
    quality_str = str(quality.value) if hasattr(quality, "value") else str(quality)

    # Show extraction message
    await query.edit_message_text("⏳ در حال استخراج آیتم‌های پلی‌لیست...")

    # Get playlist info
    downloader = Downloader()
    playlist_info = await downloader.get_playlist_info(url)

    if not playlist_info or not playlist_info.entries:
        await query.edit_message_text("❌ استخراج آیتم‌های پلی‌لیست ممکن نبود.")
        return

    total = playlist_info.count
    await query.edit_message_text(
        f"📋 *دانلود پلی‌لیست: {playlist_info.title}*\n\n"
        f"آیتم‌ها: ۰/{total} تکمیل‌شده\n"
        f"وضعیت: در حال شروع...",
        parse_mode="Markdown"
    )

    # Download each entry sequentially
    completed = 0
    failed = 0
    results = []

    for entry in playlist_info.entries:
        # Update progress
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=(
                f"📋 *دانلود پلی‌لیست: {playlist_info.title}*\n\n"
                f"آیتم‌ها: {completed}/{total} تکمیل‌شده ({failed} ناموفق)\n"
                f"فعلی: {entry.title[:50]}..."
            ),
            parse_mode="Markdown"
        )

        # Download this entry
        result = await downloader.download(url=entry.url, quality=quality)

        if result.success:
            completed += 1
            results.append((entry.title, result.filepath, result.filesize_mb))

            # Record download in stats
            platform = detect_platform(entry.url)
            stats_service.record_download(
                url=entry.url,
                platform=platform,
                format_type=format_type,
                quality=quality_str,
                filesize_mb=result.filesize_mb,
                title=entry.title,
                user_id=user_id,
            )
        else:
            failed += 1
            logger.warning(f"Failed to download playlist item {entry.index}: {result.error_message}")

    # Final summary
    summary_text = (
        f"📋 *پلی‌لیست تکمیل شد: {playlist_info.title}*\n\n"
        f"✅ دانلود‌شده: {completed}/{total}\n"
    )
    if failed > 0:
        summary_text += f"❌ ناموفق: {failed}\n"

    # List first few successful downloads
    if results:
        summary_text += "\n*فایل‌های دانلود‌شده:*\n"
        for i, (title, filepath, size_mb) in enumerate(results[:5]):
            summary_text += f"• {title[:40]}... ({size_mb:.1f} MB)\n"
        if len(results) > 5:
            summary_text += f"_... و {len(results) - 5} مورد دیگر_\n"

    await context.bot.edit_message_text(
        chat_id=chat_id,
        message_id=message_id,
        text=summary_text,
        parse_mode="Markdown"
    )


async def handle_download_complete(bot, chat_id: int, message_id: int, filepath: Path, title: str, filesize_mb: float):
    """Handle completed download - send file or notify about large file."""
    config = get_config()

    if is_file_within_limit(filepath):
        # Send file to user
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text="📤 در حال آپلود..."
        )

        try:
            with open(filepath, "rb") as f:  # noqa: ASYNC230 — only opens the handle; PTB streams it during upload
                if filepath.suffix.lower() == ".mp3":
                    await bot.send_audio(
                        chat_id=chat_id,
                        audio=InputFile(f, filename=filepath.name),
                        title=title,
                        caption=f"🎵 {title}"
                    )
                else:
                    await bot.send_video(
                        chat_id=chat_id,
                        video=InputFile(f, filename=filepath.name),
                        caption=f"🎬 {title}",
                        supports_streaming=True
                    )

            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=f"✅ دانلود شد: {title}\n📁 حجم: {filesize_mb:.1f} MB"
            )

        except OSError:
            logger.exception(f"Failed to send file: {filepath}")
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=f"✅ دانلود شد ولی ارسال نشد:\n{title}\n📁 حجم: {filesize_mb:.1f} MB\n📍 ذخیره در: {filepath}"
            )
    else:
        # File too large for Telegram - generate download link
        download_link = await file_server_client.generate_download_link(str(filepath))

        if download_link:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=(
                    f"✅ Downloaded: {title}\n"
                    f"📁 Size: {filesize_mb:.1f} MB\n\n"
                    f"⚠️ فایل از محدودیت {config.max_file_size_mb}MB تلگرام بیشتر است.\n\n"
                    f"📥 دانلود: {download_link.url}"
                ),
                reply_markup=file_delete_keyboard(download_link.token),
            )
        else:
            # Fallback if file server is unavailable
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=(
                    f"✅ Downloaded: {title}\n"
                    f"📁 Size: {filesize_mb:.1f} MB\n\n"
                    f"⚠️ فایل از محدودیت {config.max_file_size_mb}MB بیشتر است.\n"
                    f"📍 ذخیره در: {filepath}"
                )
            )


def create_progress_bar(percent: float, length: int = 10) -> str:
    """Create a text progress bar."""
    filled = int(length * percent / 100)
    empty = length - filled
    return "▓" * filled + "░" * empty


def register_handlers(app: Application):
    """Register all handlers with the application."""
    # Commands
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("health", health_command))
    app.add_handler(CommandHandler("about", about_command))

    # Callback queries (inline keyboards)
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Text messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Handlers registered")
