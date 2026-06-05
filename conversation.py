"""
conversation.py — MetaPlay by Coinplay

THREE INDEPENDENT LAYERS — never interfere:

LAYER 1 — MENU (always wins first)
  Any menu button → direct feature handler, skip everything else

LAYER 2 — ONBOARDING (free text, first 3 exchanges)
  Step 0: sport preference
  Step 1: leagues / games
  Step 2: teams + style
  → saves preferences → shows bridge → done forever

LAYER 3 — ASSISTANT (free text, post-onboarding)
  Mateo answers questions, personalized by preferences
  No hard funnel — menu handles feature access
  FTD detection always active
"""
import asyncio, logging, time, random
from telegram import Bot, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup
from telegram.constants import ParseMode
from config import State, COINPLAY_REG_URL
from storage import get_user, update_user, get_preferences, append_history, log_barrier
from ai_agent import get_ai_response, detect_ftd
from onboarding import (
    process_onboarding_answer, get_first_question,
    DONE_MSG, format_preferences_summary,
)
from messages import (
    BRIDGE, CTA_REGISTER, FTD_CELEBRATION,
    BARRIER_FALLBACK, GENERIC_FALLBACK,
    JOIN_PROMPT, JOIN_CHECK_BTN, JOIN_OK, JOIN_NOT_YET,
)
from ftd_onboarding import start_repeat_machine
from livescore import (
    fetch_match_context,
    format_livescore_message, format_upcoming_message,
)
from predictions import generate_daily_predictions, format_predictions_message, get_stats_display
from media import send_pic, get_repeat_moment, sanitize_markdown
import analytics
import membership

logger = logging.getLogger(__name__)


# ── Persistent menu ───────────────────────────────────────────────────────────

from brand import BRAND, CTAMode

_NAME = BRAND.character.name  # "Mateo", "Diego", …
# Текст пятой кнопки зависит от режима воронки.
_PROFIT_EN = "📣 The channel" if BRAND.cta.mode is CTAMode.CHANNEL else f"💰 How {_NAME} Profits"
_PROFIT_ES = "📣 El canal"     if BRAND.cta.mode is CTAMode.CHANNEL else f"💰 Cómo Gana {_NAME}"

# Метки кнопок (строятся из одного источника → всегда совпадают с MENU_ACTIONS)
_BTN = {
    "live_en":  "🔴 Live Scores",       "live_es":  "🔴 En Vivo",
    "today_en": "📅 Today's Matches",   "today_es": "📅 Hoy",
    "picks_en": f"🎯 {_NAME}'s Picks",  "picks_es": f"🎯 Picks de {_NAME}",
    "stats_en": "📊 Track Record",      "stats_es": "📊 Historial",
    "profit_en": _PROFIT_EN,            "profit_es": _PROFIT_ES,
}


def main_menu(lang: str) -> ReplyKeyboardMarkup:
    if lang == "es":
        buttons = [
            [_BTN["live_es"],  _BTN["today_es"]],
            [_BTN["picks_es"], _BTN["stats_es"]],
            [_BTN["profit_es"]],
        ]
    else:
        buttons = [
            [_BTN["live_en"],  _BTN["today_en"]],
            [_BTN["picks_en"], _BTN["stats_en"]],
            [_BTN["profit_en"]],
        ]
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)


MENU_ACTIONS = {
    _BTN["live_en"]:   "live",  _BTN["live_es"]:   "live",
    _BTN["today_en"]:  "today", _BTN["today_es"]:  "today",
    _BTN["picks_en"]:  "picks", _BTN["picks_es"]:  "picks",
    _BTN["stats_en"]:  "stats", _BTN["stats_es"]:  "stats",
    _BTN["profit_en"]: "bridge", _BTN["profit_es"]: "bridge",
}


# ── Send helpers ──────────────────────────────────────────────────────────────

# callback_data кнопки верификации подписки (рычаг №2)
JOIN_CHECK_CB = "cb_join_check"


def _cta_keyboard(lang: str) -> InlineKeyboardMarkup:
    """
    Единая inline-клавиатура CTA под текущий режим воронки.

    PRODUCT → одна кнопка на регистрацию.
    CHANNEL → кнопка вступления в канал + (если gate) кнопка «✅ Я подписался»,
              которая проверяет членство через getChatMember и мгновенно
              разблокирует контент. Конверсия внутри бота выше, чем через
              выход в вебвью.
    """
    if BRAND.cta.mode is CTAMode.CHANNEL:
        rows = [[InlineKeyboardButton(BRAND.cta.label(lang), url=COINPLAY_REG_URL)]]
        if BRAND.cta.gate and membership.channel_configured():
            rows.append([InlineKeyboardButton(
                JOIN_CHECK_BTN.get(lang, JOIN_CHECK_BTN["en"]),
                callback_data=JOIN_CHECK_CB,
            )])
        return InlineKeyboardMarkup(rows)
    return InlineKeyboardMarkup([[InlineKeyboardButton(BRAND.cta.label(lang), url=COINPLAY_REG_URL)]])


