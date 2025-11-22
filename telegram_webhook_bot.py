"""
Переписанный файл бота (async, webhook + Flask).

Инструкции:
1) Создайте файл .env рядом с этим скриптом с переменными:
   BOT_TOKEN=...
   PROVIDER_TOKEN=...   # если используете Telegram Payments (Stars)
   WEBHOOK_URL=https://yourdomain.com/webhook
   MANAGER_ID=878251704
   CHANNEL_INVITE_LINK=https://t.me/yourchannel

2) Положите папку photos с photo_hello.JPG, photo_payments.JPG, photo_thanks.JPG рядом со скриптом.
3) Запустите: python telegram_webhook_bot.py

Описание:
- Flask принимает входящие Update от Telegram (webhook).
- Один Application используется для обработки обновлений (нет дублирования).
- Безопаснее: токен берётся из переменных окружения, не хранится в коде.
- Файлы читаются асинхронно (aiofiles -> BytesIO) чтобы не блокировать event loop.

"""

import os
import logging
import io
import asyncio
from typing import Dict, Any

import aiofiles
from flask import Flask, request, abort
from dotenv import load_dotenv

from telegram import (
    Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup,
    LabeledPrice, InputMediaPhoto, InputFile
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    PreCheckoutQueryHandler, ContextTypes, filters
)

# ------ Настройка окружения ------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
PROVIDER_TOKEN = os.getenv("PROVIDER_TOKEN", "")  # если есть
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
MANAGER_ID = int(os.getenv("MANAGER_ID", "0"))
CHANNEL_INVITE_LINK = os.getenv("CHANNEL_INVITE_LINK", "")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set in environment")

# ------ Логирование ------
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ------ Flask-приложение ------
flask_app = Flask(__name__)

# ------ Telegram Application ------
app_telegram = Application.builder().token(BOT_TOKEN).build()
bot = Bot(token=BOT_TOKEN)

# ------ Внутренние структуры ------
current_inline_message: Dict[int, Any] = {}  # ключи — user.id
users_query: Dict[int, Any] = {}
admin_ids = {MANAGER_ID} if MANAGER_ID != 0 else set()
users_subs_list = []
users_waitingcryptocheck = set()

# Константы
ONELINK = CHANNEL_INVITE_LINK or "https://t.me/yourchannel"
CHANNEL_ID = os.getenv("CHANNEL_ID", "-1000000000000")

# ------ Вспомогательные функции для асинхронного чтения файлов ------
async def read_image_bytes(path: str) -> io.BytesIO:
    """Асинхронно читает файл и возвращает BytesIO (для send_photo)"""
    buf = io.BytesIO()
    async with aiofiles.open(path, "rb") as f:
        content = await f.read()
        buf.write(content)
    buf.seek(0)
    return buf

