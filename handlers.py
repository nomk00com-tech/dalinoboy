"""
Telegram bot handlers (aiogram 3.x) — inline UI.

UX principles:
  * ONE message that gets EDITED as you navigate (no spam of new messages).
  * All actions are inline buttons attached to that message (buttons in chat).
  * Typed input (names, numbers) is still needed for some steps; the user's typed
    message is deleted and the anchor message is edited to the next step.

Notifications about GPS status come from scheduler.py (separate alert messages).
"""

import logging
import os

from aiogram import Bot, Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, ErrorEvent,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

import db
import news
import puesc
import scheduler

log = logging.getLogger(__name__)

router = Router()

ADMIN_IDS: set[int] = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}

# (chat_id, trip_id) -> message_id of the last map pin, so a re-check deletes the
# previous map and posts a fresh one instead of piling up maps in the chat.
_live_maps: dict[tuple[int, int], int] = {}


async def _cleanup_maps(chat_id: int, bot: Bot):
    """Delete every live map pin belonging to this chat."""
    to_remove = [k for k in _live_maps if k[0] == chat_id]
    for key in to_remove:
        mid = _live_maps.pop(key, None)
        if mid:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=mid)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_admin(telegram_id: int) -> bool:
    return telegram_id in ADMIN_IDS


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _admin_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("➕ Додати рейс", "m:add_trip"), _btn("📋 Рейси", "m:trips")],
        [_btn("🚗 Авто і трекери", "m:veh"), _btn("👥 Водії та доступ", "m:users")],
        [_btn("📰 Новини PUESC", "m:news")],
    ])


def _back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_btn("⬅️ Меню", "m:main")]])


def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_btn("✖️ Скасувати", "m:main")]])


_MENU_TITLE_ADMIN = "🛰 <b>Меню адміністратора</b>\nОберіть дію:"


