# spyfall_bot.py
import asyncio
import random
import time
import logging
from typing import Dict, Any

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.error import Forbidden
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
)

# ----------------- Настройки -----------------
TOKEN = "123"  # <- Вставь сюда свой токен
MIN_PLAYERS = 4
LOBBY_SECONDS = 60
GAME_MAX_SECONDS = 15 * 60
VOTE_TIMEOUT_SECONDS = 60
SPY_GUESS_TIMEOUT = 30

# 20 локаций (как просил)
LOCATIONS = [
    "Аэропорт", "Кафе", "Пляж", "Театр", "Стадион", "Космическая станция",
    "Казино", "Подводная лодка", "Школа", "Церковь", "Поезд", "Зоопарк",
    "Больница", "Ресторан", "Кинотеатр", "Полицейский участок",
    "Парк", "Библиотека", "Отель", "Корпоративный офис"
]

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ----------------- Состояние -----------------
# Лобби (ожидание игроков): chat_id -> {players: {user_id: {name, username}}, task}
lobbies: Dict[int, Dict[str, Any]] = {}

# Игры в процессе: chat_id -> game_state
games: Dict[int, Dict[str, Any]] = {}

# Активные голосования: chat_id -> vote_state
active_votes: Dict[int, Dict[str, Any]] = {}


# ----------------- Утилиты -----------------
async def safe_send_pm(context: ContextTypes.DEFAULT_TYPE, user_id: int, text: str):
    """Попытаться отправить сообщение в ЛС; вернуть True если получилось, False если Forbidden."""
    try:
        await context.bot.send_message(chat_id=user_id, text=text)
        return True
    except Forbidden:
        return False
    except Exception as e:
        logger.exception("Ошибка при отправке PM: %s", e)
        return False


def format_players_list(players: Dict[int, Any]):
    return ", ".join(p["name"] for p in players.values())


# ----------------- ЛОББИ -----------------
async def cmd_spyfall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Создать лобби — старт набора на 60 секунд."""
    chat = update.effective_chat
    user = update.effective_user
    chat_id = chat.id

    # Лобби или игра уже идёт?
    if chat_id in games and games[chat_id].get("started"):
        await update.message.reply_text("Игра уже идёт в этом чате.")
        return
    if chat_id in lobbies:
        await update.message.reply_text("Набор уже запущен в этом чате.")
        return

    # Проверка: может ли бот писать в ЛС тому, кто запустил?
    try:
        test_ok = await safe_send_pm(context, user.id, "Я могу присылать тебе роли в ЛС — отлично! ✅")
    except Exception:
        test_ok = False

    if not test_ok:
        # сообщаем в группу, как открыть ЛС
        bot_username = context.bot.username or "this_bot"
        await update.message.reply_text(
            f"⚠️ Я не могу писать тебе в личку. Пожалуйста, открой диалог со мной: https://t.me/{bot_username} "
            "и нажми /start, затем запусти /spyfall снова."
        )
        return

    # создаём лобби
    lobbies[chat_id] = {
        "players": {},           # user_id -> {"name": str, "username": str}
        "created_by": user.id,
        "started": False,
        "task": asyncio.create_task(lobby_countdown(chat_id, context)),
    }

    await update.message.reply_text(
        f"🎲 Набор на игру Spyfall начат! Используйте в этом чате:\n"
        f"/join <имя> — чтобы присоединиться.\n"
        f"Набор идёт {LOBBY_SECONDS} секунд. Минимум игроков: {MIN_PLAYERS}.\n"
        "Важно: всем участникам нужно открыть личный чат с ботом, иначе роли им не придут."
    )


async def lobby_countdown(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Таймер лобби — через LOBBY_SECONDS запускаем игру если хватает игроков."""
    await asyncio.sleep(LOBBY_SECONDS)
    lobby = lobbies.get(chat_id)
    if not lobby:
        return

    players = lobby["players"]
    if len(players) < MIN_PLAYERS:
        # отправляем в ЛС каждого участника, что игра не состоялась
        for uid, p in players.items():
            try:
                await context.bot.send_message(uid, f"Игра не запустилась — недостаточно игроков (нужно {MIN_PLAYERS}).")
            except Forbidden:
                # если кто-то не открыл бота, пропускаем (не спамим в чат)
                logger.info("User %s didn't open PM, cannot notify about cancelled game.", uid)
        # удаляем лобби (без сообщений в общий чат по требованию)
        del lobbies[chat_id]
        logger.info("Lobby %s cancelled (not enough players).", chat_id)
        return

    # стартуем игру
    await start_game_from_lobby(chat_id, context)


