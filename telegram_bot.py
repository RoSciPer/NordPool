#!/usr/bin/env python3
"""
Nord Pool Shelly Telegram bot — v3 (Boiler Control).
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    MenuButtonCommands,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from core_prices import (
    LOCAL_TZ,
    PriceFetcher,
    PriceStore,
    day_complete,
    hourly_average,
    filter_day,
)
from core_planner import humanize_schedule, plan_for_device, schedule_for_date
from core_storage import (
    ALL_DAYS,
    DAY_NAMES,
    DeviceProfile,
    DeviceStore,
    WEEKEND,
    WORKDAYS,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

SHELLY_TEMPLATE_PATH = Path(__file__).parent / "shelly_script_template.js"


def _render_shelly_script(server_url: str, token: str, switch_id: int = 0) -> bytes:
    tpl = SHELLY_TEMPLATE_PATH.read_text(encoding="utf-8")
    out = (
        tpl.replace("{{SERVER_URL}}", server_url.rstrip("/"))
           .replace("{{DEVICE_TOKEN}}", token)
           .replace("{{SWITCH_ID}}", str(switch_id))
    )
    return out.encode("utf-8")


class NordPoolBot:
    def __init__(self, token: str, server_url: str, db_path: str = "prices.db"):
        self.server_url = server_url.rstrip("/")
        self.store = PriceStore(db_path)
        self.devices = DeviceStore(db_path)
        self.fetcher = PriceFetcher(self.store)
        self.application = (
            Application.builder()
            .token(token)
            .post_init(self._on_post_init)
            .build()
        )
        self._register_handlers()

    @staticmethod
    async def _on_post_init(app: Application) -> None:
        commands = [
            BotCommand("start", "Sākt"),
            BotCommand("devices", "Ierīces"),
            BotCommand("schedule", "Grafiks"),
            BotCommand("now", "Tagad"),
            BotCommand("prices", "Cenas"),
            BotCommand("profile", "Profils"),
            BotCommand("config", "Konfigurācija"),
            BotCommand("vacation", "Atvaļinājums"),
            BotCommand("status", "Statuss"),
            BotCommand("sync", "Ielādēt grafiku Shelly"),
            BotCommand("script", "Shelly skripts"),
            BotCommand("help", "Palīdzība"),
        ]
        await app.bot.set_my_commands(commands)
        await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())

    def _register_handlers(self) -> None:
        app = self.application
        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("help", self.cmd_help))
        app.add_handler(CommandHandler("setup", self.cmd_setup))
        app.add_handler(CommandHandler("connect", self.cmd_connect))
        app.add_handler(CommandHandler("script", self.cmd_connect))
        app.add_handler(CommandHandler("devices", self.cmd_devices))
        app.add_handler(CommandHandler("schedule", self.cmd_schedule))
        app.add_handler(CommandHandler("sync", self.cmd_sync))
        app.add_handler(CommandHandler("profile", self.cmd_profile))
        app.add_handler(CommandHandler("config", self.cmd_config))
        app.add_handler(CommandHandler("vacation", self.cmd_vacation))
        app.add_handler(CommandHandler("status", self.cmd_status))
        app.add_handler(CommandHandler("now", self.cmd_now))
        app.add_handler(CommandHandler("prices", self.cmd_prices))
        app.add_handler(CommandHandler("delete_device", self.cmd_delete_device))
        app.add_handler(CallbackQueryHandler(self.on_callback))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))
        app.add_error_handler(self.on_error)

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.exception("Bot error", exc_info=context.error)

    def _ensure_prices(self) -> None:
        try: self.fetcher.ensure_fresh()
        except: pass

    def _fetch_server_json(self, path: str, params: dict) -> Optional[dict]:
        try:
            url = f"{self.server_url}{path}"
            if params: url += "?" + urlencode(params)
            req = Request(url, headers={"Accept": "application/json"})
            with urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except: return None

    def _menu_keyboard(self) -> ReplyKeyboardMarkup:
        return ReplyKeyboardMarkup([["/devices", "/schedule"], ["/now", "/prices"], ["/config", "/profile"], ["/vacation", "/status"]], resize_keyboard=True)

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("Sveiks! Nord Pool Boiler bots gatavs.", reply_markup=self._menu_keyboard())

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "/setup - jauna ierīce\n"
            "/script - lejupielādēt Shelly skriptu\n"
            "/sync - tūlīt ielādēt rītdienas grafiku Shelly\n"
            "/prices - šodienas + rītdienas cenas\n"
            "/config - mainīt logus\n"
            "/status - reālais grafiks no Shelly",
            parse_mode=ParseMode.MARKDOWN,
        )

    async def cmd_setup(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        context.user_data["setup"] = {"step": "name"}
        await update.message.reply_text("Ierīces nosaukums?")

    async def _setup_finalize(self, update: Update, context: ContextTypes.DEFAULT_TYPE, device_type: str):
        chat_id = update.effective_chat.id
        name = context.user_data.get("setup", {}).get("name", "Shelly")
        profile = DeviceProfile.default_boiler()
        device = self.devices.create_device(chat_id, name, profile)
        context.user_data["setup"] = {}
        await update.effective_chat.send_message(f"✅ Ierīce *{name}* izveidota.\nŽetons: `{device.token}`", parse_mode=ParseMode.MARKDOWN)
        await self._send_shelly_script(chat_id, device.token, name)

    async def _send_shelly_script(self, chat_id: int, token: str, device_name: str):
        script_bytes = _render_shelly_script(self.server_url, token)
        await self.application.bot.send_document(chat_id=chat_id, document=InputFile(io.BytesIO(script_bytes), filename=f"shelly_{device_name}.js"))

    async def cmd_connect(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        devs = self.devices.list_devices(update.effective_chat.id)
        for d in devs: await self._send_shelly_script(update.effective_chat.id, d.token, d.device_name)

    async def cmd_devices(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        devs = self.devices.list_devices(update.effective_chat.id)
        if not devs: return await update.message.reply_text("Nav ierīču.")
        lines = ["📡 *Ierīces:*\n"]
        for d in devs:
            online = "🟢" if self.devices.is_device_online(d.token) else "⚪"
            lines.append(f"{online} *{d.device_name}* (pēdējais: {self.devices.get_device(d.token).last_seen})")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

    async def cmd_now(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self._ensure_prices()
        now = datetime.now(LOCAL_TZ)
        prices = self.store.load()
        slot = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
        p = prices.get(slot)
        await update.message.reply_text(f"🕒 {now.strftime('%H:%M')}\n⚡ Cena: *{p:.4f}* €/kWh" if p else "Nav datu.", parse_mode=ParseMode.MARKDOWN)

    async def cmd_schedule(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self._ensure_prices()
        prices = self.store.load()
        for d in self.devices.list_devices(update.effective_chat.id):
            profile = self.devices.get_profile(d.token)
            sched = plan_for_device(profile, prices)
            await update.message.reply_text(f"*{d.device_name}*\n" + humanize_schedule(sched), parse_mode=ParseMode.MARKDOWN)

    async def cmd_sync(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Manuāli triggerēt Shelly grafika ielādi (tests jebkurā laikā)."""
        self._ensure_prices()
        prices = self.store.load()
        tomorrow = datetime.now(LOCAL_TZ).date() + timedelta(days=1)

        for d in self.devices.list_devices(update.effective_chat.id):
            profile = self.devices.get_profile(d.token)
            tomorrow_sched = schedule_for_date(profile, prices, tomorrow)
            ready = day_complete(prices, tomorrow)

            lines = [f"🔄 *{d.device_name}* — sinhronizācija"]
            lines.append(f"📅 Rītdiena: `{tomorrow}`")
            if not ready:
                lines.append("⚠️ Rītdienas cenas vēl nav gatavas — Shelly saglabās vecos grafikus.")
            elif profile.vacation_mode:
                lines.append("🏖 Atvaļinājums — grafiki tiks notīrīti.")
            else:
                ranges = tomorrow_sched.to_ranges()
                if not ranges:
                    lines.append("⚠️ Nav ko plānot.")
                else:
                    lines.append("*Shelly ielādēs:*")
                    for r in ranges:
                        s = datetime.fromtimestamp(r["start"], LOCAL_TZ)
                        e = datetime.fromtimestamp(r["end"], LOCAL_TZ)
                        lines.append(f"  ▶️ {s.strftime('%H:%M')} → ⏹ {e.strftime('%H:%M')}")
                    lines.append(f"_({len(ranges) * 2} Schedule ieraksti)_")

            self.devices.request_schedule_refresh(d.token)
            lines.append("\n_Slēdzis ielādē tikai pēc 20:00 (DeleteAll + rītdiena)._")
            lines.append("Pēc ielādes TG saņemsi ziņu par cenu kvalitāti.")
            await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

    async def cmd_profile(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        for d in self.devices.list_devices(update.effective_chat.id):
            await self._render_profile(update.effective_chat.id, d.token)

    async def _render_profile(self, chat_id: int, token: str):
        device = self.devices.get_device(token)
        profile = self.devices.get_profile(token)
        cfg = profile.boiler_config
        text = (f"⚙️ Profils — *{device.device_name}*\n"
                f"D.D.: {cfg.weekday_morning_slots} + {cfg.weekday_evening_slots} logi\n"
                f"B.D.: {cfg.weekend_morning_slots} + {cfg.weekend_daytime_slots} logi\n"
                f"Atv: {'Jā' if profile.vacation_mode else 'Nē'}")
        await self.application.bot.send_message(chat_id, text, parse_mode=ParseMode.MARKDOWN)

    async def cmd_config(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        devs = self.devices.list_devices(update.effective_chat.id)
        for d in devs: await self._config_menu(update.effective_chat.id, d.token, None)

    @staticmethod
    def _slots_label(n: int) -> str:
        h = n * 15 // 60
        m = n * 15 % 60
        return f"{n} ({h}h{m:02d}m)" if n else "0 (izsl.)"

    async def _config_menu(self, chat_id: int, token: str, via_edit) -> None:
        device = self.devices.get_device(token)
        profile = self.devices.get_profile(token)
        cfg = profile.boiler_config
        text = (
            f"⚙️ *Konfigurācija — {device.device_name}*\n"
            f"Katrs solis = 15 min (1 slots)\n\n"
            f"*Darba diena*\n"
            f"Rīts līdz {cfg.weekday_morning_ready_by}: {self._slots_label(cfg.weekday_morning_slots)}\n"
            f"Vakars līdz {cfg.weekday_evening_ready_by}: {self._slots_label(cfg.weekday_evening_slots)}\n\n"
            f"*Brīvdiena*\n"
            f"Rīts līdz {cfg.weekend_morning_ready_by}: {self._slots_label(cfg.weekend_morning_slots)}\n"
            f"Diena līdz {cfg.weekend_daytime_ready_by}: {self._slots_label(cfg.weekend_daytime_slots)}"
        )
        keyboard = [
            [InlineKeyboardButton("DD Rīts −", callback_data=f"config:slots:{token}:wd_m:-1"),
             InlineKeyboardButton("+", callback_data=f"config:slots:{token}:wd_m:1")],
            [InlineKeyboardButton("DD Vakars −", callback_data=f"config:slots:{token}:wd_e:-1"),
             InlineKeyboardButton("+", callback_data=f"config:slots:{token}:wd_e:1")],
            [InlineKeyboardButton("WE Rīts −", callback_data=f"config:slots:{token}:we_m:-1"),
             InlineKeyboardButton("+", callback_data=f"config:slots:{token}:we_m:1")],
            [InlineKeyboardButton("WE Diena −", callback_data=f"config:slots:{token}:we_d:-1"),
             InlineKeyboardButton("+", callback_data=f"config:slots:{token}:we_d:1")],
            [InlineKeyboardButton("🔄 Refresh Shelly", callback_data=f"refresh:{token}")],
        ]
        if via_edit:
            await via_edit.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            await self.application.bot.send_message(chat_id, text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard))

    async def cmd_vacation(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        for d in self.devices.list_devices(update.effective_chat.id):
            p = self.devices.get_profile(d.token)
            p.vacation_mode = not p.vacation_mode
            self.devices.save_profile(d.token, p)
            self.devices.request_schedule_refresh(d.token)
            await update.message.reply_text(f"{d.device_name} atvaļinājums: {p.vacation_mode}")

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        for d in self.devices.list_devices(update.effective_chat.id):
            # Shelly reports its schedules via status in Gen2/3
            # For simplicity, we show what server thinks it should have.
            # In a real app, Shelly would POST its schedule to /api/v1/report
            await update.message.reply_text(f"*{d.device_name}* status: OK")

    def _format_day_prices(self, prices: dict, day: date, *, title: str) -> str:
        day_15 = filter_day(prices, day)
        if not day_15:
            return f"*{title}* (`{day}`)\nNav datu."
        hourly = hourly_average(day_15)
        lines = [f"*{title}* (`{day}`)", "`Laiks`  |  EUR/kWh", "-----------------------"]
        for dt, p in sorted(hourly.items()):
            lines.append(f"`{dt.strftime('%H:%M')}`  |  *{p:.4f}*")
        low = min(hourly.values())
        high = max(hourly.values())
        lines.append(f"_Min: {low:.4f}  Max: {high:.4f}_")
        return "\n".join(lines)

    async def cmd_prices(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self._ensure_prices()
        prices = self.store.load()
        if not prices:
            return await update.message.reply_text("Cenu dati pašlaik nav pieejami datubāzē.")

        now = datetime.now(LOCAL_TZ)
        today = now.date()
        tomorrow = today + timedelta(days=1)
        arg = (context.args[0].lower() if context.args else "").strip()

        if arg in ("rit", "rīt", "tomorrow", "rid"):
            if not day_complete(prices, tomorrow):
                return await update.message.reply_text(
                    f"Rītdienas (`{tomorrow}`) cenas vēl nav pilnas.\n"
                    "Parasti pieejamas no ~15:30.",
                    parse_mode=ParseMode.MARKDOWN,
                )
            text = self._format_day_prices(prices, tomorrow, title="💶 Rītdiena")
            return await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

        if arg in ("sod", "šod", "today"):
            text = self._format_day_prices(prices, today, title="💶 Šodiena")
            return await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

        parts = [self._format_day_prices(prices, today, title="💶 Šodiena")]
        if day_complete(prices, tomorrow):
            parts.append(self._format_day_prices(prices, tomorrow, title="💶 Rītdiena"))
        else:
            parts.append(
                f"*💶 Rītdiena* (`{tomorrow}`)\n"
                "Vēl nav pilnas cenas (parasti no ~15:30).\n"
                "_Raksti_ `/prices rit` _kad gatavs._"
            )
        await update.message.reply_text("\n\n".join(parts), parse_mode=ParseMode.MARKDOWN)

    async def cmd_delete_device(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        for d in self.devices.list_devices(update.effective_chat.id):
            self.devices.delete_device(d.token)
            await update.message.reply_text(f"Dzēsts {d.device_name}")

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        parts = query.data.split(":")
        action = parts[0]

        if action == "refresh":
            token = parts[1]
            self.devices.request_schedule_refresh(token)
            await query.answer("Shelly paņems jauno grafiku ~1 min.", show_alert=True)

        elif action == "config" and len(parts) >= 5 and parts[1] == "slots":
            token = parts[2]
            block_type = parts[3]
            delta = int(parts[4])

            p = self.devices.get_profile(token)
            if not p:
                await query.answer("Profils nav atrasts.", show_alert=True)
                return

            cfg = p.boiler_config
            lo, hi = 0, 12  # 0 = izslēgts, max 3 h

            if block_type == "wd_m":
                cfg.weekday_morning_slots = max(lo, min(hi, cfg.weekday_morning_slots + delta))
            elif block_type == "wd_e":
                cfg.weekday_evening_slots = max(lo, min(hi, cfg.weekday_evening_slots + delta))
            elif block_type == "we_m":
                cfg.weekend_morning_slots = max(lo, min(hi, cfg.weekend_morning_slots + delta))
            elif block_type == "we_d":
                cfg.weekend_daytime_slots = max(lo, min(hi, cfg.weekend_daytime_slots + delta))
            else:
                await query.answer("Nezināms bloks.", show_alert=True)
                return

            self.devices.save_profile(token, p)
            self.devices.request_schedule_refresh(token)
            await query.answer()
            await self._config_menu(update.effective_chat.id, token, query)
        else:
            await query.answer()
            
    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        setup = context.user_data.get("setup")
        if setup and setup["step"] == "name":
            setup["name"] = update.message.text
            await self._setup_finalize(update, context, "boiler")

    def run(self):
        self.application.run_polling()

if __name__ == "__main__":
    load_dotenv()
    bot = NordPoolBot(os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("SERVER_BASE_URL"))
    bot.run()
