# AI-стилист бот на Python через polling + .env + логирование + анализ фото/голоса + wardrobe/get_weather
import ast
import os
import time
import json
import openai
import requests
import csv
import base64
import re
from dotenv import load_dotenv

# Загрузка переменных из .env
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ASSISTANT_ID = os.getenv("ASSISTANT_ID")
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID"))
SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT")

openai.api_key = OPENAI_API_KEY

THREADS = {}  # user_id -> thread_id
OFFSET = None
LOG_FILE = os.path.join(os.path.dirname(__file__), "log.csv")
WARDROBE_FILE = "wardrobe.json"
PENDING_ACTIONS = {}  # user_id -> action (e.g. "awaiting_add_photo")
CACHED_PHOTOS = {}    # user_id -> image path for reprocessing

CATEGORIES = [
    "ВЕРХНЯЯ ОДЕЖДА", "ПИДЖАК", "ЮБКА", "ПЛАТЬЕ", "ШТАНЫ", "КОФТА", "ЖИЛЕТ",
    "РУБАШКА", "ТОПЫ", "ФУТБОЛКА", "СУМКА", "ОБУВЬ", "УКРАШЕНИЯ И АКСЕССУАРЫ"
]

# ===================== Утилиты =====================

def log_message(user_id, username, text, event_type="INFO"):
    with open(LOG_FILE, mode='a', newline='', encoding='utf-8') as file:
        writer = csv.writer(file)
        writer.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), user_id, username, event_type, text])

def clean_and_parse_json(raw):
    # если это строка вида "\"{\"категория\":...}\"", убираем внешние кавычки
    if isinstance(raw, str):
        if not raw:
            raise ValueError("Пустой ответ от GPT")
        if raw.startswith('"') and raw.endswith('"'):
            raw = ast.literal_eval(raw)  # превращаем в строку
        raw = raw.replace('""', '"')  # заменяем двойные кавычки

        # Убираем markdown блоки ```json
        raw = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw.strip())

        return json.loads(raw)

    # если уже dict — просто возвращаем
    elif isinstance(raw, dict):
        return raw

    else:
        raise ValueError("Неподдерживаемый формат ответа")



def parse_raw_response(text):
    text = text.strip()
    # Убираем обёртку ```json ... ``` если вдруг она есть
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    text = text.replace('""', '"')
    return text


def send_message(chat_id, text, reply_markup=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    requests.post(url, json=payload)

def send_file(chat_id, file_path):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    with open(file_path, "rb") as f:
        requests.post(url, files={"document": f}, data={"chat_id": chat_id})

def download_file(file_id):
    file_info_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getFile?file_id={file_id}"
    file_info = requests.get(file_info_url).json()
    file_path = file_info['result']['file_path']
    file_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
    local_path = f"temp/{file_id}_{os.path.basename(file_path)}"
    os.makedirs("temp", exist_ok=True)
    with open(local_path, 'wb') as f:
        f.write(requests.get(file_url).content)
    return local_path

# ===================== Ассистент =====================

def create_or_get_thread(user_id):
    if user_id not in THREADS:
        thread = openai.beta.threads.create()
        THREADS[user_id] = thread.id
    return THREADS[user_id]

def send_to_assistant(user_id, content):
    try:
        messages = []

        expecting_json = False  # 💡 По умолчанию — обычный текст

        if isinstance(content, dict):
            if content.get("image_path"):
                with open(content["image_path"], "rb") as img:
                    base64_img = base64.b64encode(img.read()).decode("utf-8")
                messages.append({
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{base64_img}"}},
                        {"type": "text", "text": f"Определи категорию СТРОГО из списка: {CATEGORIES} и опиши вещь. Верни JSON: {{\"категория\": \"...\", \"описание\": \"...\"}} ⚠️ Не используй markdown-обёртку ```json. Просто верни JSON."}
                    ]
                })
                expecting_json = True

            elif content.get("audio_path"):
                with open(content["audio_path"], "rb") as audio_file:
                    transcript = openai.audio.transcriptions.create(
                        model="whisper-1",
                        file=audio_file
                    )
                    messages.append({"role": "user", "content": transcript.text})

            else:
                raise ValueError("Неподдерживаемый тип входного контента.")

        else:
            messages.append({"role": "user", "content": content})

        # GPT Вызов
        response = openai.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            temperature=0.5,
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "load_wardrobe",
                        "description": "Получает список вещей пользователя, сгруппированный по категориям.",
                        "parameters": {
                            "type": "object",
                            "properties": {},
                        }
                    }
                }
            ],

            tool_choice="auto"
        )
        # 🛠️ Обработка tool call
        if response.choices[0].finish_reason == "tool_calls":
            for tool_call in response.choices[0].message.tool_calls:
                if tool_call.function.name == "load_wardrobe":
                    user_wardrobe = load_wardrobe()

                    if not user_wardrobe:
                        return "🧥 Гардероб пока пуст."

                    response_text = "👗 *Твой гардероб:*\n"
                    for category, items in user_wardrobe.items():
                        response_text += f"\n*{category}*\n"
                        for item in items:
                            response_text += f"• {item}\n"
                    return response_text

        text = response.choices[0].message.content
        log_message(user_id, "assistant", text, event_type="ASSISTANT_REPLY")

        return clean_and_parse_json(text) if expecting_json else text

    except Exception as e:
        log_message(user_id, "error", str(e), event_type="ERROR")
        return f"❌ Ошибка при обращении к GPT: {e}"


