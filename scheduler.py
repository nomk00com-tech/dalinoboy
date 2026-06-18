"""
Scheduled GPS checks — runs every CHECK_INTERVAL_MINUTES minutes.


Two-stage, vehicle-level, movement-aware alerting (per client requirement):
  - GPS must transmit WHILE DRIVING. When PARKED the locator may legitimately go
    silent (driver may even switch GPS off) — those must NOT trigger alerts.
  - A vehicle is "covered" while AT LEAST ONE of its trackers (main/backup) reports
    a fresh position. It goes "dark" only when ALL trackers are silent.
  - While moving and dark:
      * after WARN_MINUTES (default 30) → ⚠️ "signal lost, stop & wait" notice;
      * after FINE_MINUTES (default 60) → 🚨 "fine" notice if still dark.
  - A position that was already stationary before going silent = parking → no alarm.

Anti-spam:
  - trips.alarm_stage holds 0/1/2. We notify only on stage changes
    (ok→warn, warn→fine, →ok recovered). No repeats in between.
  - PUESC unreachable (site_error): notify admins once per outage, reset on success.
  - invalid_data (wrong RMPD/plate/locator): notify admins once.
"""

import logging
import math
from datetime import datetime as _dt
import os

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import db
import news
import puesc

log = logging.getLogger(__name__)

CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "5"))
# Two-stage, while-moving only:
#   WARN_MINUTES — first "signal lost, stop & wait" notice (default 30).
#   FINE_MINUTES — "fine risk" notice if still no signal (default 60).
WARN_MINUTES = int(os.getenv("WARN_MINUTES", "30"))
FINE_MINUTES = int(os.getenv("FINE_MINUTES", "60"))
MOVE_THRESHOLD_M = float(os.getenv("MOVE_THRESHOLD_M", "500"))  # min displacement counted as "moving"
NEWS_INTERVAL_MINUTES = int(os.getenv("NEWS_INTERVAL_MINUTES", "30"))

# Alarm stages stored in trips.alarm_stage
STAGE_OK, STAGE_WARN, STAGE_FINE = 0, 1, 2

_site_error_notified = False


# ---------------------------------------------------------------------------
# Movement / alarm decision
# ---------------------------------------------------------------------------

def _haversine_m(lat1, lon1, lat2, lon2) -> float:
    """Distance between two lat/lon points in metres."""
    r = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _was_moving(history: list[dict]) -> bool:
    """Was the vehicle moving during its last *reporting* period?

    Looks at the coordinates of the most recent signal_ok checks. If they spread
    out by more than MOVE_THRESHOLD_M, the vehicle was en route when the signal
    dropped. If they are clustered (or unknown), it was standing still (parked).
    Unknown history (<2 reporting points) defaults to **False** — if we have no
    evidence that the vehicle was moving we must NOT trigger an alarm (the client
    requires alerts only while driving; a parked vehicle may legitimately be
    silent).
    """
    pts = []
    for h in history:  # newest first
        if h["status"] == "signal_ok" and h["latitude"] is not None and h["longitude"] is not None:
            pts.append((h["latitude"], h["longitude"]))
        if len(pts) >= 4:
            break
    if len(pts) < 2:
        return False
    max_d = 0.0
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            max_d = max(max_d, _haversine_m(*pts[i], *pts[j]))
    return max_d > MOVE_THRESHOLD_M


def _parse_lp(value) -> "_dt | None":
    """Parse a stored last_position_time (ISO string) back to a datetime."""
    if not value:
        return None
    if isinstance(value, _dt):
        return value
    try:
        return _dt.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def effective_status(result, history: list[dict]) -> str:
    """Robust GPS status — corrects PUESC's freshness call via timestamp advancement.

    PUESC labels a locator 'missing' once its last position is older than
    SIGNAL_FRESH_MINUTES. But the SENT-GEO portal often lags, so a moving,
    perfectly-transmitting vehicle can show a 15-30 min old position. If that
    position TIMESTAMP advanced since the previous check, the locator clearly
    IS transmitting → treat it as signal_ok. This is independent of timezone and
    of GEO lag, so it fixes "map shows movement but text says no signal".

    `history` is newest-first and taken BEFORE the current check is saved, so
    history[0] (with a timestamp) is the previous check.
    """
    if result.status != "signal_missing" or result.last_position is None:
        return result.status
    for h in history:
        prev = _parse_lp(h.get("last_position_time"))
        if prev is not None:
            return "signal_ok" if result.last_position > prev else result.status
    return result.status


