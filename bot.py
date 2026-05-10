# -*- coding: utf-8 -*-
import logging
from datetime import datetime

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

import gspread
from google.oauth2.service_account import Credentials

# ---- CONFIG ----
BOT_TOKEN = "ВСТАВЬТЕ_ТОКЕН_БОТА"
GOOGLE_SHEET_ID = "ВСТАВЬТЕ_ID_ТАБЛИЦЫ"
CREDENTIALS_FILE = "credentials.json"

# ---- STATES ----
SELECT_PROJECT, CONFIRM_START, WORKING, COMMENTING = range(4)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


def get_client():
    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
    return gspread.authorize(creds)


def get_user_projects(tg_id: int) -> list:
    client = get_client()
    sheet = client.open_by_key(GOOGLE_SHEET_ID).worksheet("Проекты")
    rows = sheet.get_all_values()

    projects = []
    for row in rows[1:]:
        if len(row) < 9:
            continue
        project_id   = row[0].strip()
        project_name = row[1].strip()
        status       = row[2].strip()
        client_name  = row[3].strip()
        company_name = row[4].strip()
        expert_name  = row[5].strip()
        expert_tg_id = row[6].strip()
        pm_name      = row[7].strip()
        pm_tg_id     = row[8].strip()

        if status == "Активный" and expert_tg_id == str(tg_id):
            projects.append({
                "name": project_name,
                "project_id": project_id,
                "client": client_name,
                "company": company_name,
                "expert_name": expert_name,
                "pm_name": pm_name,
                "pm_tg_id": pm_tg_id,
            })

    return projects


def ensure_timesheet_headers(sheet):
    headers = [
        "Дата", "ID проекта", "Проект", "Клиент", "Компания",
        "Эксперт", "TG ID", "Начало", "Конец", "Длительность (мин)", "Комментарий"
    ]
    first_row = sheet.row_values(1)
    if not first_row:
        sheet.insert_row(headers, index=1)


def save_timesheet(project_info: dict, expert_name: str, tg_id: int,
                   start_time: datetime, end_time: datetime, comment: str) -> float:
    client = get_client()
    try:
        sheet = client.open_by_key(GOOGLE_SHEET_ID).worksheet("Таймшит")
    except gspread.WorksheetNotFound:
        spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)
        sheet = spreadsheet.add_worksheet(title="Таймшит", rows=1000, cols=12)

    ensure_timesheet_headers(sheet)

    duration_min = round((end_time - start_time).total_seconds() / 60, 1)

    sheet.append_row([
        start_time.strftime("%d.%m.%Y"),
        project_info["project_id"],
        project_info["name"],
        project_info["client"],
        project_info["company"],
        expert_name,
        str(tg_id),
        start_time.strftime("%H:%M:%S"),
        end_time.strftime("%H:%M:%S"),
        duration_min,
        comment,
    ])
    return duration_min


def format_elapsed(start_time: datetime) -> str:
    elapsed = datetime.now() - start_time
    total_seconds = int(elapsed.total_seconds())
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def timer_text(project_name: str, start_time: datetime) -> str:
    return (
        f"Timer running\n\n"
        f"Проект: {project_name}\n"
        f"Начало: {start_time.strftime('%H:%M:%S')}\n"
        f"Прошло: {format_elapsed(start_time)}\n\n"
        f"Нажмите кнопку когда закончите работу:"
    )


async def update_timer_job(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data
    try:
        await context.bot.edit_message_text(
            chat_id=job_data["chat_id"],
            message_id=job_data["message_id"],
            text=timer_text(job_data["project"], job_data["start_time"]),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Stop - Завершить работу", callback_data="stop_work")]
            ]),
        )
    except Exception:
        pass


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    tg_id = update.effective_user.id

    await update.message.reply_text(
        "Загружаю ваши проекты...",
        reply_markup=ReplyKeyboardRemove()
    )

    try:
        projects = get_user_projects(tg_id)
    except Exception as e:
        logger.error(f"Error loading projects: {e}")
        await update.message.reply_text("Ошибка подключения к таблице. Попробуйте позже.")
        return ConversationHandler.END

    if not projects:
        await update.message.reply_text(
            "У вас нет активных проектов.\n"
            "Обратитесь к проектному менеджеру."
        )
        return ConversationHandler.END

    context.user_data["projects"] = {p["name"]: p for p in projects}
    project_names = [p["name"] for p in projects]

    keyboard = [[name] for name in project_names]
    keyboard.append(["Отмена"])

    await update.message.reply_text(
        f"Привет, {update.effective_user.first_name}!\n\n"
        f"Ваши активные проекты ({len(projects)}):\n"
        f"Выберите проект для начала работы:",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True),
    )
    return SELECT_PROJECT