async def _safe_edit(target, text: str, kb: InlineKeyboardMarkup | None):
    """Edit a message, ignoring 'message is not modified' and falling back to send."""
    try:
        await target.edit_text(text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as exc:
        if "not modified" in str(exc).lower():
            return
        try:
            await target.answer(text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)
        except Exception as e2:
            log.warning("edit/send failed: %s", e2)


async def _edit_anchor(bot: Bot, chat_id: int, message_id: int, text: str, kb: InlineKeyboardMarkup | None):
    """Edit the stored anchor message by id (used after typed input)."""
    try:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id,
                                    reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as exc:
        if "not modified" in str(exc).lower():
            return
        await bot.send_message(chat_id, text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)


async def _show_main(cb: CallbackQuery):
    if _is_admin(cb.from_user.id):
        await _safe_edit(cb.message, _MENU_TITLE_ADMIN, _admin_menu_kb())
    else:
        text, kb = await _driver_screen(cb.from_user.id)
        await _safe_edit(cb.message, text, kb)


# ---------------------------------------------------------------------------
# FSM states (typed input only)
# ---------------------------------------------------------------------------

class AddVehicle(StatesGroup):
    name = State()
    plate = State()


class EditVehicle(StatesGroup):
    name = State()
    plate = State()


class AddTracker(StatesGroup):
    number = State()


class AddTrip(StatesGroup):
    rmpd = State()


class AddRmpd(StatesGroup):
    rmpd = State()


class AddRecipient(StatesGroup):
    tgid = State()


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

@router.error()
async def on_error(event: ErrorEvent):
    """Swallow harmless Telegram errors (expired callback queries, no-op edits)."""
    text = str(event.exception).lower()
    if "query is too old" in text or "message is not modified" in text or "message to edit not found" in text:
        return True
    log.exception("Unhandled error: %s", event.exception)
    return True


@router.message(Command("start"))
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    user = msg.from_user
    is_admin = _is_admin(user.id)
    await db.upsert_user(user.id, user.full_name, is_admin=is_admin, username=user.username)

    if is_admin:
        await msg.answer(_MENU_TITLE_ADMIN, reply_markup=_admin_menu_kb(), parse_mode="HTML")
        return

    # Non-admin: only pre-authorised drivers may use the bot.
    if await db.is_allowed(user.id, user.username):
        text, kb = await _driver_screen(user.id)
        await msg.answer(text, reply_markup=kb, parse_mode="HTML")
    else:
        uname = f"@{user.username}" if user.username else "—"
        await msg.answer(
            "🚫 Доступ закрито.\n\n"
            "Зверніться до адміністратора, щоб він додав вас у список водіїв.\n"
            f"Ваш ID: <code>{user.id}</code>\nВаш username: {uname}",
            parse_mode="HTML",
        )


# ---------------------------------------------------------------------------
# Driver onboarding (pick your vehicle)
# ---------------------------------------------------------------------------

async def _driver_screen(telegram_id: int):
    """Build (text, kb) for a driver: shows their vehicle + lets them (re)select it."""
    u = await db.get_user(telegram_id)
    vid = u.get("vehicle_id") if u else None
    vehicles = await db.get_all_vehicles()
    if vid:
        v = await db.get_vehicle(vid)
        if v:
            text = (f"🚚 <b>Ваша машина:</b> {v['name']} ({v['plate_number']})\n\n"
                    "Сповіщення про GPS цієї машини приходитимуть саме вам.")
        else:
            text = "🚚 Ваша машина більше не існує. Оберіть іншу:"
            vid = None
    else:
        text = "🚚 Оберіть свою машину, щоб отримувати сповіщення лише по ній:"
    rows = [[_btn(("✅ " if v["id"] == vid else "") + f"{v['name']} ({v['plate_number']})",
                  f"mycar:{v['id']}")] for v in vehicles]
    if not vehicles:
        text = "Поки немає жодної машини. Зачекайте, доки адміністратор їх додасть."
    rows.append([_btn("📰 Новини PUESC", "m:news")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("mycar:"))
async def cb_mycar(cb: CallbackQuery):
    await _cleanup_maps(cb.message.chat.id, cb.bot)
    if _is_admin(cb.from_user.id):
        await cb.answer()
        return
    if not await db.is_allowed(cb.from_user.id, cb.from_user.username):
        await cb.answer("Доступ закрито.", show_alert=True)
        return
    vid = int(cb.data.split(":")[1])
    await db.set_user_vehicle(cb.from_user.id, vid)
    text, kb = await _driver_screen(cb.from_user.id)
    await _safe_edit(cb.message, "✅ Машину обрано.\n\n" + text, kb)
    await cb.answer("Збережено")


@router.callback_query(F.data == "m:main")
async def cb_main(cb: CallbackQuery, state: FSMContext):
    await _cleanup_maps(cb.message.chat.id, cb.bot)
    await state.clear()
    await _show_main(cb)
    await cb.answer()


# ---------------------------------------------------------------------------
# Trips: list -> detail (with RMPD stack) -> actions
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "m:trips")
async def cb_trips(cb: CallbackQuery, state: FSMContext):
    await _cleanup_maps(cb.message.chat.id, cb.bot)
    await state.clear()
    is_admin = _is_admin(cb.from_user.id)
    trips = await db.get_active_trips()
    rows = [[_btn(f"🚛 {t['vehicle_name']} ({t['plate_number']}) — {t['rmpd_number']}", f"trip:{t['id']}")]
            for t in trips]
    if is_admin:
        rows.append([_btn("➕ Додати рейс", "m:add_trip")])
    rows.append([_btn("⬅️ Меню", "m:main")])
    title = "📋 <b>Активні рейси</b>\nОберіть рейс:" if trips else "📋 Немає активних рейсів."
    await _safe_edit(cb.message, title, InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


async def _trip_detail(tid: int, is_admin: bool):
    """Build (text, kb) for a trip: current RMPD + stack history + actions."""
    trip = await db.get_trip(tid)
    if not trip:
        return "Рейс не знайдено.", InlineKeyboardMarkup(inline_keyboard=[[_btn("⬅️ Рейси", "m:trips")]])
    rmpds = await db.get_trip_rmpds(tid)
    lines = [f"🚛 <b>{trip['vehicle_name']}</b> ({trip['plate_number']})",
             f"Створено: {trip['created_at'][:16]}\n",
             f"📍 Поточний RMPD (їдемо по ньому):\n<code>{trip['rmpd_number']}</code>"]
    if len(rmpds) > 1:
        lines.append("\n🗂 Історія RMPD (старі вже не моніторяться):")
        for i, r in enumerate(rmpds, 1):
            mark = "🟢 поточний" if r["rmpd_number"] == trip["rmpd_number"] else "▫️ старий"
            lines.append(f"  {i}. <code>{r['rmpd_number']}</code> — {mark}")
    rows = [[_btn("🔍 Перевірити", f"chk:{tid}")]]
    if is_admin:
        rows.append([_btn("➕ Новий RMPD", f"rmpdadd:{tid}")])
        rows.append([_btn("✅ Завершити", f"fin:{tid}"), _btn("🗑 Видалити", f"delq:{tid}")])
    rows.append([_btn("⬅️ Рейси", "m:trips")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("trip:"))
async def cb_trip_detail(cb: CallbackQuery, state: FSMContext):
    await _cleanup_maps(cb.message.chat.id, cb.bot)
    await state.clear()
    tid = int(cb.data.split(":")[1])
    text, kb = await _trip_detail(tid, _is_admin(cb.from_user.id))
    await _safe_edit(cb.message, text, kb)
    await cb.answer()


@router.callback_query(F.data.startswith("rmpdadd:"))
async def cb_rmpd_add(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb.from_user.id):
        await cb.answer("Недостатньо прав.", show_alert=True)
        return
    tid = int(cb.data.split(":")[1])
    await state.set_state(AddRmpd.rmpd)
    await state.update_data(trip_id=tid, anchor=cb.message.message_id)
    await _safe_edit(cb.message,
                     "➕ Введіть <b>новий номер RMPD</b>.\n"
                     "Він стане поточним — бот моніторитиме саме його, старий більше не потрібен.",
                     _cancel_kb())
    await cb.answer()


@router.message(AddRmpd.rmpd)
async def add_rmpd_do(msg: Message, state: FSMContext, bot: Bot):
    rmpd = msg.text.strip()
    data = await state.get_data()
    tid = data["trip_id"]
    await _delete(msg)
    await state.clear()
    try:
        await db.add_trip_rmpd(tid, rmpd)
        prefix = f"✅ Новий RMPD <code>{rmpd}</code> — тепер моніторимо його.\n\n"
    except Exception as exc:
        prefix = f"⚠️ Помилка: {exc}\n\n"
    text, kb = await _trip_detail(tid, True)
    await _edit_anchor(bot, msg.chat.id, data["anchor"], prefix + text, kb)


# ---------------------------------------------------------------------------
# Vehicles & trackers management
# ---------------------------------------------------------------------------

# Two tracker slots per vehicle: main + backup.
ROLE_LABEL = {"main": "Основний", "backup": "Запасний"}
ROLE_ICON = {"main": "🟢", "backup": "🟡"}


def _tracker_by_role(trackers: list[dict], role: str) -> dict | None:
    return next((t for t in trackers if t["provider"] == ROLE_LABEL[role]), None)


async def _vehicle_detail(vid: int):
    """Build (text, keyboard) for a vehicle detail screen with main+backup slots."""
    v = await db.get_vehicle(vid)
    if not v:
        return "Авто не знайдено.", InlineKeyboardMarkup(inline_keyboard=[[_btn("⬅️ До списку", "m:veh")]])
    trackers = await db.get_trackers_for_vehicle(vid)
    lines = [f"🚗 <b>{v['name']}</b> ({v['plate_number']})", ""]
    rows = [[_btn("✏️ Назва", f"vehname:{vid}"), _btn("🔢 Номер", f"vehplate:{vid}")]]
    for role in ("main", "backup"):
        t = _tracker_by_role(trackers, role)
        label, icon = ROLE_LABEL[role], ROLE_ICON[role]
        if t:
            lines.append(f"{icon} <b>{label}</b>: <code>{t['tracker_number']}</code>")
            rows.append([_btn(f"📡 {label}: {t['tracker_number']}", f"trkslot:{vid}:{role}")])
        else:
            lines.append(f"{icon} <b>{label}</b>: <i>не додано</i>")
            rows.append([_btn(f"➕ Додати {label.lower()}", f"trkslot:{vid}:{role}")])
    lines.append("\nℹ️ Достатньо, щоб працював хоч один трекер — авто відстежується.")
    rows.append([_btn("🗑 Видалити авто", f"vehdel:{vid}")])
    rows.append([_btn("⬅️ До списку", "m:veh")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "m:veh")
async def cb_veh(cb: CallbackQuery, state: FSMContext):
    await _cleanup_maps(cb.message.chat.id, cb.bot)
    if not _is_admin(cb.from_user.id):
        await cb.answer("Недостатньо прав.", show_alert=True)
        return
    await state.clear()
    vehicles = await db.get_all_vehicles()
    rows = [[_btn(f"{v['name']} ({v['plate_number']})", f"veh:{v['id']}")] for v in vehicles]
    rows.append([_btn("➕ Додати авто", "vehadd")])
    rows.append([_btn("⬅️ Меню", "m:main")])
    title = "🚗 <b>Авто і трекери</b>\nОберіть авто або додайте нове:" if vehicles \
        else "🚗 <b>Авто і трекери</b>\nПоки немає жодного авто."
    await _safe_edit(cb.message, title, InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@router.callback_query(F.data.startswith("veh:"))
async def cb_veh_detail(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    vid = int(cb.data.split(":")[1])
    text, kb = await _vehicle_detail(vid)
    await _safe_edit(cb.message, text, kb)
    await cb.answer()


# --- Add vehicle ---

@router.callback_query(F.data == "vehadd")
async def cb_veh_add(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AddVehicle.name)
    await state.update_data(anchor=cb.message.message_id)
    await _safe_edit(cb.message, "🚗 Введіть <b>назву авто</b> (напр.: Volvo FH-1):", _cancel_kb())
    await cb.answer()


@router.message(AddVehicle.name)
async def add_veh_name(msg: Message, state: FSMContext, bot: Bot):
    name = msg.text.strip()
    data = await state.get_data()
    await _delete(msg)
    await state.update_data(name=name)
    await state.set_state(AddVehicle.plate)
    await _edit_anchor(bot, msg.chat.id, data["anchor"],
                       f"🚗 Авто: <b>{name}</b>\nВведіть <b>номерний знак</b> (напр.: BC8849PO):", _cancel_kb())


@router.message(AddVehicle.plate)
async def add_veh_plate(msg: Message, state: FSMContext, bot: Bot):
    plate = msg.text.strip().upper()
    data = await state.get_data()
    await _delete(msg)
    await state.clear()
    try:
        vid = await db.add_vehicle(data["name"], plate)
        text, kb = await _vehicle_detail(vid)
        text = "✅ Авто додано.\n\n" + text
    except Exception as exc:
        text, kb = f"⚠️ Помилка: {exc}", InlineKeyboardMarkup(inline_keyboard=[[_btn("⬅️ До списку", "m:veh")]])
    await _edit_anchor(bot, msg.chat.id, data["anchor"], text, kb)


# --- Edit vehicle name / plate ---

@router.callback_query(F.data.startswith("vehname:"))
async def cb_veh_edit_name(cb: CallbackQuery, state: FSMContext):
    vid = int(cb.data.split(":")[1])
    await state.set_state(EditVehicle.name)
    await state.update_data(vehicle_id=vid, anchor=cb.message.message_id)
    await _safe_edit(cb.message, "✏️ Введіть <b>нову назву</b> авто:", _cancel_kb())
    await cb.answer()


@router.message(EditVehicle.name)
async def edit_veh_name(msg: Message, state: FSMContext, bot: Bot):
    name = msg.text.strip()
    data = await state.get_data()
    await _delete(msg)
    await state.clear()
    await db.update_vehicle(data["vehicle_id"], name=name)
    text, kb = await _vehicle_detail(data["vehicle_id"])
    await _edit_anchor(bot, msg.chat.id, data["anchor"], "✅ Назву змінено.\n\n" + text, kb)


@router.callback_query(F.data.startswith("vehplate:"))
async def cb_veh_edit_plate(cb: CallbackQuery, state: FSMContext):
    vid = int(cb.data.split(":")[1])
    await state.set_state(EditVehicle.plate)
    await state.update_data(vehicle_id=vid, anchor=cb.message.message_id)
    await _safe_edit(cb.message, "🔢 Введіть <b>новий номерний знак</b>:", _cancel_kb())
    await cb.answer()


@router.message(EditVehicle.plate)
async def edit_veh_plate(msg: Message, state: FSMContext, bot: Bot):
    plate = msg.text.strip().upper()
    data = await state.get_data()
    await _delete(msg)
    await state.clear()
    try:
        await db.update_vehicle(data["vehicle_id"], plate=plate)
        text, kb = await _vehicle_detail(data["vehicle_id"])
        text = "✅ Номер змінено.\n\n" + text
    except Exception as exc:
        text, kb = f"⚠️ Помилка: {exc}", InlineKeyboardMarkup(inline_keyboard=[[_btn("⬅️ До списку", "m:veh")]])
    await _edit_anchor(bot, msg.chat.id, data["anchor"], text, kb)


# --- Delete vehicle ---

@router.callback_query(F.data.startswith("vehdel:"))
async def cb_veh_del_confirm(cb: CallbackQuery):
    vid = int(cb.data.split(":")[1])
    v = await db.get_vehicle(vid)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        _btn("🗑 Так, видалити", f"vehdelok:{vid}"), _btn("✖️ Назад", f"veh:{vid}"),
    ]])
    await _safe_edit(cb.message,
                     f"🗑 Видалити авто <b>{v['name']}</b> ({v['plate_number']})?\n"
                     f"Разом з трекерами, рейсами та історією. Незворотно.", kb)
    await cb.answer()


@router.callback_query(F.data.startswith("vehdelok:"))
async def cb_veh_del_do(cb: CallbackQuery):
    vid = int(cb.data.split(":")[1])
    await db.delete_vehicle(vid)
    vehicles = await db.get_all_vehicles()
    rows = [[_btn(f"{v['name']} ({v['plate_number']})", f"veh:{v['id']}")] for v in vehicles]
    rows.append([_btn("➕ Додати авто", "vehadd")])
    rows.append([_btn("⬅️ Меню", "m:main")])
    await _safe_edit(cb.message, "🗑 Авто видалено.\n\n🚗 <b>Авто і трекери</b>:",
                     InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer("Видалено")


# --- Tracker slot (main / backup): open, add/edit, delete ---

@router.callback_query(F.data.startswith("trkslot:"))
async def cb_trk_slot(cb: CallbackQuery, state: FSMContext):
    _, vid, role = cb.data.split(":")
    vid = int(vid)
    label = ROLE_LABEL[role]
    trackers = await db.get_trackers_for_vehicle(vid)
    t = _tracker_by_role(trackers, role)
    if t:
        # Slot occupied — offer edit/delete.
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [_btn("✏️ Змінити номер", f"trkedit:{vid}:{role}")],
            [_btn("🗑 Видалити", f"trkdelrole:{vid}:{role}")],
            [_btn("✖️ Назад", f"veh:{vid}")],
        ])
        await _safe_edit(cb.message,
                         f"{ROLE_ICON[role]} <b>{label} трекер</b>\n"
                         f"Номер: <code>{t['tracker_number']}</code>", kb)
    else:
        # Empty slot — ask for number.
        await state.set_state(AddTracker.number)
        await state.update_data(vehicle_id=vid, provider=label, anchor=cb.message.message_id)
        await _safe_edit(cb.message,
                         f"{ROLE_ICON[role]} Введіть <b>номер {label.lower()} локалізатора</b> "
                         f"(напр.: Z21-AF67XZ-8):", _cancel_kb())
    await cb.answer()


@router.callback_query(F.data.startswith("trkedit:"))
async def cb_trk_edit(cb: CallbackQuery, state: FSMContext):
    _, vid, role = cb.data.split(":")
    label = ROLE_LABEL[role]
    await state.set_state(AddTracker.number)
    await state.update_data(vehicle_id=int(vid), provider=label, anchor=cb.message.message_id)
    await _safe_edit(cb.message, f"✏️ Введіть <b>новий номер {label.lower()} локалізатора</b>:", _cancel_kb())
    await cb.answer()


@router.message(AddTracker.number)
async def add_trk_number(msg: Message, state: FSMContext, bot: Bot):
    number = msg.text.strip()
    data = await state.get_data()
    vid = data["vehicle_id"]
    await _delete(msg)
    await state.clear()
    if not puesc.LOCATOR_RE.match(number):
        prefix = (f"⚠️ Номер <code>{number}</code> має невірний формат "
                  f"(очікується напр. <b>Z21-AF67XZ-8</b>).\n\n")
    else:
        try:
            await db.add_tracker(vid, data["provider"], number)   # upsert by (vehicle, role)
            prefix = f"✅ {data['provider']} трекер збережено.\n\n"
        except Exception as exc:
            prefix = f"⚠️ Помилка: {exc}\n\n"
    text, kb = await _vehicle_detail(vid)
    await _edit_anchor(bot, msg.chat.id, data["anchor"], prefix + text, kb)


@router.callback_query(F.data.startswith("trkdelrole:"))
async def cb_trk_del_role(cb: CallbackQuery):
    _, vid, role = cb.data.split(":")
    vid = int(vid)
    trackers = await db.get_trackers_for_vehicle(vid)
    t = _tracker_by_role(trackers, role)
    if t:
        await db.delete_tracker(t["id"])
    text, kb = await _vehicle_detail(vid)
    await _safe_edit(cb.message, f"🗑 {ROLE_LABEL[role]} трекер видалено.\n\n" + text, kb)
    await cb.answer("Видалено")


# ---------------------------------------------------------------------------
# Add trip (select vehicle -> typed RMPD)
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "m:add_trip")
async def cb_add_trip(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb.from_user.id):
        await cb.answer("Недостатньо прав.", show_alert=True)
        return
    vehicles = await db.get_all_vehicles()
    if not vehicles:
        await _safe_edit(cb.message, "Спочатку додайте авто.", _back_kb())
        await cb.answer()
        return
    rows = []
    for v in vehicles:
        trackers = await db.get_trackers_for_vehicle(v["id"])
        cnt = f" [{len(trackers)} трек.]" if trackers else " [без трекерів]"
        rows.append([_btn(f"{v['name']} ({v['plate_number']}){cnt}", f"tripveh:{v['id']}")])
    rows.append([_btn("⬅️ Меню", "m:main")])
    await _safe_edit(cb.message, "➕ Оберіть авто для рейсу:", InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@router.callback_query(F.data.startswith("tripveh:"))
async def cb_trip_vehicle(cb: CallbackQuery, state: FSMContext):
    vid = int(cb.data.split(":")[1])
    v = await db.get_vehicle(vid)
    await state.update_data(vehicle_id=vid, anchor=cb.message.message_id)
    await state.set_state(AddTrip.rmpd)
    await _safe_edit(cb.message,
                     f"➕ Авто: <b>{v['name']}</b> ({v['plate_number']})\n"
                     f"Введіть <b>номер RMPD</b> (напр.: RMPD20260607000433):", _cancel_kb())
    await cb.answer()


@router.message(AddTrip.rmpd)
async def add_trip_rmpd(msg: Message, state: FSMContext, bot: Bot):
    rmpd = msg.text.strip()
    data = await state.get_data()
    await _delete(msg)
    await state.clear()
    try:
        trip_id = await db.add_trip(data["vehicle_id"], rmpd)
        text, kb = await _trip_detail(trip_id, True)
        text = "✅ Рейс додано.\n\n" + text
    except Exception as exc:
        text, kb = f"⚠️ Помилка: {exc}", _back_kb()
    await _edit_anchor(bot, msg.chat.id, data["anchor"], text, kb)


# --- Finish / delete trip (actions from trip detail) ---

@router.callback_query(F.data.startswith("fin:"))
async def cb_finish_do(cb: CallbackQuery):
    if not _is_admin(cb.from_user.id):
        await cb.answer("Недостатньо прав.", show_alert=True)
        return
    tid = int(cb.data.split(":")[1])
    await db.finish_trip(tid)
    kb = InlineKeyboardMarkup(inline_keyboard=[[_btn("⬅️ Рейси", "m:trips")]])
    await _safe_edit(cb.message, f"✅ Рейс #{tid} завершено.", kb)
    await cb.answer("Завершено")


@router.callback_query(F.data.startswith("delq:"))
async def cb_del_confirm(cb: CallbackQuery):
    if not _is_admin(cb.from_user.id):
        await cb.answer("Недостатньо прав.", show_alert=True)
        return
    tid = int(cb.data.split(":")[1])
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        _btn("🗑 Так, видалити", f"delok:{tid}"), _btn("✖️ Скасувати", f"trip:{tid}"),
    ]])
    await _safe_edit(cb.message, f"🗑 Видалити рейс #{tid} разом з історією? Це незворотно.", kb)
    await cb.answer()