def _missing_minutes_fallback(history: list[dict]) -> float:
    """Rough silence duration when PUESC gave no timestamp (e.g. błąd 200):
    leading consecutive signal_missing checks × check interval."""
    n = 0
    for h in history:
        if h["status"] == "signal_missing":
            n += 1
        else:
            break
    return n * CHECK_INTERVAL_MINUTES


def _stage_for(silence_min: float, moving: bool) -> int:
    """Map silence duration (while moving) to an alarm stage."""
    if not moving:
        return STAGE_OK                  # parked → never alarm
    if silence_min >= FINE_MINUTES:
        return STAGE_FINE
    if silence_min >= WARN_MINUTES:
        return STAGE_WARN
    return STAGE_OK


def decide_alarm(result, history: list[dict]) -> tuple[bool, float, bool]:
    """Back-compat helper for the manual check screen: (alarm, silence, moving)."""
    if effective_status(result, history) != "signal_missing":
        return (False, 0.0, False)
    moving = _was_moving(history)
    silence = result.signal_age_min
    if silence is None:
        silence = _missing_minutes_fallback(history) + CHECK_INTERVAL_MINUTES
    stage = _stage_for(silence, moving)
    return (stage >= STAGE_WARN, silence, moving)


# ---------------------------------------------------------------------------
# Main scheduled run
# ---------------------------------------------------------------------------