async def select_project(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()

    if text == "Отмена":
        return await cancel(update, context)

    projects = context.user_data.get("projects", {})
    if text not in projects:
        await update.message.reply_text("Пожалуйста, выберите проект из списка.")
        return SELECT_PROJECT

    context.user_data["current_project"] = text
    project_info = projects[text]

    keyboard = [["Начать работу"], ["Назад", "Отмена"]]
    await update.message.reply_text(
        f"Проект: {text}\n"
        f"Клиент: {project_info['company']}\n"
        f"Менеджер: {project_info['pm_name']}\n\n"
        f"Нажмите Начать работу чтобы запустить таймер.",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
    )
    return CONFIRM_START


async def confirm_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()

    if text == "Отмена":
        return await cancel(update, context)

    if text == "Назад":
        return await start(update, context)

    if text != "Начать работу":
        await update.message.reply_text("Нажмите Начать работу.")
        return CONFIRM_START

    project_name = context.user_data["current_project"]
    project_info = context.user_data["projects"][project_name]
    start_time = datetime.now()
    context.user_data["start_time"] = start_time
    context.user_data["expert_name"] = update.effective_user.full_name

    await update.message.reply_text("Таймер запущен!", reply_markup=ReplyKeyboardRemove())

    timer_msg = await update.message.reply_text(
        timer_text(project_name, start_time),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Stop - Завершить работу", callback_data="stop_work")]
        ]),
    )
    context.user_data["timer_message_id"] = timer_msg.message_id

    job_name = f"timer_{update.effective_user.id}"
    for job in context.job_queue.get_jobs_by_name(job_name):
        job.schedule_removal()

    context.job_queue.run_repeating(
        update_timer_job,
        interval=30,
        first=30,
        data={
            "chat_id": update.effective_chat.id,
            "message_id": timer_msg.message_id,
            "project": project_name,
            "start_time": start_time,
        },
        name=job_name,
    )
    context.user_data["job_name"] = job_name

    pm_tg_id = project_info.get("pm_tg_id")
    if pm_tg_id:
        try:
            await context.bot.send_message(
                chat_id=int(pm_tg_id),
                text=(
                    f"Начало работы\n\n"
                    f"Проект: {project_name}\n"
                    f"Клиент: {project_info['company']}\n"
                    f"Эксперт: {update.effective_user.full_name}\n"
                    f"Время: {start_time.strftime('%d.%m.%Y %H:%M:%S')}\n"
                    f"Telegram: @{update.effective_user.username}"
                ),
            )
        except Exception as e:
            logger.warning(f"Could not notify PM: {e}")

    return WORKING


async def stop_work(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    job_name = context.user_data.get("job_name")
    if job_name:
        for job in context.job_queue.get_jobs_by_name(job_name):
            job.schedule_removal()

    start_time = context.user_data["start_time"]
    end_time = datetime.now()
    context.user_data["end_time"] = end_time
    elapsed = format_elapsed(start_time)
    project_name = context.user_data["current_project"]

    await query.edit_message_text(
        f"Работа завершена\n\n"
        f"Проект: {project_name}\n"
        f"Начало: {start_time.strftime('%H:%M:%S')}\n"
        f"Конец: {end_time.strftime('%H:%M:%S')}\n"
        f"Итого: {elapsed}",
    )

    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="Напишите комментарий по проделанной работе:",
        reply_markup=ReplyKeyboardRemove(),
    )
    return COMMENTING


async def save_comment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    comment = update.message.text.strip()
    project_name = context.user_data["current_project"]
    project_info = context.user_data["projects"][project_name]
    start_time = context.user_data["start_time"]
    end_time = context.user_data["end_time"]
    expert_name = context.user_data["expert_name"]
    tg_id = update.effective_user.id

    await update.message.reply_text("Сохраняю данные...")

    try:
        duration_min = save_timesheet(
            project_info, expert_name, tg_id,
            start_time, end_time, comment
        )
    except Exception as e:
        logger.error(f"Save timesheet error: {e}")
        await update.message.reply_text("Ошибка сохранения. Обратитесь к администратору.")
        return ConversationHandler.END

    elapsed = format_elapsed(start_time)

    await update.message.reply_text(
        f"Данные сохранены!\n\n"
        f"Проект: {project_name}\n"
        f"Время работы: {elapsed} ({duration_min} мин)\n"
        f"Комментарий: {comment}\n\n"
        f"Чтобы начать новый сеанс - напишите /start"
    )

    pm_tg_id = project_info.get("pm_tg_id")
    if pm_tg_id:
        try:
            await context.bot.send_message(
                chat_id=int(pm_tg_id),
                text=(
                    f"Завершение работы\n\n"
                    f"Проект: {project_name}\n"
                    f"Клиент: {project_info['company']}\n"
                    f"Эксперт: {expert_name}\n"
                    f"Начало: {start_time.strftime('%d.%m.%Y %H:%M:%S')}\n"
                    f"Конец: {end_time.strftime('%d.%m.%Y %H:%M:%S')}\n"
                    f"Итого: {elapsed} ({duration_min} мин)\n"
                    f"Комментарий: {comment}"
                ),
            )
        except Exception as e:
            logger.warning(f"Could not notify PM: {e}")

    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    job_name = context.user_data.get("job_name")
    if job_name:
        for job in context.job_queue.get_jobs_by_name(job_name):
            job.schedule_removal()

    await update.message.reply_text(
        "Отменено. Напишите /start чтобы начать заново.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Error: {context.error}", exc_info=context.error)


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            SELECT_PROJECT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, select_project)
            ],
            CONFIRM_START: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_start)
            ],
            WORKING: [
                CallbackQueryHandler(stop_work, pattern="^stop_work$")
            ],
            COMMENTING: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_comment)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(conv_handler)
    app.add_error_handler(error_handler)
    logger.info("Timesheet bot started.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