@router.callback_query(F.data.startswith("delok:"))
async def cb_del_do(cb: CallbackQuery):
    tid = int(cb.data.split(":")[1])
    await db.delete_trip(tid)
    kb = InlineKeyboardMarkup(inline_keyboard=[[_btn("⬅️ Рейси", "m:trips")]])
    await _safe_edit(cb.message, f"🗑 Рейс #{tid} видалено.", kb)
    await cb.answer("Видалено")


# ---------------------------------------------------------------------------
# Access management (drivers allowlist)
# ---------------------------------------------------------------------------

async def _access_screen():
    """Build (text, keyboard) for the driver access (allowlist) screen."""
    entries = await db.get_allowlist()
    lines = ["👥 <b>Доступ водіїв</b>\n"
             "Тут ви дозволяєте водіям користуватись ботом. Дозволений водій "
             "натискає /start і обирає свою машину — і отримує сповіщення лише по ній.\n"]
    if entries:
        for e in entries:
            who = f"@{e['username']}" if e["username"] else f"ID <code>{e['telegram_id']}</code>"
            u = await db.find_user(e["telegram_id"], e["username"])
            if u:
                v = await db.get_vehicle(u["vehicle_id"]) if u.get("vehicle_id") else None
                car = f" → 🚚 {v['name']} ({v['plate_number']})" if v else " → (машину не обрано)"
                lines.append(f"✅ {who} — {u['name']}{car}")
            else:
                lines.append(f"⏳ {who} — ще не заходив")
    else:
        lines.append("(поки нікого не дозволено)")
    rows = [[_btn("➕ Дозволити водія", "aladd"), _btn("➖ Прибрати доступ", "aldellist")],
            [_btn("⬅️ Меню", "m:main")]]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "m:users")