# ----------------- JOIN -----------------
async def cmd_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Игрок присоединяется к текущему лобби: /join Имя"""
    chat = update.effective_chat
    user = update.effective_user
    chat_id = chat.id

    # нет лобби -> ответить в ЛС (если возможно), иначе в чат подсказать открыть ЛС
    if chat_id not in lobbies or lobbies[chat_id].get("started"):
        pm_ok = await safe_send_pm(context, user.id, "Сейчас нет активного подбора игроков в этом чате.")
        if not pm_ok:
            bot_username = context.bot.username or "this_bot"
            await update.message.reply_text(
                f"Сейчас нет активного подбора. Открой мне ЛС: https://t.me/{bot_username} и попробуй снова."
            )
        return

    if not context.args:
        await update.message.reply_text("Напишите имя: /join <имя>")
        return

    name = " ".join(context.args).strip()
    if not name:
        await update.message.reply_text("Неверное имя. /join <имя>")
        return

    players = lobbies[chat_id]["players"]
    if user.id in players:
        await update.message.reply_text("Вы уже в списке участников.")
        return

    players[user.id] = {"name": name, "username": user.username}
    await update.message.reply_text(f"✅ {name} присоединился(ась) к лобби! (Всего: {len(players)})")


# ----------------- СТАРТ ИГРЫ -----------------
async def start_game_from_lobby(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Переносим лобби в games и раздаём роли."""
    lobby = lobbies.get(chat_id)
    if not lobby:
        return

    players = lobby["players"]
    # защита: минимальная проверка
    if len(players) < MIN_PLAYERS:
        # вдруг параллельно изменилось
        for uid in players:
            try:
                await context.bot.send_message(uid, f"Игра не стартовала — недостаточно игроков (нужно {MIN_PLAYERS}).")
            except Forbidden:
                pass
        del lobbies[chat_id]
        return

    # формируем игровое состояние
    player_ids = list(players.keys())
    location = random.choice(LOCATIONS)
    spy_id = random.choice(player_ids)
    order = player_ids.copy()
    random.shuffle(order)
    # выбран, кто стартует
    current_index = random.randrange(len(order))

    game = {
        "players": players,             # user_id -> {"name", "username"}
        "location": location,
        "spy_id": spy_id,
        "order": order,                 # очередь потенциальных спрашивающих
        "current_index": current_index,
        "started": True,
        "started_at": time.time(),
        "mistakes": 0,                  # неверные обвинения жителей
        "active_vote": None,            # структура голосования (если есть)
        "lobby_task": lobbies[chat_id]["task"],
        "timer_task": None,
        "spy_exposed": False,
        "spy_guess_task": None,
    }

    games[chat_id] = game
    # удаляем лобби
    del lobbies[chat_id]

    # рассылаем роли в ЛС
    notified = []
    not_opened = []
    for uid, p in players.items():
        if uid == spy_id:
            # шпион
            text = (
                "🤫 Ты — ШПИОН!\n\n"
                "Твоя задача — выяснить локацию. В любой момент можешь раскрыть себя и угадать локацию командой:\n"
                "/guess <название локации>\n\n"
                f"Вот все возможные локации (подсказка):\n{', '.join(LOCATIONS)}"
            )
        else:
            text = (
                f"📍 Ты — житель. Локация: <b>{location}</b>\n\n"
                "Ваша цель — вычислить шпиона. Задавайте вопросы по очереди.\n\n"
                f"Вот все возможные локации (подсказка):\n{', '.join(LOCATIONS)}"
            )

        ok = await safe_send_pm(context, uid, text)
        if ok:
            notified.append(uid)
        else:
            not_opened.append(uid)

    # По требованию: если кто-то не открыл ЛС, сообщаем в общий чат, кто не получил роль
    if not_opened:
        names = ", ".join(games[chat_id]["players"][uid]["name"] for uid in not_opened)
        await context.bot.send_message(chat_id, f"⚠️ Следующие игроки не открыли диалог с ботом и роли им не отправлены: {names}")

    # Оповещение в общем чате — игра стартовала
    players_list_text = ", ".join(p["name"] for p in players.values())
    starter_id = order[current_index]
    starter_name = players[starter_id]["name"]
    await context.bot.send_message(
        chat_id,
        f"🎮 Игра началась! Участники: {players_list_text}\n"
        f"Первый, кто задаёт вопрос: <b>{starter_name}</b>.\n\n"
        "Когда придёт твой ход, нажми на кнопку и выбери, у кого спросить."
    )

    # стартуем таймер игры (15 минут)
    game["timer_task"] = asyncio.create_task(game_timer(chat_id, context))

    # пришлём кнопки первого хода
    await send_turn_keyboard(chat_id, context)