async def run_scheduled_checks(bot: Bot):
    global _site_error_notified

    trips = await db.get_active_trips()
    if not trips:
        log.debug("Scheduler: no active trips.")
        return

    log.info("Scheduler: checking %d active trip(s)…", len(trips))

    for trip in trips:
        trackers = await db.get_trackers_for_vehicle(trip["vehicle_id"])
        if not trackers:
            continue

        results = await puesc.check_trip_trackers(trip, trackers)

        # PUESC unreachable for this trip?
        if any(r.status == "site_error" for r in results):
            if not _site_error_notified:
                await _notify_admins(
                    bot,
                    "🔴 <b>PUESC niedostępny</b>\n"
                    f"Не вдалося перевірити рейс {trip['id']} — {trip['vehicle_name']}.\n"
                    "Автоматичні перевірки продовжаться.",
                )
                _site_error_notified = True
            log.warning("Site error for trip %d — skipping.", trip["id"])
            continue
        _site_error_notified = False

        # Save every tracker check (history for movement + coords for the map),
        # and gather movement/silence info at the VEHICLE level.
        any_moving = False
        ages = []
        statuses = []
        for r in results:
            history = await db.get_check_history(trip["id"], r.tracker_id, limit=20)
            # Robust status: a still-advancing position counts as transmitting,
            # even if PUESC flagged it stale (GEO lag / timezone skew).
            status = effective_status(r, history)
            statuses.append(status)
            prev_lp = next((_parse_lp(h.get("last_position_time")) for h in history
                            if _parse_lp(h.get("last_position_time")) is not None), None)
            log.info("DIAG trip %s %s: puesc=%s eff=%s czas=%s prev=%s age=%s",
                     trip["id"], r.tracker_number, r.status, status,
                     r.last_position, prev_lp, r.signal_age_min)
            await db.save_check(
                trip["id"], r.tracker_id, status, r.last_position, r.message,
                latitude=r.latitude, longitude=r.longitude, alarm=0,
            )
            if status == "signal_missing":
                if _was_moving(history):
                    any_moving = True
                ages.append(r.signal_age_min if r.signal_age_min is not None
                            else _missing_minutes_fallback(history) + CHECK_INTERVAL_MINUTES)

            # invalid_data (wrong RMPD/number) → tell admins once per tracker.
            if status == "invalid_data":
                prev_status = history[0]["status"] if history else None  # history is pre-save
                if prev_status != "invalid_data":
                    tr = next((t for t in trackers if t["id"] == r.tracker_id), None)
                    await _notify_admins(
                        bot,
                        f"❓ <b>Невірні дані</b>\nРейс — {trip['vehicle_name']}\n"
                        f"Трекер {tr['provider'] if tr else '?'}/{r.tracker_number}\n"
                        f"RMPD: {trip['rmpd_number']}\n\n{r.message}",
                    )

        # Vehicle is "covered" if at least one tracker reports a fresh position.
        covered = any(s == "signal_ok" for s in statuses)
        prev_stage = trip.get("alarm_stage", 0) or 0
        best = _freshest(results)

        if covered:
            new_stage = STAGE_OK
        else:
            silence = min(ages) if ages else 0.0   # how long since the freshest tracker reported
            new_stage = _stage_for(silence, any_moving)
            ages_dbg = f"{silence:.0f}" if ages else "?"
            log.info("Trip %d: dark, moving=%s, silence=%s min -> stage %d",
                     trip["id"], any_moving, ages_dbg, new_stage)

        if new_stage != prev_stage:
            await db.set_trip_alarm_stage(trip["id"], new_stage)
            silence = (min(ages) if ages else 0.0)
            if new_stage == STAGE_WARN and prev_stage < STAGE_WARN:
                await _notify_vehicle(bot, trip["vehicle_id"], _warn_msg(trip, best, silence))
            elif new_stage == STAGE_FINE and prev_stage < STAGE_FINE:
                await _notify_vehicle(bot, trip["vehicle_id"], _fine_msg(trip, best, silence))
            elif new_stage == STAGE_OK and prev_stage >= STAGE_WARN and covered:
                await _notify_vehicle(bot, trip["vehicle_id"], _recovered_msg(trip, best))


# ---------------------------------------------------------------------------
# PUESC news monitor
# ---------------------------------------------------------------------------

async def run_news_check(bot: Bot):
    """Poll the SENT news category; notify recipients about new articles."""
    items = await news.fetch_news()
    if not items:
        return

    first_run = await db.count_news_seen() == 0
    if first_run:
        # Don't blast historical news — just record what's there now.
        for it in items:
            await db.mark_news_seen(it["id"], it["title"])
        log.info("News monitor initialised with %d existing articles.", len(items))
        return

    # Notify oldest-first so the newest ends up last in the chat.
    for it in reversed(items):
        if await db.is_news_seen(it["id"]):
            continue
        await db.mark_news_seen(it["id"], it["title"])
        await _notify_all(bot, _news_msg(it))
        log.info("News: new article %s — %s", it["id"], it["title"][:60])


def _news_msg(it: dict) -> str:
    head = "🔴 <b>УВАГА — новина PUESC (SENT-GEO/аварія)</b>" if it["is_alert"] \
        else "📰 <b>Новина PUESC (SENT/моніторинг)</b>"
    date = f"\n🗓 {it['date']}" if it.get("date") else ""
    return (
        f"{head}\n"
        f"{it['title']}{date}\n"
        f"<a href='{it['url']}'>Читати на puesc.gov.pl</a>"
    )


# ---------------------------------------------------------------------------
# Message formatters
# ---------------------------------------------------------------------------

def _freshest(results):
    """Pick the result with the most recent last_position (for showing the spot)."""
    with_pos = [r for r in results if r.last_position is not None]
    if with_pos:
        return max(with_pos, key=lambda r: r.last_position)
    return results[0] if results else None


def _last_pos_str(r) -> str:
    return r.last_position.strftime("%d.%m.%Y %H:%M") if (r and r.last_position) else "немає даних"