async def cb_users(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb.from_user.id):
        await cb.answer("Недостатньо прав.", show_alert=True)
        return
    await state.clear()
    text, kb = await _access_screen()
    await _safe_edit(cb.message, text, kb)
    await cb.answer()


@router.callback_query(F.data == "aladd")
async def cb_al_add(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AddRecipient.tgid)
    await state.update_data(anchor=cb.message.message_id)
    await _safe_edit(cb.message,
                     "➕ Введіть <b>Telegram ID</b> або <b>@username</b> водія.\n\n"
                     "• ID — числом (напр. <code>123456789</code>)\n"
                     "• username — з @ (напр. <code>@ivan_driver</code>)\n\n"
                     "Свій ID/username водій бачить, написавши боту /start.", _cancel_kb())
    await cb.answer()


@router.message(AddRecipient.tgid)
async def add_allow_do(msg: Message, state: FSMContext, bot: Bot):
    raw = msg.text.strip()
    data = await state.get_data()
    await _delete(msg)
    await state.clear()
    if raw.isdigit():
        await db.add_allow(telegram_id=int(raw))
        prefix = f"✅ Дозволено водія за ID <code>{raw}</code>.\n\n"
    elif raw.lstrip("@"):
        await db.add_allow(username=raw)
        prefix = f"✅ Дозволено водія {('@'+raw.lstrip('@'))}.\n\n"
    else:
        prefix = "⚠️ Введіть числовий ID або @username.\n\n"
    text, kb = await _access_screen()
    await _edit_anchor(bot, msg.chat.id, data["anchor"], prefix + text, kb)


