import asyncio
import json
import logging
import os
from datetime import date
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import FSInputFile, KeyboardButton, Message, ReplyKeyboardMarkup, User
from dotenv import load_dotenv
from openai import AsyncOpenAI
from pipeline import estimate_cost, load_scenarios, render_text_artifact, run_pipeline

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")
MAX_FILE_MB = float(os.getenv("MAX_FILE_MB", "20"))
MAX_REQUESTS_PER_DAY = int(os.getenv("MAX_REQUESTS_PER_DAY", "30"))
DAILY_BUDGET_USD = float(os.getenv("DAILY_BUDGET_USD", "10.0"))
MONTHLY_BUDGET_USD = float(os.getenv("MONTHLY_BUDGET_USD", "50.0"))

SCENARIOS_PATH = Path(__file__).parent / "scenarios.json"
DATA_DIR = Path(__file__).parent / "data"
USERS_PATH = DATA_DIR / "users.json"
STATS_PATH = DATA_DIR / "stats.json"
TMP_DIR = Path(__file__).parent / "tmp"
TMP_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)

ADMIN_IDS: set[int] = {int(x) for x in ADMIN_IDS_RAW.split(",") if x.strip().isdigit()}

HELP_TEXT = """*Что умеет бот*
Бот принимает голосовые сообщения и аудиофайлы и выдаёт результат в выбранном формате (сценарии/кнопки).

*Быстрый старт*
1) Отправьте голосовое сообщение или файл аудио/видео (mp4/m4a)
2) Дождитесь меню сценариев и выберите нужную кнопку
3) Получите результат в чате или файлом

*Что такое сценарии*
Сценарии — это кнопки, которые определяют в каком виде бот оформит результат (например: "Дословно", "ТЗ", "Резюме встречи", "Мысли/заметки").

*Как узнать свой Telegram ID*
Отправьте команду /whoami

*Доступ и подключение*
Отправьте администратору ваш Telegram ID (из /whoami). После подключения бот пришлёт уведомление в личные сообщения."""


# --- Users ---

def load_users() -> dict:
    if USERS_PATH.exists():
        return json.loads(USERS_PATH.read_text(encoding="utf-8"))
    return {"whitelist": [], "registry": {}}


def save_users(data: dict):
    USERS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def user_label(info: dict) -> str:
    name = info.get("first_name", "")
    if info.get("last_name"):
        name += f" {info['last_name']}"
    username = f" @{info['username']}" if info.get("username") else ""
    return f"{info['id']} —{username} {name}".strip()


def register_user(users_data: dict, tg_user: User):
    uid = str(tg_user.id)
    users_data["registry"][uid] = {
        "id": tg_user.id,
        "username": tg_user.username,
        "first_name": tg_user.first_name,
        "last_name": tg_user.last_name,
    }
    save_users(users_data)


# --- Stats / Limits ---

def load_stats() -> dict:
    if STATS_PATH.exists():
        return json.loads(STATS_PATH.read_text(encoding="utf-8"))
    return {"daily": {}, "monthly": {}, "users_daily": {}}


def save_stats(stats: dict):
    STATS_PATH.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")


def today_key() -> str:
    return date.today().isoformat()


def month_key() -> str:
    return date.today().strftime("%Y-%m")


def get_user_requests_today(stats: dict, user_id: int) -> int:
    return stats["users_daily"].get(today_key(), {}).get(str(user_id), 0)


def increment_user_requests(stats: dict, user_id: int):
    day = today_key()
    if day not in stats["users_daily"]:
        stats["users_daily"][day] = {}
    stats["users_daily"][day][str(user_id)] = stats["users_daily"][day].get(str(user_id), 0) + 1
    save_stats(stats)


def add_cost(stats: dict, cost_usd: float):
    day = today_key()
    month = month_key()
    stats["daily"][day] = round(stats["daily"].get(day, 0.0) + cost_usd, 6)
    stats["monthly"][month] = round(stats["monthly"].get(month, 0.0) + cost_usd, 6)
    save_stats(stats)


