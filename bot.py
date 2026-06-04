"""
bot.py — MetaPlay by Coinplay
Commands: /start /live /today /picks /stats /signal /record /apitest
"""
import asyncio, logging, os, sys, atexit, time
from telegram import Update
from telegram.error import Conflict, NetworkError
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from telegram.constants import ParseMode
from config import BOT_TOKEN, BOT_USERNAME, HOOK_IMAGE, State, TG_LANG_MAP, DEFAULT_LANG
from storage import get_user, update_user, append_history
from conversation import handle_message, handle_menu_action, main_menu
from signals import run_signal_scheduler
from ftd_onboarding import resume_pending_repeats
from messages import HOOK_CAPTION
from media import send_pic, pics_available

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

LOCK_FILE = "/tmp/metaplay_bot.lock"

def _check_lock():
    if os.path.exists(LOCK_FILE):
        try:
            pid = int(open(LOCK_FILE).read().strip())
            os.kill(pid, 0)
            logger.critical(f"Already running (PID {pid}). Exiting.")
            sys.exit(1)
        except (ProcessLookupError, ValueError):
            os.remove(LOCK_FILE)
    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))
    atexit.register(lambda: os.path.exists(LOCK_FILE) and os.remove(LOCK_FILE))

_check_lock()

def _detect_lang(code):
    if not code:
        return DEFAULT_LANG
    return TG_LANG_MAP.get(code.split("-")[0].lower(), DEFAULT_LANG)

def _admin_ids():
    raw = os.environ.get("ADMIN_IDS", "")
    return [int(x.strip()) for x in raw.split(",") if x.strip()]


# ── /start ────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user    = update.effective_user
    chat_id = update.effective_chat.id
    lang    = _detect_lang(user.language_code)
    u_check = get_user(user.id, lang)
    is_new = u_check.get("message_count", 0) == 0
    if is_new:
        update_user(user.id, lang=lang, state=State.NEW,
                    onboarding_done=False, onboarding_turn=0, stage_replies=0)
    else:
        update_user(user.id, lang=lang)
    caption = HOOK_CAPTION.get(lang, HOOK_CAPTION["en"])
    menu    = main_menu(lang)
    # Try branded image first (pics/19.png), then hook.png, then text
    sent = await send_pic(context.bot, chat_id, "start", caption, lang, reply_markup=menu)
    if not sent and os.path.exists(HOOK_IMAGE):
        try:
            with open(HOOK_IMAGE, "rb") as p:
                await context.bot.send_photo(
                    chat_id=chat_id, photo=p,
                    caption=caption, parse_mode=ParseMode.HTML,
                    reply_markup=menu,
                )
            sent = True
        except Exception as e:
            logger.warning(f"Hook image fallback: {e}")
    append_history(user.id, "assistant", caption)
    logger.info(f"/start user={user.id} lang={lang}")


# ── Menu command shortcuts ────────────────────────────────────────────────────

async def _menu(action, update, context):
    user  = update.effective_user
    u     = get_user(user.id)
    lang  = u.get("lang", _detect_lang(user.language_code))
    try:
        await handle_menu_action(context.bot, user.id, update.effective_chat.id, lang, action)
    except Exception as e:
        logger.error(f"_menu crashed action={action} user={user.id}: {e}", exc_info=True)
        err = "⚠️ Algo salió mal — intentá de nuevo." if lang == "es" else "⚠️ Something went wrong — try again in a moment."
        try:
            await update.message.reply_text(err)
        except Exception:
            pass

async def cmd_live(u, c):  await _menu("live",   u, c)
async def cmd_today(u, c): await _menu("today",  u, c)
async def cmd_picks(u, c): await _menu("picks",  u, c)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    u    = get_user(user.id)
    lang = u.get("lang", _detect_lang(user.language_code))
    if user.id in _admin_ids():
        from storage import get_all_users
        users  = get_all_users()
        counts = {}
        for usr in users:
            s = usr.get("state", "unknown")
            counts[s] = counts.get(s, 0) + 1
        lines = ["MetaPlay Funnel Stats\n"]
        for st, cnt in sorted(counts.items()):
            lines.append(f"{st}: {cnt}")
        lines.append(f"\nTotal: {len(users)}")
        await update.message.reply_text("\n".join(lines))
        return
    await handle_menu_action(context.bot, user.id, update.effective_chat.id, lang, "stats")


# ── Admin: /apitest ───────────────────────────────────────────────────────────