# ------ Обработчики Telegram ------

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat_id = update.effective_chat.id

    # Сохраняем сообщение (по id пользователя)
    if update.message:
        current_inline_message[user.id] = update.message

    keyboard = [[InlineKeyboardButton("Да, конечно💕 / Yes, of course💕", callback_data="siski_gopay")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    text_hello = (
        f"👋, {user.first_name}!\n\n"
        "💗В моем личном канале вы найдете все, о чем всегда мечтали: мои самые горячие фото и видео 18+ 😈💋\n"
        "После оплаты вы сможете общаться со мной напрямую и получать эксклюзивный контент...\n\n"
        "💗On my personal channel, you will find everything you have always dreamed of: my hottest photos and videos 18+\n"
        "💗Ready?😉"
    )

    photo_path = os.path.join("photos", "photo_hello.JPG")
    try:
        photo_buf = await read_image_bytes(photo_path)
        await context.bot.send_photo(chat_id=chat_id, photo=photo_buf, caption=text_hello, reply_markup=reply_markup)
    except FileNotFoundError:
        logger.exception("photo_hello.JPG not found")
        await context.bot.send_message(chat_id=chat_id, text=text_hello, reply_markup=reply_markup)


async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    data = query.data or ""

    # Убираем спиннер у кнопки
    await query.answer()

    # Если раньше сохраняли сообщения, делаем безопасный доступ
    try:
        message = current_inline_message.get(user.id)
    except Exception:
        message = None

    # Ожидание подтверждения по крипте — если пользователь открыл чек, убираем из waiting
    if data != "siski_checkcryptopay" and user.id in users_waitingcryptocheck:
        users_waitingcryptocheck.discard(user.id)

    # Обработаем админские колбэки: формат admin_cryptopay_YES:<user_id>
    if data.startswith("admin_cryptopay_"):
        # пример: admin_cryptopay_YES:123456
        parts = data.split(":")
        if len(parts) == 2 and str(user.id) in map(str, admin_ids):
            action_part = parts[0]  # admin_cryptopay_YES
            target_user_id = parts[1]
            if action_part.endswith("_YES"):
                # Добавляем подписчика
                users_subs_list.append(int(target_user_id))
                await query.edit_message_caption(f"Пользователь с id {target_user_id} добавлен в список подписчиков")
                # Отправим приватную ссылку пользователю
                keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("💌", url=ONELINK)]])
                try:
                    photo_buf = await read_image_bytes(os.path.join("photos", "photo_thanks.JPG"))
                    await context.bot.send_photo(chat_id=int(target_user_id), photo=photo_buf, caption="💗Добро пожаловать💗\n\n💗Welcome💗", reply_markup=keyboard)
                except Exception:
                    logger.exception("Не удалось прислать сообщение подписчику")
            elif action_part.endswith("_NO"):
                await query.edit_message_caption(f"Пользователь с id {target_user_id} не был добавлен в список подписчиков")
                keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔁", callback_data="siski_gocryptopayagain")]])
                await context.bot.send_message(chat_id=int(target_user_id), text="Не пытайся меня обмануть 😜\n\nDon't try to trick me. 😜", reply_markup=keyboard)
        else:
            await query.edit_message_text("Нет прав на выполнение этого действия или неверный формат данных")
        return

    # Навигация по кнопкам (обычный пользователь)
    if data == "start":
        # просто вызываем стартовую логику
        await start_handler(update, context)
        return

    if data == "siski_gopay":
        keyboard = [
            [InlineKeyboardButton("TG Stars⭐️ (Apple Pay-Google Pay)", callback_data="siski_gostarpay")],
            [InlineKeyboardButton("(USDT TRC20)💎", callback_data="siski_gocryptopay")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        try:
            photo_buf = await read_image_bytes(os.path.join("photos", "photo_payments.JPG"))
            await query.edit_message_media(media=InputMediaPhoto(photo_buf, caption="💲Выберите способ оплаты\n\n💲Choose a payment method"), reply_markup=reply_markup)
        except Exception:
            await query.edit_message_caption("💲Выберите способ оплаты\n\n💲Choose a payment method", reply_markup=reply_markup)
        return

    if data == "siski_gocryptopay":
        keyboard = [[InlineKeyboardButton("☑", callback_data="siski_checkcryptopay")], [InlineKeyboardButton("🔙", callback_data="siski_gopay")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_caption("35 USDT TRC20\nTThh21cL3Thfv51hV2yeg1B5o9WSi2Vu54", reply_markup=reply_markup)
        return

    if data == "siski_checkcryptopay":
        keyboard = [[InlineKeyboardButton("❌", callback_data="siski_gocryptopay")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_caption("Отправьте чек об оплате сюда👇🧾\n\nSend your payment receipt here👇🧾", reply_markup=reply_markup)
        users_waitingcryptocheck.add(user.id)
        return

    if data == "siski_gostarpay":
        # Создаём invoice. Нужно, чтобы message, с которого вызвали, существовало
        user_message = current_inline_message.get(user.id)
        if not user_message:
            await query.answer("Не удалось найти исходное сообщение для выставления счёта", show_alert=True)
            return

        await query.edit_message_caption("💕")

        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("1500 ⭐", pay=True)]])
        prices = [LabeledPrice(label="XTR", amount=1500)]

        try:
            await user_message.reply_invoice(
                title="💕💕💕",
                description="🔽🔽🔽",
                prices=prices,
                provider_token=PROVIDER_TOKEN or "",
                payload="siski",
                currency="XTR",
                reply_markup=keyboard,
            )
        except Exception:
            logger.exception("Ошибка при создании invoice: проверьте PROVIDER_TOKEN и права бота на платежи")
        return

    if data == "siski_gocryptopayagain":
        reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("☑", callback_data="siski_checkcryptopay")], [InlineKeyboardButton("🔙", callback_data="siski_gopay")]])
        try:
            photo_buf = await read_image_bytes(os.path.join("photos", "photo_payments.JPG"))
            sent = await context.bot.send_photo(chat_id=user.id, photo=photo_buf, caption="35 USDT TRC20\nTThh21cL3Thfv51hV2yeg1B5o9WSi2Vu54", reply_markup=reply_markup)
            current_inline_message[user.id] = sent
        except Exception:
            await context.bot.send_message(chat_id=user.id, text="35 USDT TRC20\nTThh21cL3Thfv51hV2yeg1B5o9WSi2Vu54", reply_markup=reply_markup)
        return

    # Неизвестный callback
    await query.answer()