def _warn_msg(trip: dict, r, silence_min: float) -> str:
    return (
        f"⚠️ <b>Зник GPS-сигнал — зупиніться!</b>\n"
        f"Авто: {trip['vehicle_name']} ({trip['plate_number']})\n"
        f"RMPD: {trip['rmpd_number']}\n"
        f"Немає сигналу вже ~{silence_min:.0f} хв.\n"
        f"Остання позиція: {_last_pos_str(r)}\n\n"
        f"❗️ Знайдіть парковку, зупиніться і чекайте, доки зв'язок відновиться. "
        f"Якщо не відновиться ще ~30 хв — буде штраф."
    )


def _fine_msg(trip: dict, r, silence_min: float) -> str:
    return (
        f"🚨 <b>ШТРАФ! Годину без GPS-сигналу під час руху</b>\n"
        f"Авто: {trip['vehicle_name']} ({trip['plate_number']})\n"
        f"RMPD: {trip['rmpd_number']}\n"
        f"Немає сигналу вже ~{silence_min:.0f} хв.\n"
        f"Остання позиція: {_last_pos_str(r)}"
    )


def _recovered_msg(trip: dict, r) -> str:
    return (
        f"✅ <b>GPS-сигнал відновлено</b>\n"
        f"Авто: {trip['vehicle_name']} ({trip['plate_number']})\n"
        f"RMPD: {trip['rmpd_number']}\n"
        f"Остання позиція: {_last_pos_str(r)}"
    )


# ---------------------------------------------------------------------------
# Notification dispatchers
# ---------------------------------------------------------------------------

async def _send_many(bot: Bot, ids, text: str):
    for uid in ids:
        try:
            await bot.send_message(uid, text, parse_mode="HTML")
        except Exception as exc:
            log.warning("Cannot send to %s: %s", uid, exc)


async def _notify_vehicle(bot: Bot, vehicle_id: int, text: str):
    """GPS alerts: go to all admins + the drivers assigned to THIS vehicle only."""
    ids = set(_admin_ids())
    for d in await db.get_drivers_for_vehicle(vehicle_id):
        ids.add(d["telegram_id"])
    await _send_many(bot, ids, text)


async def _notify_all(bot: Bot, text: str):
    """Broad notifications (news): all recipients + all admins."""
    ids = set(_admin_ids())
    for user in await db.get_notification_recipients():
        ids.add(user["telegram_id"])
    await _send_many(bot, ids, text)


async def _notify_admins(bot: Bot, text: str):
    for admin_id in _admin_ids():
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML")
        except Exception as exc:
            log.warning("Cannot send to admin %s: %s", admin_id, exc)


def _admin_ids() -> list[int]:
    raw = os.getenv("ADMIN_IDS", "")
    return [int(x.strip()) for x in raw.split(",") if x.strip().isdigit()]


# ---------------------------------------------------------------------------
# Scheduler setup
# ---------------------------------------------------------------------------

def start_scheduler(bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        run_scheduled_checks,
        trigger="interval",
        minutes=CHECK_INTERVAL_MINUTES,
        args=[bot],
        id="puesc_check",
        name="PUESC GPS check",
        replace_existing=True,
        misfire_grace_time=60,
        next_run_time=_dt.now(),       # run immediately on deploy
    )
    scheduler.add_job(
        run_news_check,
        trigger="interval",
        minutes=NEWS_INTERVAL_MINUTES,
        args=[bot],
        id="puesc_news",
        name="PUESC news monitor",
        replace_existing=True,
        misfire_grace_time=120,
        next_run_time=_dt.now(),       # run immediately on deploy
    )
    scheduler.start()
    log.info("Scheduler started — GPS every %d min (warn %d min / fine %d min lost-while-moving), news every %d min.",
             CHECK_INTERVAL_MINUTES, WARN_MINUTES, FINE_MINUTES, NEWS_INTERVAL_MINUTES)
    return scheduler