# ===================== Wardrobe =====================

def load_wardrobe():
    if not os.path.exists(WARDROBE_FILE):
        return {}
    try:
        with open(WARDROBE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        print(f"❌ Ошибка чтения wardrobe.json: {e}")
        return {}
def load_wardrobe_from_tool_call(user_id, items):
    wardrobe = load_wardrobe()
    user_id = str(user_id)

    if user_id not in wardrobe:
        wardrobe[user_id] = {}

    for item in items:
        category = item["type"].upper()
        entry = f"{item['name']}, {item['color']}, размер {item['size']}"

        if category not in wardrobe[user_id]:
            wardrobe[user_id][category] = []

        wardrobe[user_id][category].append(entry)

    save_wardrobe(wardrobe)
    return {"status": "ok", "message": f"{len(items)} items added to wardrobe"}


def save_wardrobe(data):
    try:
        with open(WARDROBE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"❌ Ошибка сохранения wardrobe.json: {e}")

def add_item_to_wardrobe(user_id, category, description):
    wardrobe = load_wardrobe()
    user_id = str(user_id)
    category = category.capitalize()  # защита от регистров и "КОФТА"

    # 💡 дополнительная защита от не-словаря
    if not isinstance(wardrobe, dict):
        wardrobe = {}

    if user_id not in wardrobe:
        wardrobe[user_id] = {}

    if category not in wardrobe[user_id]:
        wardrobe[user_id][category] = []

    wardrobe[user_id][category].append(description)
    save_wardrobe(wardrobe)

# ===================== Callback =====================

def process_callback(data, user_id, chat_id):
    action = data.get("data")
    if action == "wardrobe_add":
        user_state = PENDING_ACTIONS.get(user_id, {})
        item = user_state.get("data") if isinstance(user_state, dict) else None

        if item:
            try:
                category = item["категория"]
                description = item["описание"]

                # 🧼 Защита от мусора
                if not isinstance(description, str) or not isinstance(category, str):
                    raise ValueError("Некорректный формат данных (категория или описание не строка).")

                # 🧼 Очистка от недопустимых символов
                description = description.replace('\x00', '').replace('\n', ' ').strip()

                add_item_to_wardrobe(user_id, category, description)
                send_message(chat_id, "✅ Вещь добавлена в гардероб!")

            except Exception as e:
                send_message(chat_id, f"❌ Не удалось сохранить вещь: {e}")
                log_message(user_id, "error", f"wardrobe_add failed: {e}", event_type="ERROR")

        else:
            send_message(chat_id, "⚠️ Данные не найдены, начни сначала.")

        PENDING_ACTIONS.pop(user_id, None)
    elif action == "wardrobe_edit":
        send_message(chat_id, "Выбери действие:", reply_markup={
            "inline_keyboard": [[
                {"text": "Редактировать вручную", "callback_data": "edit_manual"},
                {"text": "Распознать заново", "callback_data": "edit_retry"}
            ]]
        })
    elif action == "edit_retry":
        path = CACHED_PHOTOS.get(user_id)
        if path:
            raw = send_to_assistant(user_id, {"image_path": path})
            try:
                if not raw:
                    send_message(chat_id, "⚠️ Ответ пустой — не удалось распознать изображение.")
                    return

                print("🪵 RAW before parsing:", raw)
                try:
                    item = clean_and_parse_json(raw)
                except Exception as e:
                    send_message(chat_id, f"❌ Ошибка при обработке JSON: {e}\n\nRAW: {raw}")
                    return
                PENDING_ACTIONS[user_id] = {"stage": "confirm_add", "data": item}
                send_message(chat_id, f"Категория: {item['категория']}\nОписание: {item['описание']}", reply_markup={
                    "inline_keyboard": [[
                        {"text": "Добавить в гардероб", "callback_data": "wardrobe_add"},
                        {"text": "Редактировать", "callback_data": "wardrobe_edit"}
                    ]]
                })
            except Exception as e:
                send_message(chat_id, f"❌ Ошибка при обработке JSON: {e}")

    elif action == "edit_manual":
        send_message(chat_id, "✍️ Введи новый текст описания:")

        if not isinstance(PENDING_ACTIONS.get(user_id), dict):
            PENDING_ACTIONS[user_id] = {"stage": "awaiting_manual_edit"}
        else:
            PENDING_ACTIONS[user_id]["stage"] = "awaiting_manual_edit"

# ===================== Команды =====================

COMMANDS_HELP = """
*Доступные команды:*
/start — показать это сообщение
/addwardrobe — добавить вещь в гардероб
(и просто отправляй фото или голосовые — я всё пойму 👗🎤)
""".strip()

# ===================== Обработка команд =====================

def process_command(msg, user_id, chat_id):
    username = msg["from"].get("username", "unknown")
    user_stage = PENDING_ACTIONS.get(user_id, {}).get("stage")

    # 📸 Фото
    if "photo" in msg:
        if user_stage == "awaiting_add_photo":
            file_id = msg["photo"][-1]["file_id"]
            local_path = download_file(file_id)
            CACHED_PHOTOS[user_id] = local_path

            raw = send_to_assistant(user_id, {"image_path": local_path})
            try:
                if isinstance(raw, str):
                    raw = raw.strip()
                    if raw.startswith('{') and raw.endswith('}'):
                        item = json.loads(raw)
                    else:
                        raise ValueError("Строка не выглядит как JSON.")
                elif isinstance(raw, dict):
                    item = raw
                else:
                    raise TypeError("Формат ответа не поддерживается.")

                # ✅ ВАЖНО: переносим сюда
                PENDING_ACTIONS[user_id] = {"stage": "confirm_add", "data": item}
                send_message(chat_id, f"Категория: {item['категория']}\nОписание: {item['описание']}", reply_markup={
                    "inline_keyboard": [[
                        {"text": "Добавить в гардероб", "callback_data": "wardrobe_add"},
                        {"text": "Редактировать", "callback_data": "wardrobe_edit"}
                    ]]
                })
                return

            except Exception as e:
                send_message(chat_id, f"❌ Не удалось распознать изображение: {e}")
        else:
            send_message(chat_id, "🤖 Сначала отправь команду `/addwardrobe`.")
        return

    # 🎤 Голос
    if "voice" in msg:
        file_id = msg["voice"]["file_id"]
        local_path = download_file(file_id)
        raw = send_to_assistant(user_id, {"audio_path": local_path})
        send_message(chat_id, raw)
        return

    # 💬 Текстовые команды
    text = msg.get("text", "")
    if text == "/start":
        send_message(chat_id, COMMANDS_HELP)
    elif text == "/get_logs":
        if user_id == ADMIN_USER_ID:
            send_file(chat_id, LOG_FILE)
        else:
            send_message(chat_id, "❌ У тебя нет доступа к логам.")
    elif text == "/addwardrobe":
        PENDING_ACTIONS[user_id] = {"stage": "awaiting_add_photo"}
        send_message(chat_id, "📸 Пришли фото вещи для добавления в гардероб.")
    elif user_stage == "awaiting_manual_edit":
        manual_text = text.strip()
        current = PENDING_ACTIONS.get(user_id)

        if isinstance(current, dict) and "data" in current:
            # Если категория есть — сохраняем, если нет — не продолжаем
            if "категория" not in current["data"]:
                send_message(chat_id, "⚠️ Категория утеряна. Пожалуйста, начни сначала.")
                return

            # Обновляем только описание
            current["data"]["описание"] = manual_text
            current["stage"] = "confirm_add"

            item = current["data"]
            send_message(chat_id, f"Категория: {item['категория']}\nНовое описание:\n{item['описание']}", reply_markup={
                "inline_keyboard": [[
                    {"text": "Добавить в гардероб", "callback_data": "wardrobe_add"},
                    {"text": "Редактировать", "callback_data": "wardrobe_edit"}
                ]]
            })
        else:
            send_message(chat_id, "⚠️ Что-то пошло не так при редактировании.")
    else:
        # 🔐 Сначала проверим, не в режиме ручного редактирования
        if user_stage == "awaiting_manual_edit":
            manual_text = text.strip()
            current = PENDING_ACTIONS.get(user_id)

            if isinstance(current, dict) and "data" in current:
                if "категория" not in current["data"]:
                    send_message(chat_id, "⚠️ Категория утеряна. Пожалуйста, начни сначала.")
                    return

                current["data"]["описание"] = manual_text
                current["stage"] = "confirm_add"

                item = current["data"]
                send_message(chat_id, f"Категория: {item['категория']}\nНовое описание:\n{item['описание']}",
                             reply_markup={
                                 "inline_keyboard": [[
                                     {"text": "Добавить в гардероб", "callback_data": "wardrobe_add"},
                                     {"text": "Редактировать", "callback_data": "wardrobe_edit"}
                                 ]]
                             })
            else:
                send_message(chat_id, "⚠️ Что-то пошло не так при редактировании.")
            return

        # 💬 Любое другое текстовое сообщение — идёт в GPT-ассистенту
        try:
            raw = send_to_assistant(user_id, text)
            if isinstance(raw, dict):
                formatted = f"Категория: {raw.get('категория')}\nОписание: {raw.get('описание')}"
            else:
                formatted = str(raw)
            send_message(chat_id, formatted)
        except Exception as e:
            send_message(chat_id, f"❌ Ошибка при ответе ассистента: {e}")

# ===================== Основной цикл =====================

def polling_loop():
    global OFFSET
    while True:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
        params = {"timeout": 100, "offset": OFFSET}
        response = requests.get(url, params=params)
        data = response.json()

        for update in data.get("result", []):
            OFFSET = update["update_id"] + 1

            if "message" in update:
                msg = update["message"]
                chat_id = msg["chat"]["id"]
                user_id = msg["from"]["id"]
                username = msg["from"].get("username", "unknown")

                # 📄 Логируем по содержимому сообщения
                if "text" in msg:
                    log_message(user_id, username, msg["text"], event_type="USER_MESSAGE")
                elif "photo" in msg:
                    log_message(user_id, username, "[photo]", event_type="USER_MESSAGE")
                elif "voice" in msg:
                    log_message(user_id, username, "[voice]", event_type="USER_MESSAGE")
                else:
                    log_message(user_id, username, "[unknown message]", event_type="USER_MESSAGE")

                # 🎯 Обрабатываем как команду
                process_command(msg, user_id, chat_id)

            elif "callback_query" in update:
                data = update["callback_query"]
                user_id = data["from"]["id"]
                chat_id = data["message"]["chat"]["id"]
                username = data["from"].get("username", "unknown")
                callback_data = data.get("data", "[no data]")
                log_message(user_id, username, f"[callback] {callback_data}", event_type="CALLBACK")
                process_callback(data, user_id, chat_id)

        time.sleep(1)

if __name__ == "__main__":
    print("🤖 Бот запущен в режиме polling...")
    try:
        polling_loop()
    except Exception as e:
        log_message("SYSTEM", "error", str(e), event_type="ERROR")
        raise
