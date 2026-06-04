"""
brand.py — единый конфиг-слой бренда (backend)
================================================

Single source of truth для ВСЕГО, что отличается от бота к боту:
идентичность, персонаж/персона ИИ, вид спорта, оффер, ссылки, режим CTA
(вести в продукт ИЛИ в Telegram-канал), картинки воронки, язык/гео.

Архитектура «один движок — много брендов»:
    • Логика (bot.py, conversation.py, livescore.py, ai_agent.py …) НЕ меняется.
    • Меняется только активный объект BRAND.
    • Какой бренд активен — решает env-переменная BRAND_ID.
    • Секреты (токены, ключи API) сюда НЕ кладём — они остаются в окружении
      и читаются в config.py. brand.py хранит только НЕсекретную идентичность.

Поднять ещё одного бота = добавить запись в BRANDS + задеплоить инстанс
с BRAND_ID=<id> и своими BOT_TOKEN / ANTHROPIC_API_KEY / ключами API.

Совместимость: config.py превращён в тонкий шим, который реэкспортирует
поля BRAND под старыми именами (OFFER, COINPLAY_REG_URL, ESPORTS_GAMES …),
поэтому остальной код не требует правок.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from enum import Enum


# ──────────────────────────────────────────────────────────────────────────────
#  Перечисления
# ──────────────────────────────────────────────────────────────────────────────

class CTAMode(str, Enum):
    """Куда ведёт финальная кнопка воронки."""
    PRODUCT = "product"   # партнёрская ссылка (казино/букмекер) — как сейчас
    CHANNEL = "channel"   # подписка на Telegram-канал — новая воронка


class Vertical(str, Enum):
    """Основная спортивная вертикаль бренда (фильтрует данные и тексты)."""
    ESPORTS  = "esports"
    FOOTBALL = "football"
    BOTH     = "both"


# ──────────────────────────────────────────────────────────────────────────────
#  Вложенные конфиги
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Character:
    """Персонаж-аналитик, от лица которого говорит бот."""
    name: str                      # "Mateo"
    role: str                      # короткая роль для системного промпта
    persona: str                   # абзац характера (вставляется в system prompt)
    win_rate_display: float = 0.78  # отображаемая историческая точность (0..1)


@dataclass(frozen=True)
class Offer:
    """Числа оффера. Используются и в текстах бота, и в строках оффера фронта."""
    bonus_pct: int = 100
    bonus_max: int = 5000
    free_spins: int = 80
    min_deposit: int = 20
    wagering: int = 40
    cashback_pct: int = 5
    currencies: int = 40
    currency: str = "USDT"

    def summary(self, lang: str = "en") -> str:
        """Однострочное резюме оффера для системного промпта ИИ."""
        return (
            f"{self.bonus_pct}% bonus up to {self.bonus_max} {self.currency} "
            f"+ {self.free_spins} free spins, min {self.min_deposit} {self.currency}, "
            f"{self.cashback_pct}% cashback, {self.currencies}+ cryptos"
        )


@dataclass(frozen=True)
class CTA:
    """
    Финальный призыв к действию.

    mode == PRODUCT → ведём в партнёрский продукт (registration_url / click_url).
    mode == CHANNEL → ведём в Telegram-канал (channel_url), оффер скрываем,
                      машина дожима FTD отключается (см. Funnel.repeat_enabled).
    """
    mode: CTAMode = CTAMode.PRODUCT

    # — режим PRODUCT —
    click_url: str = ""            # трекинговая ссылка (для дожимов/текста)
    registration_url: str = ""     # ссылка для кнопки «Зарегистрироваться»
    partner_name: str = ""         # "Coinplay"
    license_label: str = "Curacao licensed"
    license_url: str = ""
    since: str = "2022"

    # — режим CHANNEL —
    channel_url: str = ""          # "https://t.me/your_channel"
    channel_handle: str = ""       # "@your_channel" (для подписей)

    # — подписи кнопки по языкам —
    button_label: dict[str, str] = field(default_factory=lambda: {
        "en": "🎯 Register",
        "es": "🎯 Registrarme",
    })

    def primary_url(self) -> str:
        """Куда реально ведёт основная кнопка при текущем режиме."""
        return self.channel_url if self.mode is CTAMode.CHANNEL else self.registration_url

    def label(self, lang: str) -> str:
        return self.button_label.get(lang, self.button_label.get("en", "Open"))


@dataclass(frozen=True)
class SportConfig:
    """Какие данные тянем у провайдера и как их показываем."""
    vertical: Vertical = Vertical.BOTH

    # Киберспорт: слаги PandaScore/ESportApi + человекочитаемые имена
    esports_games: tuple[str, ...] = ("cs2", "lol", "dota2", "valorant", "ow2", "r6")
    game_display: dict[str, str] = field(default_factory=lambda: {
        "cs2": "CS2", "csgo": "CS2", "lol": "League of Legends", "dota2": "Dota 2",
        "valorant": "Valorant", "ow2": "Overwatch 2", "r6siege": "Rainbow Six",
        "codmw": "Call of Duty", "rocketleague": "Rocket League",
    })

    # Футбол: лиги API-Football (название → league_id)
    football_leagues: dict[str, int] = field(default_factory=lambda: {
        "Champions League": 2, "Premier League": 39, "La Liga": 140,
        "Copa Libertadores": 13, "Liga 1 Peru": 268, "Liga BetPlay CO": 239,
        "Primera Division AR": 130, "V.League VN": 340, "ISL India": 323,
    })

    def wants_esports(self) -> bool:
        return self.vertical in (Vertical.ESPORTS, Vertical.BOTH)

    def wants_football(self) -> bool:
        return self.vertical in (Vertical.FOOTBALL, Vertical.BOTH)


@dataclass(frozen=True)
class Funnel:
    """Механика воронки (тюнингуется под бренд)."""
    max_daily_picks: int = 3
    onboarding_turns: int = 2
    repeat_enabled: bool = True        # машина повторных дожимов после FTD
    repeat_schedule: tuple[int, ...] = (3_600, 21_600, 86_400, 259_200, 604_800)


@dataclass(frozen=True)
class I18n:
    """Языки и сопоставление гео/языков Telegram."""
    supported: tuple[str, ...] = ("en", "es")
    default: str = "en"
    geo_lang: dict[str, str] = field(default_factory=lambda: {
        "VN": "en", "IN": "en", "PE": "es", "CO": "es", "AR": "es",
    })
    tg_lang_map: dict[str, str] = field(default_factory=lambda: {
        "en": "en", "vi": "en", "hi": "en", "es": "es",
    })


@dataclass(frozen=True)
class Brand:
    """
    Корневой объект бренда. Всё, что различается между ботами, живёт тут.
    Поля имён зеркалят brand.config.ts на фронте — это один конфиг на двух языках.
    """
    id: str
    display_name: str                   # "MetaPlay"
    bot_username: str                    # "MetaPlayBot"
    tagline: dict[str, str]             # подзаголовок/слоган по языкам
    character: Character
    sport: SportConfig
    offer: Offer
    cta: CTA
    funnel: Funnel = field(default_factory=Funnel)
    i18n: I18n = field(default_factory=I18n)
    privacy_url: str = ""

    # Картинки воронки: момент → файл в pics/ (переопределяет media.MOMENT_PICS)
    images: dict[str, str] = field(default_factory=lambda: {
        "start": "19.png", "onboarding1": "110.png", "onboarding2": "111.png",
        "bridge": "113.png", "cta": "114.png", "ftd": "112.png",
        "morning": "115.png", "picks": "114.png",
        "repeat_hot": "116.png", "repeat_win": "117.png",
    })

    # Имя модуля с пакетом текстов (см. «Пакеты копирайта» ниже).
    copy_pack: str = "messages"

    def with_env_overrides(self) -> "Brand":
        """
        Позволяет переопределить ссылки/режим из окружения на конкретном деплое
        без правки кода (удобно для A/B и быстрых смен оффера):
            CTA_MODE=channel CHANNEL_URL=https://t.me/foo
            COINPLAY_URL=... COINPLAY_REG_URL=...
        """
        cta = self.cta
        env_mode = os.environ.get("CTA_MODE")
        if env_mode in (CTAMode.PRODUCT.value, CTAMode.CHANNEL.value):
            cta = replace(cta, mode=CTAMode(env_mode))
        cta = replace(
            cta,
            click_url=os.environ.get("COINPLAY_URL", cta.click_url),
            registration_url=os.environ.get("COINPLAY_REG_URL", cta.registration_url),
            channel_url=os.environ.get("CHANNEL_URL", cta.channel_url),
        )
        return replace(
            self,
            cta=cta,
            bot_username=os.environ.get("BOT_USERNAME", self.bot_username),
            privacy_url=os.environ.get("PRIVACY_URL", self.privacy_url),
        )


# ──────────────────────────────────────────────────────────────────────────────
#  Реестр брендов
# ──────────────────────────────────────────────────────────────────────────────

_COINPLAY_LINK = "https://promotioncoinplay.com/L?tag=d_5617175m_59419c_&site=5617175&ad=59419"

#: Бренд №1 — текущий: киберспорт + футбол, ведём в продукт Coinplay.
METAPLAY = Brand(
    id="metaplay",
    display_name="MetaPlay",
    bot_username="MetaPlayBot",
    tagline={
        "en": "Read the game before it starts.",
        "es": "Leé el partido antes de que empiece.",
    },
    character=Character(
        name="Mateo",
        role="esports and football analyst",
        persona=(
            "You are {name} — {role} for the {brand} Telegram channel. "
            "Tone: insider, sharp, genuine — an analyst who shares his edge, "
            "not a promoter. You monetize your analysis through {partner}."
        ),
        win_rate_display=0.78,
    ),
    sport=SportConfig(vertical=Vertical.BOTH),
    offer=Offer(),
    cta=CTA(
        mode=CTAMode.PRODUCT,
        click_url=_COINPLAY_LINK,
        registration_url=_COINPLAY_LINK,
        partner_name="Coinplay",
        license_label="Curacao licensed",
        license_url="https://cert.cga.cw/token?id=626",
        since="2022",
        button_label={"en": "🎯 Register on Coinplay", "es": "🎯 Registrarme en Coinplay"},
    ),
    funnel=Funnel(),
    privacy_url="https://metaarena.s26636274.workers.dev/privacy",
    copy_pack="copy_metaplay",
)

#: Бренд №2 — пример ремикса: только футбол, финал ведёт в Telegram-КАНАЛ.
#  Демонстрирует ровно твою задачу: другой вид спорта, другой персонаж,
#  последняя страница = подписка на канал (продукт скрыт, дожимы FTD выключены).
GOALCAST = Brand(
    id="goalcast",
    display_name="GoalCast",
    bot_username="GoalCastBot",
    tagline={
        "en": "Football reads, every matchday.",
        "es": "Lecturas de fútbol, cada jornada.",
    },
    character=Character(
        name="Diego",
        role="football analyst",
        persona=(
            "You are {name} — {role} running the {brand} Telegram channel. "
            "Tone: passionate, data-driven, no hype. You break down matches "
            "and invite people to follow the channel for the full reads."
        ),
        win_rate_display=0.74,
    ),
    sport=SportConfig(vertical=Vertical.FOOTBALL),
    offer=Offer(),  # в channel-режиме оффер не показывается, но поле остаётся валидным
    cta=CTA(
        mode=CTAMode.CHANNEL,
        channel_url="https://t.me/goalcast_channel",
        channel_handle="@goalcast_channel",
        button_label={"en": "📣 Join the channel", "es": "📣 Unite al canal"},
    ),
    funnel=Funnel(repeat_enabled=False),   # канал не дожимаем на депозит
    privacy_url="https://goalcast.example.workers.dev/privacy",
    copy_pack="copy_goalcast",
)

#: Все доступные бренды.
BRANDS: dict[str, Brand] = {
    METAPLAY.id: METAPLAY,
    GOALCAST.id: GOALCAST,
}


# ──────────────────────────────────────────────────────────────────────────────
#  Выбор активного бренда
# ──────────────────────────────────────────────────────────────────────────────

_active_id = os.environ.get("BRAND_ID", METAPLAY.id).strip().lower()
if _active_id not in BRANDS:
    raise RuntimeError(
        f"Unknown BRAND_ID={_active_id!r}. Available: {', '.join(BRANDS)}"
    )

#: Активный бренд этого инстанса (с применёнными env-оверрайдами).
BRAND: Brand = BRANDS[_active_id].with_env_overrides()
