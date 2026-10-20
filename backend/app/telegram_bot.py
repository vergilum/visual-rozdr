from __future__ import annotations

import asyncio
from contextlib import suppress
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .config import Settings
from .storage import Storage


START_WITHOUT_CODE = "Откройте ссылку или отсканируйте QR-код мероприятия, чтобы отправить видеокружочек."
HELP_TEXT = "Отправьте видеокружочек Telegram. Обычные видео, фото, документы и текст в очередь не попадают."
CONNECTED_TEXT = "Вы подключены к мероприятию. Отправьте видеокружочек Telegram."
RECEPTION_CLOSED_TEXT = "Приём поздравлений для этого мероприятия завершён."
UNKNOWN_CODE_TEXT = "Мероприятие не найдено. Проверьте ссылку или отсканируйте QR-код ещё раз."
NEED_BINDING_TEXT = "Сначала откройте ссылку мероприятия или отсканируйте QR-код."
TABLE_QUESTION_TEXT = "С какого столика было отправлено это видео?"
TABLE_SELECTED_TEXT = "Для этого видео выбран стол {table_number}."
UNSUPPORTED_TEXT = "Пожалуйста, отправьте именно видеокружочек Telegram. Обычные видео, фото и текст не попадают в очередь."
RECEIVED_TEXT = "Кружочек получен, готовим его к показу."
LIMIT_TEXT = "Лимит поздравлений для этого мероприятия уже исчерпан."
LOW_DISK_TEXT = "Сейчас на ноутбуке недостаточно свободного места. Организаторы уже видят проблему, попробуйте отправить поздравление позже."


@dataclass(frozen=True)
class TelegramVideoNotePayload:
    telegram_user_id: int
    telegram_chat_id: int
    telegram_message_id: int
    guest_display_name: str
    telegram_file_id: str
    telegram_file_unique_id: str | None
    telegram_file_size: int | None
    telegram_duration_sec: int | None


class TelegramReceptionService:
    def __init__(self, storage: Storage):
        self.storage = storage

    def connect_guest(self, telegram_user_id: int, code: str | None) -> str:
        if not code:
            return START_WITHOUT_CODE
        try:
            event = self.storage.get_event_by_code(code)
        except KeyError:
            return UNKNOWN_CODE_TEXT
        if not event["active"] or not event["reception_open"]:
            return RECEPTION_CLOSED_TEXT
        self.storage.bind_guest(telegram_user_id, event["id"])
        return CONNECTED_TEXT

    def receive_video_note(self, payload: TelegramVideoNotePayload) -> tuple[str, bool]:
        binding = self.storage.get_guest_binding(payload.telegram_user_id)
        if binding is None:
            return NEED_BINDING_TEXT, False

        active_event = self.storage.get_active_event()
        if active_event is None or binding["event_id"] != active_event["id"]:
            return NEED_BINDING_TEXT, False
        if not active_event["reception_open"]:
            return RECEPTION_CLOSED_TEXT, False
        if not self.storage.has_min_free_disk():
            return LOW_DISK_TEXT, False

        _, created = self.storage.create_pending_table_selection(
            {
                "event_id": active_event["id"],
                "telegram_chat_id": payload.telegram_chat_id,
                "telegram_message_id": payload.telegram_message_id,
                "telegram_user_id": payload.telegram_user_id,
                "guest_display_name": payload.guest_display_name,
                "telegram_file_id": payload.telegram_file_id,
                "telegram_file_unique_id": payload.telegram_file_unique_id,
                "telegram_file_size": payload.telegram_file_size,
                "telegram_duration_sec": payload.telegram_duration_sec,
            }
        )
        return RECEIVED_TEXT, created


def extract_start_code(text: str | None) -> str | None:
    if not text:
        return None
    parts = text.strip().split(maxsplit=1)
    if len(parts) < 2:
        return None
    return parts[1].strip() or None