async def send_channel_join(bot: Bot, chat_id: int, lang: str):
    """Нативное сообщение-приглашение в канал с верификацией (рычаг №2).

    Вызывается из бота по deep-link `/start join` или команде /join — например,
    когда мини-апп проксирует тап «подписаться» в бота, чтобы остаться внутри
    одного потока Telegram без контекст-свитча наружу.
    """
    analytics.track("cta_view")
    text = JOIN_PROMPT.get(lang, JOIN_PROMPT["en"])
    sent = await send_pic(bot, chat_id, "cta", text, lang, reply_markup=_cta_keyboard(lang))
    if not sent:
        await _send(bot, chat_id, text, lang, inline=_cta_keyboard(lang))


async def handle_join_check(bot: Bot, user_id: int, chat_id: int, lang: str) -> bool:
    """
    Колбэк «✅ Я подписался»: проверяем членство (getChatMember) без кэша и либо
    подтверждаем доступ, либо мягко возвращаем к кнопке вступления.
    Возвращает фактический статус членства.
    """
    analytics.track("cta_tap", user_id)
    membership.invalidate(user_id)
    member = await membership.is_member(user_id, use_cache=False)
    if member:
        if analytics.mark_join(user_id):
            logger.info(f"Channel join confirmed: user={user_id}")
        update_user(user_id, state=State.DEPOSITED)  # в channel-режиме = «подписан»
        await _send(bot, chat_id, JOIN_OK.get(lang, JOIN_OK["en"]), lang)
    else:
        await _send(bot, chat_id, JOIN_NOT_YET.get(lang, JOIN_NOT_YET["en"]), lang,
                    inline=_cta_keyboard(lang))
    return member


def _delay(text: str) -> float:
    return round(1.0 + min(len(text) / 160, 2.0), 1)


async def _send(bot: Bot, chat_id: int, text: str, lang: str, inline=None):
    await bot.send_chat_action(chat_id, "typing")
    await asyncio.sleep(_delay(text))
    await bot.send_message(
        chat_id=chat_id,
        text=sanitize_markdown(text),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=inline or main_menu(lang),
        disable_web_page_preview=True,
    )


async def _send_with_reg(bot: Bot, chat_id: int, text: str, lang: str):
    inline = _cta_keyboard(lang)
    await bot.send_chat_action(chat_id, "typing")
    await asyncio.sleep(_delay(text))
    await bot.send_message(
        chat_id=chat_id,
        text=sanitize_markdown(text),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=inline,
        disable_web_page_preview=True,
    )


# ── Main entry point ──────────────────────────────────────────────────────────

async def handle_message(bot: Bot, user_id: int, chat_id: int, text: str, lang: str):

    # ── LAYER 1: Menu intercept ───────────────────────────────────────────────
    action = MENU_ACTIONS.get(text)
    if action:
        await handle_menu_action(bot, user_id, chat_id, lang, action)
        return

    # ── Load state ────────────────────────────────────────────────────────────
    u               = get_user(user_id, lang)
    state           = u.get("state", State.NEW)
    history         = u.get("history", [])
    barriers        = u.get("barriers", [])
    onboarding_done = u.get("onboarding_done", False)
    onboarding_step = u.get("onboarding_turn", 0)
    prefs           = u.get("preferences", {})

    update_user(user_id, message_count=u.get("message_count", 0) + 1)
    append_history(user_id, "user", text)

    # ── FTD detection (always active) ─────────────────────────────────────────
    if state in (State.CONVERTING, State.BRIDGE, State.WARMUP, State.DEPOSITED):
        if await detect_ftd(text, lang):
            await _handle_ftd(bot, user_id, chat_id, lang)
            return

    # ── LAYER 2: Onboarding ───────────────────────────────────────────────────
    if not onboarding_done:
        await _run_onboarding(bot, user_id, chat_id, text, lang, onboarding_step)
        return

    # ── LAYER 3: Assistant ────────────────────────────────────────────────────
    ctx = await fetch_match_context()
    ai  = await get_ai_response(
        user_message  = text,
        lang          = lang,
        state         = state,
        history       = history,
        barriers      = barriers,
        real_live     = ctx["live"],
        real_upcoming = ctx["upcoming"],
        prefs         = prefs,
    )

    if ai["barrier"]:
        log_barrier(user_id, ai["barrier"])
        # Send barrier-specific response to overcome objection
        bf = BARRIER_FALLBACK.get(ai["barrier"], {})
        barrier_text = bf.get(lang) or bf.get("en")
        if barrier_text:
            append_history(user_id, "assistant", barrier_text)
            await _send(bot, chat_id, barrier_text, lang)
            return

    # Use AI's recommended next state
    if ai["next"] == "converting" and state not in (State.CONVERTING, State.DEPOSITED, State.REPEAT):
        update_user(user_id, state=State.CONVERTING)
        await _send_cta(bot, user_id, chat_id, lang)
        return
    elif ai["next"] == "bridge" and state == State.WARMUP:
        await _send_bridge(bot, user_id, chat_id, lang)
        return

    # Still catch deposit intent post-onboarding
    if ai["intent"] == "deposit_ready" and state != State.DEPOSITED:
        update_user(user_id, state=State.CONVERTING)
        await _send_cta(bot, user_id, chat_id, lang)
        return

    reply = ai["text"] or GENERIC_FALLBACK.get(lang, GENERIC_FALLBACK["en"])
    append_history(user_id, "assistant", reply)
    await _send(bot, chat_id, reply, lang)