async def cmd_apitest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Test all external API connections and report status."""
    if update.effective_user.id not in _admin_ids():
        return

    await update.message.reply_text("Testing APIs... check Railway logs for details.")

    from livescore import (
        get_live_football, get_today_football,
        get_live_esports, get_upcoming_esports,
    )

    rapidapi_key = os.environ.get("RAPIDAPI_KEY", "")
    pandascore_key = os.environ.get("PANDASCORE_KEY", "")

    lines = [
        "API Test Results",
        "",
        "Env vars:",
        "RAPIDAPI_KEY: " + ("SET (" + rapidapi_key[:8] + "...)" if rapidapi_key else "MISSING"),
        "PANDASCORE_KEY: " + ("SET (" + pandascore_key[:8] + "...)" if pandascore_key else "MISSING"),
        "",
        "Connectivity:",
    ]

    tests = [
        ("Football live",    get_live_football()),
        ("Football today",   get_today_football()),
        ("Esports live",     get_live_esports()),
        ("Esports upcoming", get_upcoming_esports(24)),
    ]

    for name, coro in tests:
        try:
            matches, is_mock = await coro
            status = "REAL DATA" if not is_mock else "MOCK (API failed or key missing)"
            lines.append(f"{name}: {status} — {len(matches)} matches")
        except Exception as e:
            lines.append(f"{name}: ERROR — {e}")

    # Check images
    from media import pics_available
    lines.append("")
    lines.append("Images (pics/ folder):")
    for moment, ok in pics_available().items():
        lines.append(f"  {moment}: {'OK' if ok else 'MISSING'}")
    from media import pics_available
    from livescore import _esportapi_get
    lines.append("")
    lines.append("ESportApi raw test:")
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for ep in ["/api/esport/matches/live", f"/api/esport/matches/scheduled/date/{today}"]:
        try:
            raw = await _esportapi_get(ep)
            if raw is None:
                lines.append(f"  {ep}: None (failed/blocked)")
            elif isinstance(raw, dict):
                events = raw.get("events") or []
                lines.append(f"  {ep}: OK — {len(events)} events")
                if events:
                    e0 = events[0]
                    t = (e0.get("tournament") or {}).get("name", "?")
                    h = (e0.get("homeTeam") or {}).get("name", "?")
                    a = (e0.get("awayTeam") or {}).get("name", "?")
                    lines.append(f"    Sample: {h} vs {a} | {t}")
            else:
                lines.append(f"  {ep}: unexpected {type(raw)}")
        except Exception as e:
            lines.append(f"  {ep}: ERROR {e}")

    await update.message.reply_text("\n".join(lines))


# ── Admin: /signal ────────────────────────────────────────────────────────────

async def cmd_signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in _admin_ids():
        return
    from signals import broadcast_daily_signal
    await broadcast_daily_signal(context.bot)
    await update.message.reply_text("Broadcast sent.")


# ── Admin: /record ────────────────────────────────────────────────────────────

async def cmd_policy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u    = get_user(update.effective_user.id)
    lang = u.get("lang", _detect_lang(update.effective_user.language_code))
    if lang == "es":
        text = (
            "🔒 *Política de Privacidad*\n\n"
            "MetaPlay recopila únicamente tu ID de Telegram para personalizar el análisis.\n"
            "No almacenamos datos de pago ni información personal sensible.\n\n"
            "[Leer política completa →](https://metaarena.s26636274.workers.dev/privacy)"
        )
    else:
        text = (
            "🔒 *Privacy Policy*\n\n"
            "MetaPlay collects only your Telegram ID to personalise analysis.\n"
            "We don't store payment data or sensitive personal information.\n\n"
            "[Read full policy →](https://metaarena.s26636274.workers.dev/privacy)"
        )
    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


async def cmd_record(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in _admin_ids():
        return
    args = context.args
    if not args or len(args) < 2:
        await update.message.reply_text("Usage: /record <keyword> <correct|wrong>")
        return
    from predictions import record_result
    record_result(args[0], args[1].lower() == "correct")
    await update.message.reply_text(f"Recorded: {args[0]} -> {args[1]}")


# ── Text messages ─────────────────────────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (update.message.text or "").strip()
    if not text:
        return
    u    = get_user(user.id)
    lang = u.get("lang", _detect_lang(user.language_code))
    try:
        await handle_message(context.bot, user.id, update.effective_chat.id, text, lang)
    except Exception as e:
        logger.error(f"handle_text crashed user={user.id} text={text[:40]!r}: {e}", exc_info=True)
        err = "⚠️ Algo salió mal — intentá de nuevo." if lang == "es" else "⚠️ Something went wrong — try again in a moment."
        try:
            await update.message.reply_text(err)
        except Exception:
            pass


# ── Post-init ─────────────────────────────────────────────────────────────────

async def post_init(application: Application):
    asyncio.create_task(run_signal_scheduler(application.bot))
    await resume_pending_repeats(application.bot)
    # Register commands visible in Telegram's "/" menu
    from telegram import BotCommand
    await application.bot.set_my_commands([
        BotCommand("start",  "Start / reset"),
        BotCommand("live",   "Live scores"),
        BotCommand("today",  "Today's matches"),
        BotCommand("picks",  "Today's picks"),
        BotCommand("stats",  "Track record"),
        BotCommand("policy", "Privacy policy"),
    ])
    logger.info("MetaPlay Bot started")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("live",    cmd_live))
    app.add_handler(CommandHandler("today",   cmd_today))
    app.add_handler(CommandHandler("picks",   cmd_picks))
    app.add_handler(CommandHandler("stats",   cmd_stats))
    app.add_handler(CommandHandler("signal",  cmd_signal))
    app.add_handler(CommandHandler("policy",  cmd_policy))
    app.add_handler(CommandHandler("record",  cmd_record))
    app.add_handler(CommandHandler("apitest", cmd_apitest))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info(f"Starting {BOT_USERNAME}...")

    async def _error_handler(update, context):
        from telegram.error import Conflict, NetworkError
        if isinstance(context.error, (Conflict, NetworkError)):
            logger.warning(f"Recoverable error: {context.error}")
            return
        logger.exception(context.error)

    app.add_error_handler(_error_handler)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