def create_dispatcher(storage: Storage, on_change: Callable[[], Awaitable[None]] | None = None) -> Dispatcher:
    router = Router()
    service = TelegramReceptionService(storage)

    async def notify_change() -> None:
        if on_change is not None:
            await on_change()

    def table_keyboard(event_id: str, submission_id: str, page: int) -> InlineKeyboardMarkup:
        tables = storage.get_event_tables(event_id)
        page_count = max(1, (len(tables) + 4) // 5)
        page = max(0, min(page, page_count - 1))
        visible = tables[page * 5:(page + 1) * 5]
        rows = [[InlineKeyboardButton(text=f"Стол {table['number']}", callback_data=f"table_select:{submission_id}:{table['number']}")] for table in visible]
        navigation = []
        if page > 0:
            navigation.append(InlineKeyboardButton(text="Назад", callback_data=f"table_page:{submission_id}:{page - 1}"))
        if page < page_count - 1:
            navigation.append(InlineKeyboardButton(text="Далее", callback_data=f"table_page:{submission_id}:{page + 1}"))
        if navigation:
            rows.append(navigation)
        return InlineKeyboardMarkup(inline_keyboard=rows)

    async def request_table(message: Message, event_id: str, submission_id: str, page: int = 0) -> None:
        tables = storage.get_event_tables(event_id)
        if not tables:
            await message.answer("Организатор ещё не настроил столы для этого мероприятия.")
            return
        await message.answer(TABLE_QUESTION_TEXT, reply_markup=table_keyboard(event_id, submission_id, page))

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        if message.from_user is None:
            await message.answer(START_WITHOUT_CODE)
            return
        code = extract_start_code(message.text)
        response = service.connect_guest(message.from_user.id, code)
        if response == CONNECTED_TEXT:
            await notify_change()
        await message.answer(response)

    @router.message(Command("help"))
    async def help_command(message: Message) -> None:
        await message.answer(HELP_TEXT)

    @router.callback_query(lambda callback: callback.data is not None and callback.data.startswith("table_"))
    async def table_callback(callback: CallbackQuery) -> None:
        if callback.from_user is None or callback.message is None or callback.data is None:
            return
        _, selection_id, value = callback.data.split(":", 2)
        selection = storage.get_pending_table_selection(selection_id)
        if selection is None:
            await callback.answer("Видео больше недоступно.", show_alert=True)
            return
        if selection["telegram_user_id"] != callback.from_user.id:
            await callback.answer("Этот выбор относится к другому видео.", show_alert=True)
            return
        if callback.data.startswith("table_page:"):
            page = int(value)
            await callback.message.edit_reply_markup(reply_markup=table_keyboard(selection["event_id"], selection_id, page))
            await callback.answer()
            return
        table_number = int(value)
        try:
            storage.confirm_pending_table_selection(selection_id, callback.from_user.id, table_number)
        except ValueError as exc:
            await callback.answer(str(exc), show_alert=True)
            return
        await callback.message.edit_text(f"{TABLE_SELECTED_TEXT.format(table_number=table_number)} Кружочек получен, готовим его к показу.")
        await notify_change()
        await callback.answer()

    @router.message(lambda message: message.video_note is not None)
    async def video_note(message: Message) -> None:
        if message.from_user is None or message.video_note is None:
            await message.answer(NEED_BINDING_TEXT)
            return
        payload = TelegramVideoNotePayload(
            telegram_user_id=message.from_user.id,
            telegram_chat_id=message.chat.id,
            telegram_message_id=message.message_id,
            guest_display_name=message.from_user.full_name,
            telegram_file_id=message.video_note.file_id,
            telegram_file_unique_id=message.video_note.file_unique_id,
            telegram_file_size=message.video_note.file_size,
            telegram_duration_sec=message.video_note.duration,
        )
        response, created = service.receive_video_note(payload)
        if created:
            await notify_change()
        await message.answer(response)
        selection = storage.get_pending_table_selection_by_message(payload.telegram_chat_id, payload.telegram_message_id)
        if selection is not None:
            await request_table(message, selection["event_id"], selection["id"])

    @router.message()
    async def unsupported(message: Message) -> None:
        await message.answer(UNSUPPORTED_TEXT)

    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    return dispatcher


class TelegramBotRunner:
    def __init__(self, settings: Settings, storage: Storage, on_change: Callable[[], Awaitable[None]] | None = None):
        self.settings = settings
        self.storage = storage
        self.on_change = on_change
        self.bot: Bot | None = None
        self.dispatcher: Dispatcher | None = None
        self.task: asyncio.Task[None] | None = None
        self._stopping = False

    @property
    def enabled(self) -> bool:
        return bool(self.settings.telegram_bot_token)

    async def start(self) -> None:
        if not self.enabled:
            self.storage.telegram_bot_status = "not_configured"
            return
        self._stopping = False
        self.storage.telegram_bot_status = "starting"
        self.storage.telegram_bot_error = None
        self.task = asyncio.create_task(self._run_forever())

    async def _run_forever(self) -> None:
        while not self._stopping:
            self.bot = Bot(token=self.settings.telegram_bot_token or "")
            self.dispatcher = create_dispatcher(self.storage, self.on_change)
            try:
                self.storage.telegram_bot_status = "running"
                self.storage.telegram_bot_error = None
                await self.dispatcher.start_polling(self.bot, handle_signals=False)
                if not self._stopping:
                    self.storage.telegram_bot_status = "stopped"
            except asyncio.CancelledError:
                self.storage.telegram_bot_status = "stopped"
                raise
            except Exception as exc:
                self.storage.telegram_bot_status = "error"
                self.storage.telegram_bot_error = str(exc)[:500]
                if self._stopping:
                    break
                await asyncio.sleep(5)
            finally:
                if self.bot is not None:
                    await self.bot.session.close()
                self.bot = None
                self.dispatcher = None

    async def stop(self) -> None:
        self._stopping = True
        if self.dispatcher is not None:
            with suppress(RuntimeError):
                await self.dispatcher.stop_polling()
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.storage.telegram_bot_status = "stopped" if self.enabled else "not_configured"