async def photo_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    user = update.effective_user

    if user.id not in users_waitingcryptocheck:
        await message.reply_text("Пожалуйста, используйте соответствующие кнопки для отправки чека.")
        return

    if not message.photo:
        await message.reply_text("Вы не отправили чек, пожалуйста, отправьте его еще раз😊")
        return

    # берём самый большой вариант фото
    photo = message.photo[-1]
    file_id = photo.file_id

    # пересылаем менеджеру
    caption = f"Чек от пользователя: @{user.username or user.id} (id {user.id})"

    keyboard = [
        [InlineKeyboardButton("Подтвердить оплату", callback_data=f"admin_cryptopay_YES:{user.id}" )],
        [InlineKeyboardButton("Отклонить оплату", callback_data=f"admin_cryptopay_NO:{user.id}")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        await context.bot.send_photo(chat_id=MANAGER_ID, photo=file_id, caption=caption, reply_markup=reply_markup)
        await message.reply_text("Ожидание подтверждения оплаты ⌛️\n\nWaiting for payment confirmation ⌛️")
        logger.info(f"Чек от {user.id} переслан менеджеру {MANAGER_ID}")
    except Exception:
        logger.exception("Ошибка при пересылке чека")
        await message.reply_text("Ошибка при пересылке чека. Попробуйте ещё раз.")


async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pre = update.pre_checkout_query
    # пример проверки суммы (логика зависит от твоих требований)
    if pre.total_amount <= 0:
        await context.bot.answer_pre_checkout_query(pre.id, ok=False, error_message="Неверная сумма заказа")
    else:
        await context.bot.answer_pre_checkout_query(pre.id, ok=True)


async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user = update.effective_user

    try:
        photo_buf = await read_image_bytes(os.path.join("photos", "photo_thanks.JPG"))
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("💌", url=ONELINK)]])
        await context.bot.send_photo(chat_id=chat_id, photo=photo_buf, caption="💗Добро пожаловать💗\n\n💗Welcome💗", reply_markup=keyboard)

        # Убираем подпись у inline-сообщения, если была
        saved_query = users_query.get(user.id)
        if saved_query and hasattr(saved_query, 'edit_message_caption'):
            try:
                await saved_query.edit_message_caption(" ")
            except Exception:
                pass

        users_subs_list.append(user.id)
    except Exception:
        logger.exception("Ошибка в successful_payment_handler")


async def fallback_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("❣", callback_data="start")]])
    await context.bot.send_message(chat_id=chat_id, text="Неверная команда.\n\nWrong command.", reply_markup=keyboard)


# ------ Регистрация обработчиков в Application ------
app_telegram.add_handler(CommandHandler("start", start_handler))
app_telegram.add_handler(CallbackQueryHandler(callback_query_handler))
app_telegram.add_handler(PreCheckoutQueryHandler(precheckout_handler))
app_telegram.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))
app_telegram.add_handler(MessageHandler(filters.PHOTO & (~filters.COMMAND), photo_message_handler))
app_telegram.add_handler(MessageHandler(filters.ALL & (~filters.COMMAND), fallback_message_handler))

# Сохраняем последний update.user -> query (нужна для успешной оплаты)
# Для более точного поведения можно добавлять middleware. Здесь упрощённо.

# ------ Flask routes для webhook ------
@flask_app.route('/webhook', methods=['POST'])
async def webhook():
    if request.headers.get('content-type') != 'application/json':
        return ('Wrong content type', 400)

    data = request.get_json(force=True)
    try:
        update = Update.de_json(data, app_telegram.bot)
        # process_update — async
        await app_telegram.process_update(update)
        return ('OK', 200)
    except Exception:
        logger.exception('Error while processing update')
        return ('Internal Server Error', 500)


@flask_app.route('/setwebhook', methods=['GET'])
async def set_webhook():
    if not WEBHOOK_URL:
        return ('WEBHOOK_URL not configured', 500)

    try:
        await app_telegram.bot.set_webhook(url=WEBHOOK_URL)
        return ('Webhook set!', 200)
    except Exception:
        logger.exception('Failed to set webhook')
        return ('Failed to set webhook', 500)


# ------ Запуск приложения ------
if __name__ == '__main__':
    # Инициализация телеграм-приложения
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(app_telegram.initialize())
    except Exception:
        logger.exception('Failed to initialize telegram application (this may be fine for webhooks)')

    # Flask должен слушать порт из окружения
    port = int(os.environ.get("PORT", 5000))
    flask_app.run(host='0.0.0.0', port=port, threaded=True)

