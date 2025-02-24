import re
import string
import sqlite3
import threading
import os

from flask import Flask
from flask_admin import Admin
from flask_admin.contrib.sqla import ModelView
from flask_sqlalchemy import SQLAlchemy
from flask_basicauth import BasicAuth
from waitress import serve

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from rapidfuzz import process, fuzz

########################################
# ЗАДАЁМ НАСТРОЙКИ ДЛЯ ОПЕРАТОРОВ
########################################

ADMIN_CHAT_ID = -4713749199  # <-- Замените на ID чата с администраторами
# Словари для отслеживания диалога с оператором:
# pending_operator: ключ – user_id, значение – None (если запрос ещё не отправлен) или ID сообщения, отправленного в чат админов.
pending_operator = {}
# Обратное соответствие: ключ – ID сообщения в админ-чате, значение – user_id
operator_admin_to_user = {}

########################################
# Настройка веб-панели (Flask + Admin)
########################################

# Инициализация Flask-приложения (база в папке database, расположенной в корне)
web_app = Flask(__name__)
web_app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
web_app.secret_key = 'supersecretkey'  # Замените на более надёжное значение

# Настройка базовой аутентификации для админ-панели
web_app.config['BASIC_AUTH_USERNAME'] = 'admin'
web_app.config['BASIC_AUTH_PASSWORD'] = 'faqIHf4u8sdfj'
web_app.config['BASIC_AUTH_FORCE'] = True  # Принудительно защищаем все маршруты

basic_auth = BasicAuth(web_app)

# Папка для базы данных (находится в корне проекта)
db_folder = os.path.join(os.getcwd(), "database")
os.makedirs(db_folder, exist_ok=True)
db_file = os.path.join(db_folder, "faq_database.db")
web_app.config['SQLALCHEMY_DATABASE_URI'] = f"sqlite:///{db_file}"

# Инициализация SQLAlchemy
db = SQLAlchemy(web_app)

# Модель FAQ для административной панели
class FAQ(db.Model):
    __tablename__ = 'faq'
    id = db.Column(db.Integer, primary_key=True)
    question = db.Column(db.Text, unique=True, nullable=False)
    answer = db.Column(db.Text, nullable=False)

    def __str__(self):
        return self.question

# Создание таблиц, если они отсутствуют
with web_app.app_context():
    db.create_all()

# Кастомное представление модели FAQ (все надписи переведены на русский, фильтры отключены)
class FAQModelView(ModelView):
    create_modal = False  # используем «старый» способ добавления
    edit_modal = False    # используем «старый» способ редактирования
    can_view_details = True

    column_labels = {
        'id': 'ID',
        'question': 'Вопрос',
        'answer': 'Ответ'
    }
    column_searchable_list = ['question', 'answer']
    column_sortable_list = ['id', 'question']
    # Фильтры не используются

    page_size = 20

# Инициализация Flask-Admin (админка доступна по адресу http://zrkbot.ru)
admin = Admin(web_app, name='Панель администратора FAQ', template_mode='bootstrap3')
admin.add_view(FAQModelView(FAQ, db.session, name='FAQ'))

# Функция для запуска production‑WSGI сервера с помощью Waitress
def run_flask():
    serve(web_app, host="0.0.0.0", port=5000)

########################################
# Функции для обработки вопросов FAQ
########################################

# Нормализация текста для поиска (без учета регистра)
def search_normalize(text):
    translator = str.maketrans('', '', string.punctuation)
    return text.translate(translator).lower().strip()

# Нормализация отображаемого текста (удаление лишних пробелов)
def display_normalize(text):
    return text.strip()

# Извлечение кодов из текста (слова, содержащие буквы и цифры)
def extract_candidate_codes(text):
    pattern = r'\b(?=\w*[A-Za-z])(?=\w*\d)\w+\b'
    return re.findall(pattern, text)

# Проверка, является ли ответ ссылкой на .mp4
def is_mp4_link(text):
    return bool(re.fullmatch(r'https?://\S+\.mp4', text.strip()))