@router.callback_query(F.data == "aldellist")
async def cb_al_del_list(cb: CallbackQuery):
    entries = await db.get_allowlist()
    if not entries:
        await cb.answer("Немає кого прибирати.", show_alert=True)
        return
    rows = []
    for e in entries:
        who = f"@{e['username']}" if e["username"] else f"ID {e['telegram_id']}"
        rows.append([_btn(f"🗑 {who}", f"aldel:{e['id']}")])
    rows.append([_btn("✖️ Назад", "m:users")])
    await _safe_edit(cb.message, "➖ У кого прибрати доступ:", InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@router.callback_query(F.data.startswith("aldel:"))
async def cb_al_del_do(cb: CallbackQuery):
    eid = int(cb.data.split(":")[1])
    entries = await db.get_allowlist()
    entry = next((e for e in entries if e["id"] == eid), None)
    if entry:
        await db.delete_users_matching(entry["telegram_id"], entry["username"])  # stop their alerts + access
        await db.remove_allow(eid)
    text, kb = await _access_screen()
    await _safe_edit(cb.message, "🗑 Доступ прибрано.\n\n" + text, kb)
    await cb.answer("Прибрано")


# ---------------------------------------------------------------------------
# PUESC news (on demand)
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "m:news")
async def cb_news(cb: CallbackQuery):
    await _cleanup_maps(cb.message.chat.id, cb.bot)
    await cb.answer("Завантажую новини…")
    await _safe_edit(cb.message, "📰 Завантажую новини PUESC (SENT)…", None)
    items = await news.fetch_news()
    if not items:
        await _safe_edit(cb.message, "Не вдалося завантажити новини PUESC зараз. Спробуйте пізніше.",
                         InlineKeyboardMarkup(inline_keyboard=[[
                             _btn("🔄 Ще раз", "m:news"), _btn("⬅️ Меню", "m:main")]]))
        return
    lines = ["<b>📰 Новини PUESC — моніторинг перевезень (SENT):</b>\n"]
    for it in items[:6]:
        flag = "🔴 " if it["is_alert"] else "• "
        date = f" ({it['date']})" if it.get("date") else ""
        lines.append(f"{flag}<a href='{it['url']}'>{it['title']}</a>{date}")
    lines.append("\n🔴 = аварія/доступність SENT-GEO")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        _btn("🔄 Оновити", "m:news"), _btn("⬅️ Меню", "m:main")]])
    await _safe_edit(cb.message, "\n".join(lines), kb)


