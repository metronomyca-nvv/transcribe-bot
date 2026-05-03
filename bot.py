import asyncio
import json
import logging
import os
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
SCENARIOS_PATH = Path(__file__).parent / "scenarios.json"
DATA_DIR = Path(__file__).parent / "data"
USERS_PATH = DATA_DIR / "users.json"
TMP_DIR = Path(__file__).parent / "tmp"
TMP_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)

ADMIN_IDS: set[int] = {int(x) for x in ADMIN_IDS_RAW.split(",") if x.strip().isdigit()}


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


def load_scenarios():
    data = json.loads(SCENARIOS_PATH.read_text(encoding="utf-8"))
    return data["scenarios"], data.get("defaults", {})


def build_keyboard(scenarios):
    rows = [[KeyboardButton(text=s["buttonTitle"])] for s in scenarios]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


class UserState(StatesGroup):
    waiting_for_scenario = State()


def register_user(users_data: dict, tg_user: User):
    uid = str(tg_user.id)
    users_data["registry"][uid] = {
        "id": tg_user.id,
        "username": tg_user.username,
        "first_name": tg_user.first_name,
        "last_name": tg_user.last_name,
    }
    save_users(users_data)


async def transcribe(client: AsyncOpenAI, audio_path: Path, language: str = "ru") -> str:
    with open(audio_path, "rb") as f:
        result = await client.audio.transcriptions.create(
            model="whisper-1",
            file=f,
            language=language,
        )
    return result.text


async def apply_llm(client: AsyncOpenAI, transcript: str, scenario: dict) -> str:
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
    return response.choices[0].message.content


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

    def is_allowed(user_id: int) -> bool:
        return user_id in ADMIN_IDS or user_id in users_data["whitelist"]

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
            await message.answer("Использование: /add <user_id>")
            return
        arg = parts[1].lstrip("@")
        # поиск по ID или username
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

        if message.voice:
            file_id = message.voice.file_id
        elif message.audio:
            file_id = message.audio.file_id
        elif message.video:
            file_id = message.video.file_id
        elif message.document:
            file_id = message.document.file_id
        else:
            return

        await state.update_data(file_id=file_id)
        await state.set_state(UserState.waiting_for_scenario)
        await message.answer("Файл получен. Выбери сценарий:", reply_markup=kb)

    @dp.message(UserState.waiting_for_scenario, F.text.in_(list(title_to_scenario.keys())))
    async def process_scenario(message: Message, state: FSMContext):
        data = await state.get_data()
        file_id = data.get("file_id")
        scenario = title_to_scenario[message.text]
        await state.clear()

        status_msg = await message.answer("⏳ Скачиваю и расшифровываю...")

        tg_file = await bot.get_file(file_id)
        suffix = Path(tg_file.file_path).suffix or ".ogg"
        tmp_path = TMP_DIR / f"{file_id}{suffix}"
        await bot.download_file(tg_file.file_path, destination=tmp_path)

        try:
            transcript = await transcribe(oai, tmp_path, language=language)
            log.info("user=%s scenario=%s transcript_len=%d", message.from_user.id, scenario["id"], len(transcript))

            if "llm" in scenario.get("pipeline", []):
                await status_msg.edit_text("⏳ Обрабатываю текст...")
                result_text = await apply_llm(oai, transcript, scenario)
            else:
                result_text = transcript

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