# ── Layer 2: Onboarding steps ─────────────────────────────────────────────────

async def _run_onboarding(bot, user_id, chat_id, text, lang, step):
    # Process this answer, extract preferences, get next question
    next_question, complete = await process_onboarding_answer(user_id, text, step, lang)
    update_user(user_id, onboarding_turn=step + 1)

    if not complete:
        append_history(user_id, "assistant", next_question)
        # Step 1 gets image 110 (demon at keyboard), step 2 gets 111 (demon flying)
        pic_moment = "onboarding1" if step == 0 else "onboarding2"
        sent = await send_pic(bot, chat_id, pic_moment, next_question, lang)
        if not sent:
            await _send(bot, chat_id, next_question, lang)
    else:
        # Onboarding done — show summary + bridge
        update_user(user_id, onboarding_done=True, state=State.BRIDGE)
        prefs = get_preferences(user_id)

        # Summary of what we learned
        summary = format_preferences_summary(prefs, lang)
        done_text = DONE_MSG.get(lang, DONE_MSG["en"])

        if summary:
            await _send(bot, chat_id, summary, lang)
            await asyncio.sleep(1.0)

        # Bridge to Coinplay
        await _send_bridge(bot, user_id, chat_id, lang, done_text)


# ── Menu action handler ───────────────────────────────────────────────────────

async def handle_menu_action(bot: Bot, user_id: int, chat_id: int, lang: str, action: str):
    u     = get_user(user_id)
    state = u.get("state", State.NEW)
    prefs = u.get("preferences", {})

    # bridge / stats don't need match data — handle first to avoid unnecessary API calls
    if action == "bridge":
        update_user(user_id, onboarding_done=True)
        if u.get("bridge_shown") and state not in (State.DEPOSITED, State.REPEAT):
            update_user(user_id, state=State.CONVERTING)
            await _send_cta(bot, user_id, chat_id, lang)
        else:
            update_user(user_id, state=State.BRIDGE)
            await _send_bridge(bot, user_id, chat_id, lang)
        return

    if action == "stats":
        text = get_stats_display(lang)
        if state not in (State.DEPOSITED, State.REPEAT):
            await _send_with_reg(bot, chat_id, text, lang)
        else:
            await _send(bot, chat_id, text, lang)
        return

    # live / today / picks need match context
    try:
        ctx = await fetch_match_context()
    except Exception as e:
        logger.error(f"fetch_match_context failed: {e}", exc_info=True)
        err = "📡 No se pudo cargar datos — intentá de nuevo." if lang == "es" else "📡 Couldn't load match data — try again in a moment."
        await _send(bot, chat_id, err, lang)
        return

    if action == "live":
        display = ctx["for_display"]
        is_mock = not ctx.get("has_real_live", ctx["has_real"])
        text    = format_livescore_message(display["live"], lang, is_mock=is_mock)
        if state not in (State.DEPOSITED, State.REPEAT) and not is_mock:
            if BRAND.cta.mode is CTAMode.CHANNEL:
<<<<<<< HEAD
                text += {
                    "ru": "\n\n💡 _Полные разборы этих матчей — в канале._",
                    "es": "\n\n💡 _Los análisis completos de estos partidos — en el canal._",
                }.get(lang, "\n\n💡 _Full breakdowns of these matches — in the channel._")