# ---------------------------------------------------------------------------
# Manual check (from trip detail: chk:<id>) -> run -> show in one message
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("chk:"))
async def cb_check_do(cb: CallbackQuery):
    await _cleanup_maps(cb.message.chat.id, cb.bot)
    tid = int(cb.data.split(":")[1])
    trip = await db.get_trip(tid)
    if not trip:
        await _safe_edit(cb.message, "Рейс не знайдено.", _back_kb())
        await cb.answer()
        return
    await cb.answer("Перевіряю…")
    await _safe_edit(cb.message, f"⏳ Перевіряю рейс #{tid} — {trip['vehicle_name']}…", None)

    trackers = await db.get_trackers_for_vehicle(trip["vehicle_id"])
    if not trackers:
        await _safe_edit(cb.message, f"У авто {trip['vehicle_name']} немає трекерів.", _back_kb())
        return

    results = await puesc.check_trip_trackers(trip, trackers)
    missing = sum(1 for r in results if r.status == "signal_missing")
    prov_by_id = {t["id"]: t["provider"] for t in trackers}

    lines = [f"<b>Рейс #{tid} — {trip['vehicle_name']} ({trip['plate_number']})</b>",
             f"RMPD: <code>{trip['rmpd_number']}</code>\n"]
    best = None  # (lat, lon, label) to drop a map pin for
    for r in results:
        icon = {"signal_ok": "✅", "signal_missing": "❌", "invalid_data": "❓",
                "site_error": "🔴", "unknown_response": "❔"}.get(r.status, "❔")
        role = prov_by_id.get(r.tracker_id, "Трекер")
        lp = r.last_position.strftime("%d.%m.%Y %H:%M") if r.last_position else "N/A"
        history = await db.get_check_history(tid, r.tracker_id, limit=20)
        alarm_now, silence_min, moving = scheduler.decide_alarm(r, history)
        await db.save_check(tid, r.tracker_id, r.status, r.last_position, r.message,
                            latitude=r.latitude, longitude=r.longitude, alarm=alarm_now)
        line = f"{icon} <b>{role}</b>: <code>{r.tracker_number}</code> — {r.status}\n   Остання позиція: {lp}"
        if r.latitude is not None and r.longitude is not None:
            line += f"\n   📍 {r.latitude:.5f}, {r.longitude:.5f}"
            # Prefer a working tracker's position for the map pin.
            if best is None or r.status == "signal_ok":
                best = (r.latitude, r.longitude, role)
        if r.status == "signal_missing":
            st = "🚗 рух → ТРИВОГА" if (moving and alarm_now) else \
                 ("🚗 рух, очікування" if moving else "🅿️ стоянка (без тривоги)")
            line += f"\n   ⏱ без сигналу ~{silence_min:.0f} хв — {st}"
        line += f"\n   {r.message}"
        lines.append(line)

    if len(results) > 1:
        ok_count = sum(1 for r in results if r.status == "signal_ok")
        if missing == len(results):
            lines.append("\n🚨 <b>КРИТИЧНО: обидва трекери без сигналу!</b>")
        elif missing > 0 and ok_count > 0:
            lines.append("\n⚠️ Один трекер без сигналу, але інший працює — авто відстежується.")

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        _btn("🔄 Ще раз", f"chk:{tid}"), _btn("⬅️ Рейс", f"trip:{tid}"),
    ]])
    await _safe_edit(cb.message, "\n".join(lines), kb)

    # Show the vehicle position on a map pin (manual check only).
    if best:
        lat, lon, role = best
        key = (cb.message.chat.id, tid)
        try:
            sent = await cb.message.answer_location(latitude=lat, longitude=lon)
            _live_maps[key] = sent.message_id
        except Exception as exc:
            log.warning("answer_location failed: %s", exc)


# ---------------------------------------------------------------------------
# Utils
# ---------------------------------------------------------------------------

async def _delete(msg: Message):
    """Best-effort delete of the user's typed message to keep the chat clean."""
    try:
        await msg.delete()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Fallback for stray text (not in an FSM step)
# ---------------------------------------------------------------------------

@router.message()
async def fallback(msg: Message, state: FSMContext, bot: Bot):
    if await state.get_state() is not None:
        return  # an FSM handler will deal with it
    await _cleanup_maps(msg.chat.id, bot)
    await _delete(msg)
    if _is_admin(msg.from_user.id):
        await msg.answer(_MENU_TITLE_ADMIN, reply_markup=_admin_menu_kb(), parse_mode="HTML")
    elif await db.is_allowed(msg.from_user.id, msg.from_user.username):
        text, kb = await _driver_screen(msg.from_user.id)
        await msg.answer(text, reply_markup=kb, parse_mode="HTML")
    else:
        await msg.answer("🚫 Доступ закрито. Зверніться до адміністратора.\n"
                         f"Ваш ID: <code>{msg.from_user.id}</code>", parse_mode="HTML")