# Загрузка FAQ-данных из базы
def load_faq_data():
    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT question, answer FROM faq")
    except sqlite3.OperationalError:
        conn.close()
        return []
    data = [(display_normalize(q), a) for q, a in cursor.fetchall()]
    conn.close()
    return data

# Подготовка структур данных для поиска
def get_faq_mappings():
    faq_data = load_faq_data()
    faq_dict = {q: a for q, a in faq_data}
    norm_to_orig = {search_normalize(q): q for q in faq_dict.keys()}
    normalized_questions = list(norm_to_orig.keys())
    return faq_dict, norm_to_orig, normalized_questions

# Поиск по FAQ (без учета регистра)
def search_faq(query, threshold=75):
    faq_dict, norm_to_orig, normalized_questions = get_faq_mappings()
    query_norm = search_normalize(query)
    print(f"🔍 Ищу (без учета регистра): '{query_norm}'")

    # Точное совпадение
    if query_norm in norm_to_orig:
        orig_q = norm_to_orig[query_norm]
        print(f"✅ Нашел точное совпадение: '{orig_q}'")
        return orig_q, faq_dict[orig_q], 100

    # Поиск по вхождению
    for norm_q in normalized_questions:
        if query_norm in norm_q or norm_q in query_norm:
            orig_q = norm_to_orig[norm_q]
            print(f"✅ Нашел совпадение по вхождению: '{orig_q}'")
            return orig_q, faq_dict[orig_q], 100

    # Нечеткий поиск
    best_match = process.extractOne(query_norm, normalized_questions, scorer=fuzz.partial_ratio, score_cutoff=threshold)
    if best_match:
        matched_norm, score = best_match[:2]
        orig_q = norm_to_orig[matched_norm]
        print(f"🧐 Нашел похожий вопрос: '{orig_q}' (совпадение {score}%)")
        return orig_q, faq_dict[orig_q], score

    # Поиск по извлечённым кодам
    candidate_codes = extract_candidate_codes(query)
    if candidate_codes:
        print(f"🔍 Извлеченные коды: {candidate_codes}")
        for code in candidate_codes:
            code_norm = search_normalize(code)
            for norm_q in normalized_questions:
                if code_norm in norm_q:
                    orig_q = norm_to_orig[norm_q]
                    print(f"✅ Нашел по коду '{code}': '{orig_q}'")
                    return orig_q, faq_dict[orig_q], 100
            best_match = process.extractOne(code_norm, normalized_questions, scorer=fuzz.partial_ratio,
                                            score_cutoff=threshold)
            if best_match:
                matched_norm, score = best_match[:2]
                orig_q = norm_to_orig[matched_norm]
                print(f"🧐 Нашел по коду '{code}': '{orig_q}' (совпадение {score}%)")
                return orig_q, faq_dict[orig_q], score

    print("❌ Ничего не найдено")
    return None, None, None

########################################
# Telegram-бот на Pyrogram
########################################

app_bot = Client(
    "faq_bot",
    api_id=22806579,
    api_hash="e455bdf231f1bcdba4bcf98423ddcae0",
    bot_token="7790967506:AAGzDLB6apodMF_RTZQsHeCmZ_6L0GDch4Y"
)

# Обработчик вызова оператора по callback (при нажатии на кнопку)
@app_bot.on_callback_query(filters.regex("call_operator"))
async def call_operator_callback(client, callback_query):
    user_id = callback_query.from_user.id
    if user_id in pending_operator:
        await callback_query.answer("Обращение уже отправлено. Ожидайте ответа.", show_alert=True)
        return
    # Регистрируем запрос: значение None означает, что оператор вызван, но сообщение еще не получено
    pending_operator[user_id] = None
    await callback_query.answer("Оператор вызван. Пожалуйста, еще раз подробно опишите проблему и приложите скриншот (если нужно)", show_alert=True)
    await client.send_message(callback_query.from_user.id,
                              "Оператор вызван. Пожалуйста, еще раз подробно опишите проблему и приложите скриншот (если нужно)")

