import asyncio
import json
import logging
import os
from datetime import date, datetime
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

# Стоимость OpenAI (USD)
WHISPER_COST_PER_MIN = 0.006
GPT_INPUT_COST_PER_1K = 0.000150
GPT_OUTPUT_COST_PER_1K = 0.000600


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


def estimate_cost(audio_duration_sec: float, input_tokens: int, output_tokens: int) -> float:
    whisper = (audio_duration_sec / 60) * WHISPER_COST_PER_MIN
    gpt_in = (input_tokens / 1000) * GPT_INPUT_COST_PER_1K
    gpt_out = (output_tokens / 1000) * GPT_OUTPUT_COST_PER_1K
    return round(whisper + gpt_in + gpt_out, 6)


# --- Scenarios ---

def load_scenarios():
    data = json.loads(SCENARIOS_PATH.read_text(encoding="utf-8"))
    return data["scenarios"], data.get("defaults", {})


def build_keyboard(scenarios):
    rows = [[KeyboardButton(text=s["buttonTitle"])] for s in scenarios]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


class UserState(StatesGroup):
    waiting_for_scenario = State()


async def transcribe(client: AsyncOpenAI, audio_path: Path, language: str = "ru") -> str:
    with open(audio_path, "rb") as f:
        result = await client.audio.transcriptions.create(
            model="whisper-1",
            file=f,
            language=language,
        )
    return result.text


async def apply_llm(client: AsyncOpenAI, transcript: str, scenario: dict) -> tuple[str, int, int]:
    prompt = scenario["prompt"]
    template = scenario.get("template")
    if template:
        system = (
            f"{prompt}\n\n"
            f"Заполни следующий шаблон на основе транскрипта. "
            f"Выведи ТОЛЬКО заполненный шаблон:\n\n{template}"
        )
    else:
        system = prompt
    response = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": transcript},
        ],
    )
    usage = response.usage
    return response.choices[0].message.content, usage.prompt_tokens, usage.completion_tokens


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

    scenarios, defaults = load_scenarios()
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
        if not is_allowed(message.from_user.id):
            await message.answer("У вас нет доступа к боту. Обратитесь к администратору.")
            return
        await message.answer(
            "Привет! Отправь голосовое сообщение или аудиофайл — я его расшифрую.\n"
            "После этого выбери сценарий обработки.",
        )

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

    @dp.message(Command("myid"), StateFilter("*"))
    async def cmd_myid(message: Message):
        register_user(users_data, message.from_user)
        await message.answer(f"Ваш Telegram ID: `{message.from_user.id}`", parse_mode="Markdown")

    @dp.message(F.voice | F.audio | F.video | F.document)
    async def handle_audio(message: Message, state: FSMContext):
        if not is_allowed(message.from_user.id):
            await message.answer("У вас нет доступа к боту.")
            return
        register_user(users_data, message.from_user)

        # Лимит запросов в день
        if get_user_requests_today(stats, message.from_user.id) >= MAX_REQUESTS_PER_DAY:
            await message.answer(f"Вы достигли лимита {MAX_REQUESTS_PER_DAY} запросов в день. Попробуйте завтра.")
            return

        # Бюджет-лимит
        if get_daily_cost(stats) >= DAILY_BUDGET_USD:
            await message.answer("Дневной бюджет бота исчерпан. Обратитесь к администратору.")
            return
        if get_monthly_cost(stats) >= MONTHLY_BUDGET_USD:
            await message.answer("Месячный бюджет бота исчерпан. Обратитесь к администратору.")
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

        # Лимит размера файла
        max_bytes = MAX_FILE_MB * 1024 * 1024
        if file_size > max_bytes:
            await message.answer(f"Файл слишком большой. Максимум {MAX_FILE_MB:.0f} МБ.")
            return

        await state.update_data(file_id=file_id, file_size=file_size)
        await state.set_state(UserState.waiting_for_scenario)
        await message.answer("Файл получен. Выбери сценарий:", reply_markup=kb)

    @dp.message(UserState.waiting_for_scenario, F.text.in_(list(title_to_scenario.keys())))
    async def process_scenario(message: Message, state: FSMContext):
        data = await state.get_data()
        file_id = data.get("file_id")
        file_size = data.get("file_size", 0)
        scenario = title_to_scenario[message.text]
        await state.clear()

        status_msg = await message.answer("⏳ Скачиваю и расшифровываю...")

        tg_file = await bot.get_file(file_id)
        suffix = Path(tg_file.file_path).suffix or ".ogg"
        tmp_path = TMP_DIR / f"{file_id}{suffix}"
        await bot.download_file(tg_file.file_path, destination=tmp_path)

        try:
            transcript = await transcribe(oai, tmp_path, language=language)
            increment_user_requests(stats, message.from_user.id)
            log.info("user=%s scenario=%s transcript_len=%d", message.from_user.id, scenario["id"], len(transcript))

            input_tokens = output_tokens = 0
            if "llm" in scenario.get("pipeline", []):
                await status_msg.edit_text("⏳ Обрабатываю текст...")
                result_text, input_tokens, output_tokens = await apply_llm(oai, transcript, scenario)
            else:
                result_text = transcript

            # Считаем стоимость
            audio_sec = file_size / 16000  # грубая оценка
            cost = estimate_cost(audio_sec, input_tokens, output_tokens)
            add_cost(stats, cost)
            log.info("user=%s cost=$%.5f daily=$%.4f monthly=$%.4f", message.from_user.id, cost, get_daily_cost(stats), get_monthly_cost(stats))

            # Уведомление при превышении 80% бюджета
            if get_daily_cost(stats) >= DAILY_BUDGET_USD * 0.8:
                await notify_admins(f"⚠️ Дневной бюджет использован на 80%+: ${get_daily_cost(stats):.4f} / ${DAILY_BUDGET_USD}")
            if get_monthly_cost(stats) >= MONTHLY_BUDGET_USD * 0.8:
                await notify_admins(f"⚠️ Месячный бюджет использован на 80%+: ${get_monthly_cost(stats):.4f} / ${MONTHLY_BUDGET_USD}")

            if scenario.get("deliver") == "file_txt":
                txt_path = TMP_DIR / f"{file_id}.txt"
                txt_path.write_text(result_text, encoding="utf-8")
                caption = scenario.get("fileCaption", "Готово. См. файл .txt")
                await status_msg.delete()
                await message.answer_document(FSInputFile(txt_path), caption=caption)
                txt_path.unlink(missing_ok=True)
            else:
                await status_msg.edit_text(result_text)

        except Exception as e:
            log.error("Error processing scenario %s for user %s: %s", scenario["id"], message.from_user.id, e)
            await status_msg.edit_text("Произошла ошибка при обработке. Попробуй ещё раз.")

        finally:
            tmp_path.unlink(missing_ok=True)

    @dp.message(F.text.in_(list(title_to_scenario.keys())))
    async def scenario_without_audio(message: Message):
        await message.answer("Сначала отправь голосовое сообщение или аудиофайл.")

    log.info("Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
