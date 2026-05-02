#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# --- Python check ---
if command -v python3.11 &>/dev/null; then
    PYTHON=python3.11
elif command -v python3.12 &>/dev/null; then
    PYTHON=python3.12
elif command -v python3.13 &>/dev/null; then
    PYTHON=python3.13
else
    echo "Python 3.11+ не найден. Установи через: brew install python@3.11"
    exit 1
fi

# --- Virtual env ---
if [ ! -d ".venv" ]; then
    echo "Создаю виртуальное окружение..."
    $PYTHON -m venv .venv
fi
source .venv/bin/activate

# --- Dependencies ---
echo "Устанавливаю зависимости..."
pip install -q --upgrade pip
pip install -q -r requirements.txt

# --- .env ---
if [ ! -f ".env" ]; then
    echo ""
    echo "Файл .env не найден. Введи токены:"
    read -rp "TELEGRAM_BOT_TOKEN: " TG_TOKEN
    read -rp "OPENAI_API_KEY: " OAI_KEY
    echo "TELEGRAM_BOT_TOKEN=$TG_TOKEN" > .env
    echo "OPENAI_API_KEY=$OAI_KEY" >> .env
    echo ".env создан."
fi

# --- Run ---
echo ""
echo "Бот запускается... (Ctrl+C чтобы остановить)"
python bot.py