# ----------------- ОЧЕРЕДЬ ВОПРОСОВ -----------------
def build_ask_keyboard(chat_id: int):
    """Построить клавиатуру 'кого спросить' для текущей игры."""
    game = games.get(chat_id)
    if not game:
        return InlineKeyboardMarkup([[]])

    current_id = game["order"][game["current_index"]]
    keyboard = []
    for uid in game["order"]:
        if uid == current_id:
            continue
        name = game["players"][uid]["name"]
        keyboard.append([InlineKeyboardButton(name, callback_data=f"ask:{chat_id}:{uid}")])
    return InlineKeyboardMarkup(keyboard)


async def send_turn_keyboard(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Отправить в чат сообщение с кнопками 'кого спросить' для текущего игрока."""
    game = games.get(chat_id)
    if not game or not game.get("started"):
        return

    current_id = game["order"][game["current_index"]]
    current_name = game["players"][current_id]["name"]

    reply_markup = build_ask_keyboard(chat_id)
    msg = await context.bot.send_message(
        chat_id,
        f"➡️ Ход: <b>{current_name}</b>. Выберите, у кого спросить (нажмите кнопку).",
        reply_markup=reply_markup
    )
    # сохраняем id последнего сообщения с кнопками, чтобы иметь возможность редактировать/пометить
    game["last_ask_message_id"] = msg.message_id


# Обработка callback'ов (ask, pass, vote_yes и т.д.)
async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()  # быстро ответить, чтобы убрать крутилку
    data = query.data or ""
    user = query.from_user

    # форматы:
    # ask:<chat_id>:<target_id>
    # pass:<chat_id>:<target_id>
    # vote_yes:<chat_id>:<target_id>
    # cancel_vote:<chat_id>
    parts = data.split(":")

    if parts[0] == "ask" and len(parts) == 3:
        chat_id = int(parts[1]); target_id = int(parts[2])
        await handle_ask_callback(query, context, chat_id, target_id)

    elif parts[0] == "pass" and len(parts) == 3:
        chat_id = int(parts[1]); target_id = int(parts[2])
        await handle_pass_callback(query, context, chat_id, target_id)

    elif parts[0] == "vote_yes" and len(parts) == 3:
        chat_id = int(parts[1]); target_id = int(parts[2])
        await handle_vote_yes(query, context, chat_id, target_id)

    elif parts[0] == "cancel_vote" and len(parts) == 2:
        chat_id = int(parts[1])
        await handle_cancel_vote(query, context, chat_id)

    else:
        await query.edit_message_text("Неподдерживаемая операция.")


async def handle_ask_callback(query, context, chat_id: int, target_id: int):
    """Кнопка: текущий игрок выбрал, у кого спросить."""
    user = query.from_user
    game = games.get(chat_id)
    if not game or not game.get("started"):
        await query.message.reply_text("Игра не активна.")
        return

    current_id = game["order"][game["current_index"]]
    if user.id != current_id:
        await query.answer("Сейчас не твоя очередь.", show_alert=True)
        return

    if target_id not in game["players"]:
        await query.answer("Игрок не в игре.", show_alert=True)
        return

    asker_name = game["players"][current_id]["name"]
    target_name = game["players"][target_id]["name"]

    # Сообщаем в чат, кто кого спросил
    # Добавляем кнопку "Дальше" — её может нажать только тот, кого спросили (чтобы принять очередь)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Дальше ➜", callback_data=f"pass:{chat_id}:{target_id}")]])
    await context.bot.send_message(chat_id, f"🗣 <b>{asker_name}</b> спросил(а) у <b>{target_name}</b>. {target_name}, отвечай!", reply_markup=kb)


async def handle_pass_callback(query, context, chat_id: int, target_id: int):
    """Кнопка 'Дальше' — передать ход тому, у кого спросили."""
    user = query.from_user
    game = games.get(chat_id)
    if not game or not game.get("started"):
        await query.answer("Игра неактивна.", show_alert=True)
        return

    if user.id != target_id:
        await query.answer("Только тот, кого спросили, может нажать эту кнопку.", show_alert=True)
        return

    # переводим текущий индекс на позицию target_id
    try:
        new_index = game["order"].index(target_id)
    except ValueError:
        await query.answer("Игрок не в очереди.", show_alert=True)
        return

    game["current_index"] = new_index
    await context.bot.send_message(chat_id, f"🔁 Теперь ход у <b>{game['players'][target_id]['name']}</b>.")
    # отправим ему клавиатуру выбора
    await send_turn_keyboard(chat_id, context)


# ----------------- ГОЛОСОВАНИЕ -----------------
async def cmd_vote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начать голосование: /vote (reply на сообщение игрока) или /vote @username"""
    chat = update.effective_chat
    user = update.effective_user
    chat_id = chat.id

    game = games.get(chat_id)
    if not game or not game.get("started"):
        await update.message.reply_text("Игра не запущена.")
        return

    # определяем цель голосования
    target_id = None
    if update.message.reply_to_message:
        target_id = update.message.reply_to_message.from_user.id
    elif context.args:
        arg = context.args[0]
        if arg.startswith("@"):
            uname = arg[1:].lower()
            # искать по username среди участников
            for uid, p in game["players"].items():
                if p.get("username") and p["username"].lower() == uname:
                    target_id = uid
                    break
        else:
            # возможно user_id
            try:
                target_id_candidate = int(arg)
                if target_id_candidate in game["players"]:
                    target_id = target_id_candidate
            except:
                pass

    if target_id is None or target_id not in game["players"]:
        await update.message.reply_text("Не удалось определить цель голосования. Используй /vote, ответив на сообщение нужного игрока, или /vote @username")
        return

    if chat_id in active_votes:
        await update.message.reply_text("Уже идёт голосование в этом чате.")
        return

    # создаём сессию голосования
    vote = {
        "target": target_id,
        "initiator": user.id,
        "votes": set(),   # user_ids, голосующие "за"
        "message_id": None,
        "end_task": None,
    }
    active_votes[chat_id] = vote

    target_name = game["players"][target_id]["name"]
    # клавиатура — кнопка "Я за"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Я за ✅", callback_data=f"vote_yes:{chat_id}:{target_id}")]])
    msg = await context.bot.send_message(chat_id, f"🗳️ Голосование за шпиона: <b>{target_name}</b>\nНажмите «Я за», если вы за обвинение.", reply_markup=kb)
    vote["message_id"] = msg.message_id

    # запустить таймаут голосования
    vote["end_task"] = asyncio.create_task(vote_timeout(chat_id, context))
    await update.message.reply_text("Голосование начато.")


async def handle_vote_yes(query, context, chat_id: int, target_id: int):
    """Обработка клика 'Я за' по голосованию."""
    user = query.from_user
    gv = active_votes.get(chat_id)
    game = games.get(chat_id)
    if not gv or not game:
        await query.answer("Нет активного голосования.", show_alert=True)
        return

    if gv["target"] != target_id:
        await query.answer("Это голосование уже не про этого игрока.", show_alert=True)
        return

    if user.id not in game["players"]:
        await query.answer("Вы не участвуете в этой игре.", show_alert=True)
        return

    gv["votes"].add(user.id)
    # обновляем число проголосовавших прямо в сообщении
    count = len(gv["votes"]); total = len(game["players"])
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=gv["message_id"],
            text=f"🗳️ Голосование за: <b>{game['players'][target_id]['name']}</b>\n"
                 f"Голосов за: {count}/{total}\n(Нужно >50% чтобы обвинение прошло)",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Я за ✅", callback_data=f"vote_yes:{chat_id}:{target_id}")]])
        )
    except Exception:
        pass

    # проверим, набрали ли большинство
    if count > total / 2:
        # подтверждённое обвинение
        # отменим таймер голосования
        task = gv.get("end_task")
        if task and not task.done():
            task.cancel()
        # обработать результат обвинения
        await finalize_vote(chat_id, context, target_id)
    else:
        await query.answer(f"Голос учтён ({count}/{total}).")