# Обработчик сообщений от пользователей, находящихся в диалоге с оператором.
# Группа 0 – имеет приоритет и обрабатывает обращения оператору.
@app_bot.on_message(filters.private & ~filters.command("start"), group=0)
async def operator_request_handler(client, message):
    user_id = message.chat.id
    if user_id in pending_operator:
        if pending_operator[user_id] is None:
            # Это сообщение оператора (текст или скриншот)
            forwarded_msg = await client.forward_messages(
                chat_id=ADMIN_CHAT_ID,
                from_chat_id=user_id,
                message_ids=message.id
            )
            pending_operator[user_id] = forwarded_msg.id
            operator_admin_to_user[forwarded_msg.id] = user_id
            await client.send_message(user_id, "Ваше обращение отправлено операторам. Ожидайте ответа.")
        else:
            await client.send_message(user_id, "Ваше обращение уже отправлено. Пожалуйста, ожидайте ответа от оператора.")
        return  # Не обрабатываем сообщение как обычный запрос

# Основной обработчик FAQ-запросов.
# Группа 1 – обрабатывает сообщения, если пользователь не находится в диалоге с оператором.
@app_bot.on_message(filters.text & ~filters.command("start") & filters.private, group=1)
async def handle_question(client, message):
    user_id = message.chat.id
    # Если пользователь уже вызвал оператора, не обрабатываем как FAQ-запрос
    if user_id in pending_operator:
        return

    user_question = message.text
    chat_id = message.chat.id

    matched_question, answer, similarity = search_faq(user_question)

    if answer:
        # Если ответ – ссылка на видео .mp4
        if is_mp4_link(answer):
            try:
                await client.send_video(chat_id, answer, caption="🎥 Видео по вашему запросу:")
                print("✅ Видео успешно отправлено")
                return
            except Exception as e:
                print(f"⚠ Ошибка при отправке видео: {e}")
                await client.send_message(chat_id, f"🔗 Видео по вашему запросу: {answer}")
                return

        # Отправка текстового ответа с кнопкой "Вызвать оператора"
        await client.send_message(
            chat_id,
            f"🧐 Найден ответ на ваш вопрос:\n\n"
            f"**Ответ:** {answer}\n"
            "Если это не то, что вы искали, вы можете вызвать оператора, нажав кнопку ниже.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Вызвать оператора", callback_data="call_operator")]
            ])
        )
    else:
        await client.send_message(
            chat_id,
            "❌ Извините, я не нашёл ответ на ваш вопрос.\n"
            "Пожалуйста, уточните запрос или нажмите кнопку, чтобы вызвать оператора.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Вызвать оператора", callback_data="call_operator")]
            ])
        )

@app_bot.on_message(filters.command("start") & filters.private)
async def start(client, message):
    await message.reply_text(
        "👋 Привет! Я бот для ответов на вопросы.\n\n"
        "Просто напишите свой вопрос, и я постараюсь вам помочь.\n"
        "Например: напишите \"Ошибка APK36651065\" или просто код \"APK36651065\".\n\n"
        "Если вам не подходит автоматический ответ, нажмите кнопку «Вызвать оператора»."
    )

# Обработчик ответов операторов.
# Если администратор в ADMIN_CHAT_ID отвечает (reply) на сообщение, пересланное ботом, ответ отправляется пользователю и диалог закрывается.
@app_bot.on_message(filters.chat(ADMIN_CHAT_ID) & filters.reply)
async def operator_reply_handler(client, message):
    replied_msg = message.reply_to_message
    if replied_msg and replied_msg.id in operator_admin_to_user:
        user_id = operator_admin_to_user[replied_msg.id]
        if message.photo:
            await client.send_photo(user_id, message.photo.file_id, caption=message.caption or "Ответ оператора:")
        else:
            await client.send_message(user_id, f"Ответ оператора:\n{message.text}")
        # После ответа закрываем диалог
        await client.send_message(ADMIN_CHAT_ID, f"Диалог с пользователем {user_id} закрыт.")
        del pending_operator[user_id]
        del operator_admin_to_user[replied_msg.id]

########################################
# Запуск бота и веб-панели
########################################

if __name__ == "__main__":
    # Запускаем веб-панель в отдельном потоке (админка доступна по адресу http://zrkbot.ru)
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    # Запускаем Telegram-бота
    app_bot.run()