=======
                text += "\n\n💡 _Полные разборы этих матчей — в канале._"
>>>>>>> dd298ed52ba989b78fbedde62379a6de22c647bf
            else:
                text += (
                    "\n\n💡 _¿Querés actuar? Para eso uso Coinplay._"
                    if lang == "es" else
                    "\n\n💡 _Want to act on these? That's what Coinplay is for._"
                )
        await _send(bot, chat_id, text, lang)

    elif action == "today":
        display = ctx["for_display"]
        is_mock = not ctx.get("has_real_upcoming", ctx["has_real"])
        from messages import MORNING_DIGEST_HEADER, MORNING_DIGEST_FOOTER
        header = MORNING_DIGEST_HEADER.get(lang, MORNING_DIGEST_HEADER["en"])
        body   = format_upcoming_message(display["upcoming"], lang, is_mock=is_mock)
        footer = MORNING_DIGEST_FOOTER.get(lang, MORNING_DIGEST_FOOTER["en"])
        await _send(bot, chat_id, header + body + footer, lang)

    elif action == "picks":
        # Use real matches if available; personalize by preferences
        ai_matches = ctx["live"] + ctx["upcoming"] if ctx["has_real"] else []
        # Filter by user's sport preference if set
        sport = prefs.get("sport")
        if sport == "football":
            ai_matches = [m for m in ai_matches if "Football" in m.get("game", "")]
        elif sport == "esports":
            ai_matches = [m for m in ai_matches if "Football" not in m.get("game", "")]

        picks = await generate_daily_predictions(ai_matches, lang)
        text  = format_predictions_message(picks, lang)

        # Add personalization note if preferences set
        if prefs.get("leagues") or prefs.get("teams"):
            pref_note = _personalization_note(prefs, lang)
            if pref_note:
                text = pref_note + "\n\n" + text

        if state not in (State.DEPOSITED, State.REPEAT):
            inline = _cta_keyboard(lang)
            sent = await send_pic(bot, chat_id, "picks", text, lang, reply_markup=inline)
            if not sent:
                await _send_with_reg(bot, chat_id, text, lang)
        else:
            sent = await send_pic(bot, chat_id, "picks", text, lang)
            if not sent:
                await _send(bot, chat_id, text, lang)


def _personalization_note(prefs: dict, lang: str) -> str:
    items = []
    if prefs.get("leagues"):
        items.extend(prefs["leagues"][:2])
    if prefs.get("teams"):
        items.extend(prefs["teams"][:2])
    if not items:
        return ""
    focused = ", ".join(items)
    return (
        f"🎯 _Filtered for your profile: {focused}_"
        if lang == "en" else
        f"🎯 _Filtrado para tu perfil: {focused}_"
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _send_bridge(bot, user_id, chat_id, lang, intro_text: str = None):
    update_user(user_id, state=State.BRIDGE, bridge_shown=True)
    inline = _cta_keyboard(lang)
    if BRAND.cta.mode is CTAMode.CHANNEL:
        analytics.track("cta_view")

    if intro_text:
        # Show done message first (text only, no image)
        append_history(user_id, "assistant", intro_text)
        await _send(bot, chat_id, intro_text, lang)
        await asyncio.sleep(1.5)

    # Bridge with branded image
    bridge_text = BRIDGE.get(lang, BRIDGE["en"])
    append_history(user_id, "assistant", bridge_text)
    await send_pic(bot, chat_id, "bridge", bridge_text, lang, reply_markup=inline)


async def _send_cta(bot, user_id, chat_id, lang):
    update_user(user_id, state=State.CONVERTING, reg_link_sent=True)
    text   = CTA_REGISTER.get(lang, CTA_REGISTER["en"]).format(url=COINPLAY_REG_URL)
    inline = _cta_keyboard(lang)
    if BRAND.cta.mode is CTAMode.CHANNEL:
        analytics.track("cta_view")
    append_history(user_id, "assistant", text)
    await send_pic(bot, chat_id, "cta", text, lang, reply_markup=inline)


async def _handle_ftd(bot, user_id, chat_id, lang):
    update_user(user_id, state=State.DEPOSITED, ftd_at=time.time())
    text = FTD_CELEBRATION.get(lang, FTD_CELEBRATION["en"])
    append_history(user_id, "assistant", text)
    await send_pic(bot, chat_id, "ftd", text, lang)
    await start_repeat_machine(bot, user_id, chat_id, lang)