def get_daily_cost(stats: dict) -> float:
    return stats["daily"].get(today_key(), 0.0)


def get_monthly_cost(stats: dict) -> float:
    return stats["monthly"].get(month_key(), 0.0)


def build_keyboard(scenarios):
    rows = [[KeyboardButton(text=s["buttonTitle"])] for s in scenarios]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


class UserState(StatesGroup):
    waiting_for_scenario = State()

async def main():
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set in .env")
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set in .env")

    tg_session = AiohttpSession()
    tg_session._connector_init = {"ssl": False}
    bot = Bot(token=BOT_TOKEN, session=tg_session)
    dp = Dispatcher(storage=MemoryStorage())
    oai = AsyncOpenAI(api_key=OPENAI_API_KEY)

    scenarios, defaults = load_scenarios(SCENARIOS_PATH)
    kb = build_keyboard(scenarios)
    title_to_scenario = {s["buttonTitle"]: s for s in scenarios}
    language = defaults.get("language", "ru")

    users_data = load_users()
    stats = load_stats()

    def is_allowed(user_id: int) -> bool:
        return user_id in ADMIN_IDS or user_id in users_data["whitelist"]

    async def notify_admins(text: str):
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(admin_id, text)
            except Exception:
                pass

    @dp.message(CommandStart(), StateFilter("*"))
    async def start(message: Message, state: FSMContext):
        await state.clear()
        register_user(users_data, message.from_user)
        uid = message.from_user.id
        if not is_allowed(uid):
            await message.answer(
                "Привет! Я голосовой бот группы «Метрономика».\n"
                "Вы пока не подключены.\n\n"
                "Передайте ваш Telegram ID администратору для получения доступа. "
                "После подключения бот пришлёт вам уведомление в личные сообщения.\n\n"
                "Команды: /help — помощь, /whoami — показать ваш ID."
            )
            await message.answer(f"`{uid}`", parse_mode="Markdown")
            return
        await message.answer(
            f"Привет! Я голосовой бот группы «Метрономика» — делаю текст из аудио и голосовых сообщений.\n\n"
            f"Отправьте голосовое или файл (mp4/m4a) — я покажу меню сценариев.\n\n"
            f"Ваш Telegram ID: `{uid}`\n"
            f"Команды: /help — помощь, /whoami — показать ваш ID.",
            parse_mode="Markdown",
        )

    @dp.message(Command("whoami"), StateFilter("*"))
    async def cmd_whoami(message: Message):
        register_user(users_data, message.from_user)
        uid = message.from_user.id
        username = f"@{message.from_user.username}" if message.from_user.username else "не задан"
        await message.answer(
            f"Ваш Telegram ID: `{uid}`\n"
            f"Username: {username}\n\n"
            f"Передайте ID администратору для подключения или изменения сценариев. "
            f"После подключения бот пришлёт вам уведомление в личные сообщения.",
            parse_mode="Markdown",
        )

    @dp.message(Command("help"), StateFilter("*"))
    async def cmd_help(message: Message):
        await message.answer(HELP_TEXT, parse_mode="Markdown")

    @dp.message(Command("add"), StateFilter("*"))
    async def cmd_add(message: Message):
        if message.from_user.id not in ADMIN_IDS:
            return
        parts = message.text.split()
        if len(parts) != 2:
            await message.answer("Использование: /add <user_id или @username>")
            return
        arg = parts[1].lstrip("@")
        uid = None
        if arg.isdigit():
            uid = int(arg)
        else:
            for info in users_data["registry"].values():
                if info.get("username", "").lower() == arg.lower():
                    uid = info["id"]
                    break
        if uid is None:
            await message.answer(f"Пользователь '{arg}' не найден. Он должен сначала написать боту /start.")
            return
        if uid not in users_data["whitelist"]:
            users_data["whitelist"].append(uid)
            save_users(users_data)
        info = users_data["registry"].get(str(uid), {"id": uid})
        await message.answer(f"Добавлен: {user_label(info)}")
        log.info("Admin %s added user %s", message.from_user.id, uid)
        # Уведомление новому пользователю
        try:
            await bot.send_message(
                uid,
                "Привет! Вас подключили к голосовому боту группы «Метрономика».\n"
                "Перейдите @golos_m2_bot — нажмите /start, чтобы начать.",
            )
        except Exception:
            pass

    @dp.message(Command("remove"), StateFilter("*"))
    async def cmd_remove(message: Message):
        if message.from_user.id not in ADMIN_IDS:
            return
        parts = message.text.split()
        if len(parts) != 2:
            await message.answer("Использование: /remove <user_id или @username>")
            return
        arg = parts[1].lstrip("@")
        uid = None
        if arg.isdigit():
            uid = int(arg)
        else:
            for info in users_data["registry"].values():
                if info.get("username", "").lower() == arg.lower():
                    uid = info["id"]
                    break
        if uid is None:
            await message.answer(f"Пользователь '{arg}' не найден.")
            return
        if uid in users_data["whitelist"]:
            users_data["whitelist"].remove(uid)
            save_users(users_data)
        info = users_data["registry"].get(str(uid), {"id": uid})
        await message.answer(f"Удалён: {user_label(info)}")
        log.info("Admin %s removed user %s", message.from_user.id, uid)

    @dp.message(Command("users"), StateFilter("*"))
    async def cmd_users(message: Message):
        if message.from_user.id not in ADMIN_IDS:
            return
        if not users_data["whitelist"]:
            await message.answer("Whitelist пуст.")
            return
        lines = []
        for uid in users_data["whitelist"]:
            info = users_data["registry"].get(str(uid), {"id": uid})
            lines.append(user_label(info))
        await message.answer("Пользователи:\n" + "\n".join(lines))

    @dp.message(Command("stats"), StateFilter("*"))
    async def cmd_stats(message: Message):
        if message.from_user.id not in ADMIN_IDS:
            return
        daily = get_daily_cost(stats)
        monthly = get_monthly_cost(stats)
        await message.answer(
            f"Расходы OpenAI:\n"
            f"Сегодня: ${daily:.4f} / ${DAILY_BUDGET_USD:.2f}\n"
            f"Месяц: ${monthly:.4f} / ${MONTHLY_BUDGET_USD:.2f}"
        )

    @dp.message(F.voice | F.audio | F.video | F.document)
    async def handle_audio(message: Message, state: FSMContext):
        if not is_allowed(message.from_user.id):
            uid = message.from_user.id
            await message.answer(
                "У вас нет доступа к боту.\n\n"
                "Передайте ваш Telegram ID администратору для получения доступа. "
                "После подключения бот пришлёт вам уведомление в личные сообщения."
            )
            await message.answer(f"`{uid}`", parse_mode="Markdown")
            return
        register_user(users_data, message.from_user)

        if get_user_requests_today(stats, message.from_user.id) >= MAX_REQUESTS_PER_DAY:
            await message.answer(f"*Лимит запросов* исчерпан: {MAX_REQUESTS_PER_DAY} в день. Попробуйте завтра.", parse_mode="Markdown")
            return

        if get_daily_cost(stats) >= DAILY_BUDGET_USD:
            await message.answer("*Дневной бюджет* бота исчерпан. Обратитесь к администратору.", parse_mode="Markdown")
            return
        if get_monthly_cost(stats) >= MONTHLY_BUDGET_USD:
            await message.answer("*Месячный бюджет* бота исчерпан. Обратитесь к администратору.", parse_mode="Markdown")
            return

        if message.voice:
            file_id = message.voice.file_id
            file_size = message.voice.file_size or 0
        elif message.audio:
            file_id = message.audio.file_id
            file_size = message.audio.file_size or 0
        elif message.video:
            file_id = message.video.file_id
            file_size = message.video.file_size or 0
        elif message.document:
            file_id = message.document.file_id
            file_size = message.document.file_size or 0
        else:
            return

        max_bytes = MAX_FILE_MB * 1024 * 1024
        if file_size > max_bytes:
            await message.answer(f"Файл слишком большой. Максимум {MAX_FILE_MB:.0f} МБ.")
            return

        await state.update_data(file_id=file_id, file_size=file_size)
        await state.set_state(UserState.waiting_for_scenario)
        await message.answer("*Голос получил.* Приступаю…\n\nВыберите сценарий для текста:", reply_markup=kb, parse_mode="Markdown")

    @dp.message(UserState.waiting_for_scenario, F.text.in_(list(title_to_scenario.keys())))
    async def process_scenario(message: Message, state: FSMContext):
        data = await state.get_data()
        file_id = data.get("file_id")
        file_size = data.get("file_size", 0)
        scenario = title_to_scenario[message.text]
        await state.clear()

        status_msg = await message.answer(f"Отлично, делаю: *{message.text}*. Работа может занять пару минут.", parse_mode="Markdown")

        tg_file = await bot.get_file(file_id)
        suffix = Path(tg_file.file_path).suffix or ".ogg"
        tmp_path = TMP_DIR / f"{file_id}{suffix}"
        await bot.download_file(tg_file.file_path, destination=tmp_path)

        try:
            pipeline_result = await run_pipeline(
                oai,
                tmp_path,
                scenario,
                language=language,
            )
            increment_user_requests(stats, message.from_user.id)
            log.info(
                "user=%s scenario=%s transcript_len=%d",
                message.from_user.id,
                scenario["id"],
                len(pipeline_result.transcript),
            )

            audio_sec = file_size / 16000
            cost = estimate_cost(
                audio_sec,
                pipeline_result.input_tokens,
                pipeline_result.output_tokens,
            )
            add_cost(stats, cost)
            log.info("user=%s cost=$%.5f daily=$%.4f monthly=$%.4f", message.from_user.id, cost, get_daily_cost(stats), get_monthly_cost(stats))

            if get_daily_cost(stats) >= DAILY_BUDGET_USD * 0.8:
                await notify_admins(f"⚠️ Дневной бюджет использован на 80%+: ${get_daily_cost(stats):.4f} / ${DAILY_BUDGET_USD}")
            if get_monthly_cost(stats) >= MONTHLY_BUDGET_USD * 0.8:
                await notify_admins(f"⚠️ Месячный бюджет использован на 80%+: ${get_monthly_cost(stats):.4f} / ${MONTHLY_BUDGET_USD}")

            if pipeline_result.deliver == "file_txt":
                txt_path = render_text_artifact(
                    TMP_DIR,
                    pipeline_result.filename,
                    pipeline_result.result_text,
                )
                await status_msg.delete()
                await message.answer_document(
                    FSInputFile(txt_path, filename=pipeline_result.filename),
                    caption=pipeline_result.caption,
                )
                await message.answer("*Готово!* Жду новых голосов!", parse_mode="Markdown")
                txt_path.unlink(missing_ok=True)
            else:
                await status_msg.edit_text(pipeline_result.result_text, parse_mode="Markdown")
                await message.answer("*Готово!* Жду новых голосов!", parse_mode="Markdown")

        except Exception as e:
            log.error("Error processing scenario %s for user %s: %s", scenario["id"], message.from_user.id, e)
            await status_msg.edit_text(
                "Не получилось обработать файл. Попробуйте отправить ещё раз или другим форматом.\n"
                "Если повторяется — напишите администратору и пришлите время/описание."
            )

        finally:
            tmp_path.unlink(missing_ok=True)

    @dp.message(F.text.in_(list(title_to_scenario.keys())))
    async def scenario_without_audio(message: Message):
        await message.answer("*Голос получил.* Приступаю…\n\nВыберите сценарий для текста:", reply_markup=kb, parse_mode="Markdown")

    log.info("Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