async def vote_timeout(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Если голосование не завершилось за VOTE_TIMEOUT_SECONDS — просто завершаем с ничьей."""
    try:
        await asyncio.sleep(VOTE_TIMEOUT_SECONDS)
        gv = active_votes.get(chat_id)
        game = games.get(chat_id)
        if not gv or not game:
            return
        # по таймауту — ничего не меняется (голосование провалено)
        await context.bot.send_message(chat_id, "⏱ Голосование завершилось без большинства — обвинение не прошло.")
        del active_votes[chat_id]
    except asyncio.CancelledError:
        return


async def finalize_vote(chat_id: int, context: ContextTypes.DEFAULT_TYPE, target_id: int):
    """Обвинение прошло (мнение большинства)."""
    gv = active_votes.get(chat_id)
    game = games.get(chat_id)
    if not gv or not game:
        return

    # очистка голосования
    try:
        del active_votes[chat_id]
    except KeyError:
        pass

    target_name = game["players"][target_id]["name"]
    spy_id = game["spy_id"]
    if target_id == spy_id:
        # жители вычислили шпиона — шпион получает шанс угадать локацию
        game["spy_exposed"] = True
        await context.bot.send_message(chat_id, f"🔔 Жители вычислили шпиона: <b>{target_name}</b>.\n"
                                                "Шпиону даётся шанс угадать локацию. Шпион, используй команду:\n"
                                                "/guess <название локации>\n"
                                                f"У тебя {SPY_GUESS_TIMEOUT} секунд.")
        # стартуем таймер на угадывание шпиона
        game["spy_guess_task"] = asyncio.create_task(spy_guess_timeout(chat_id, context))
    else:
        # ошибочное обвинение
        game["mistakes"] += 1
        await context.bot.send_message(chat_id, f"❌ Это был не шпион. Ошибок у жителей: {game['mistakes']}/2.")
        if game["mistakes"] >= 2:
            # шпион побеждает
            await end_game(chat_id, context, winner="spy", reason="Жители дважды ошиблись с обвинением.")
        else:
            # продолжаем игру; не меняем очередь
            await context.bot.send_message(chat_id, "Игра продолжается. Вернёмся к очереди вопросов.")
            await send_turn_keyboard(chat_id, context)


async def handle_cancel_vote(query, context, chat_id: int):
    """(опционально) отмена голосования."""
    if chat_id in active_votes:
        try:
            gv = active_votes[chat_id]
            if gv.get("end_task") and not gv["end_task"].done():
                gv["end_task"].cancel()
        except Exception:
            pass
        del active_votes[chat_id]
    await query.message.edit_text("Голосование отменено.")


# ----------------- УГАДЫВАНИЕ (spy) -----------------
async def cmd_guess(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /guess <локация> — шпион пытается угадать локацию."""
    chat = update.effective_chat
    user = update.effective_user
    chat_id = chat.id if chat else None
    text_args = context.args

    # Определим, в какой игре этот пользователь участвует (он мог написать в ЛС или в общем чате)
    # пробежимся по games и найдём ту, где user присутствует и started==True
    user_game_chat = None
    for cid, g in games.items():
        if g.get("started") and user.id in g["players"]:
            user_game_chat = cid
            break

    if user_game_chat is None:
        await update.message.reply_text("Ты не участвуешь в активной игре.")
        return

    game = games[user_game_chat]
    if user.id != game["spy_id"]:
        await update.message.reply_text("Угадать локацию может только шпион.")
        return

    if not text_args:
        await update.message.reply_text("Использование: /guess <название локации>")
        return

    guess = " ".join(text_args).strip().lower()
    real = game["location"].strip().lower()

    if guess == real:
        await context.bot.send_message(user_game_chat, f"🏆 Шпион <b>{game['players'][user.id]['name']}</b> угадал локацию — <b>{game['location']}</b>. Шпион побеждает!")
        await end_game(user_game_chat, context, winner="spy", reason="Шпион угадал локацию.")
    else:
        await context.bot.send_message(user_game_chat, f"✅ Шпион ошибся с угадыванием (назвал: {guess}). Победили жители!")
        await end_game(user_game_chat, context, winner="residents", reason="Шпион ошибся при угадывании.")


async def spy_guess_timeout(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Шпиону дали время на угадывание после разоблачения; если таймаут — жители выигрывают."""
    try:
        await asyncio.sleep(SPY_GUESS_TIMEOUT)
        game = games.get(chat_id)
        if not game:
            return
        if game.get("spy_exposed") and game.get("started"):
            await context.bot.send_message(chat_id, "⏱ Время шпиона вышло — он не успел назвать локацию. Победили жители!")
            await end_game(chat_id, context, winner="residents", reason="Шпион не успел назвать локацию после разоблачения.")
    except asyncio.CancelledError:
        return


# ----------------- ТАЙМЕР И ЗАВЕРШЕНИЕ -----------------
async def game_timer(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Таймер максимальной продолжительности игры (15 минут)."""
    try:
        await asyncio.sleep(GAME_MAX_SECONDS)
        game = games.get(chat_id)
        if not game or not game.get("started"):
            return
        await context.bot.send_message(chat_id, "⏳ Время игры вышло — жители побеждают (шпион не успел).")
        await end_game(chat_id, context, winner="residents", reason="Время вышло.")
    except asyncio.CancelledError:
        return


async def end_game(chat_id: int, context: ContextTypes.DEFAULT_TYPE, winner: str, reason: str):
    """Завершить игру: очистить задачи, сообщить результат и раскрыть шпиона и локацию."""
    game = games.get(chat_id)
    if not game:
        return

    spy_id = game["spy_id"]
    spy_name = game["players"][spy_id]["name"]
    location = game["location"]

    if winner == "spy":
        result_text = f"🏆 Победил шпион: <b>{spy_name}</b>.\nПричина: {reason}"
    else:
        result_text = f"🏆 Победили жители!\nПричина: {reason}\nШпион: <b>{spy_name}</b>."

    result_text += f"\n\nЛокация: <b>{location}</b>."

    await context.bot.send_message(chat_id, result_text)

    # отменяем задачи
    try:
        if game.get("timer_task") and not game["timer_task"].done():
            game["timer_task"].cancel()
    except Exception:
        pass
    try:
        if game.get("spy_guess_task") and not game["spy_guess_task"].done():
            game["spy_guess_task"].cancel()
    except Exception:
        pass

    # удаляем игру
    try:
        del games[chat_id]
    except KeyError:
        pass


# ----------------- ПРОЧИЕ КОМАНДЫ -----------------
async def cmd_players(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/players — показать список игроков в текущей игре (если есть)."""
    chat = update.effective_chat
    game = games.get(chat.id)
    if not game:
        await update.message.reply_text("Нет активной игры в этом чате.")
        return
    text = "Игроки:\n" + "\n".join(f"- {p['name']}" for p in game["players"].values())
    await update.message.reply_text(text)


async def cmd_leave(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/leave — выйти из лобби (до старта)."""
    chat = update.effective_chat
    user = update.effective_user
    chat_id = chat.id

    if chat_id in lobbies and user.id in lobbies[chat_id]["players"]:
        del lobbies[chat_id]["players"][user.id]
        await update.message.reply_text("Ты покинул лобби.")
        return
    await update.message.reply_text("Ты не в лобби или оно уже стартовало.")


# ----------------- Запуск бота -----------------
def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("spyfall", cmd_spyfall))
    app.add_handler(CommandHandler("join", cmd_join))
    app.add_handler(CommandHandler("vote", cmd_vote))
    app.add_handler(CommandHandler("guess", cmd_guess))
    app.add_handler(CommandHandler("players", cmd_players))
    app.add_handler(CommandHandler("leave", cmd_leave))

    # общий роутер для callback'ов (ask/pass/vote_yes ...)
    app.add_handler(CallbackQueryHandler(callback_router))

    print("Бот запущен...")
    app.run_polling()


if __name__ == "__main__":
    main()
