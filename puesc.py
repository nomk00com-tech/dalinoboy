"""
PUESC/SENT GPS signal checker — used by both the standalone checker
and the Telegram bot scheduler.

Public API:
    check_trip_trackers(trip, trackers) -> list[CheckResult]
    check_one(rmpd, plate, tracker_number) -> CheckResult
"""

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

# PUESC prints all timestamps in Warsaw local time. The server may run in UTC
# (e.g. Railway), so a naive datetime.now() would be skewed by 1-2 h and make a
# fresh GPS position look "stale" → false signal_missing. We anchor "now" to
# Warsaw instead. (tzdata is pinned in requirements so zoneinfo works on slim
# images and Windows, which ship no system tz database.)
try:
    from zoneinfo import ZoneInfo
    _WARSAW_TZ = ZoneInfo("Europe/Warsaw")
except Exception:  # pragma: no cover — tzdata/zoneinfo unavailable
    _WARSAW_TZ = None


def _warsaw_now() -> datetime:
    """Current Warsaw wall-clock time as a naive datetime (matches PUESC pages)."""
    if _WARSAW_TZ is not None:
        return datetime.now(_WARSAW_TZ).replace(tzinfo=None)
    # Fallback if tzdata is missing: approximate Poland's CET/CEST DST manually.
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    def _last_sunday(year: int, month: int) -> datetime:
        d = datetime(year, month, 31)
        return d - timedelta(days=(d.weekday() + 1) % 7)

    dst_start = _last_sunday(now.year, 3).replace(hour=1)    # 01:00 UTC
    dst_end = _last_sunday(now.year, 10).replace(hour=1)
    offset = 2 if dst_start <= now < dst_end else 1
    return now + timedelta(hours=offset)

# RMPD-406 form is public — "Usługa jest dostępna bez logowania" (no login needed).
FORM_URL = (
    "https://puesc.gov.pl/web/guest/uslugi/"
    "przewoz-towarow-objety-monitorowaniem/"
    "rmpd-406?systemName=SENT&formName=1000972"
)

# A locator that hasn't reported within this many minutes is considered "not
# transmitting right now" (signal_missing). The scheduler then decides — based on
# movement + duration — whether that actually warrants an alert. Device normally
# reports every few minutes, so ~15 min of silence means it stopped.
SIGNAL_FRESH_MINUTES = int(os.getenv("SIGNAL_FRESH_MINUTES", "20"))

# Run the browser headless. checker.py --headed flips this to False for debugging.
HEADLESS = os.getenv("PUESC_HEADLESS", "1") != "0"

# Stable selectors verified against the live RMPD-406 form (2026-06).
SEL_RMPD = "#_Sent_Rmpd406Portlet_rmpdNumber"
SEL_PLATE = "#_Sent_Rmpd406Portlet_truckNumber"
SEL_LOCATOR = "#_Sent_Rmpd406Portlet_geoLocatorNumber"
SEL_SUBMIT = "[data-sent-role='command-submit']"   # id suffix is random per session
SEL_RESULT = "h1:has-text('RMPD416')"               # result heading appears on success
SEL_ALERT = "[role='alert'], .alert"                # error banner "Błąd: ..."

# Locator number format enforced by PUESC XSD:
# [Z|U|M] + 2 digits + '-' + 2 letters + 2 digits + 2 letters + '-' + 1 digit
LOCATOR_RE = re.compile(
    r"^[ZUM][0-9]{2}-[ABCEFGHKMNPRSTWXYZ]{2}[0-9]{2}[ABCEFGHKMNPRSTWXYZ]{2}-[0-9]$"
)


@dataclass
class CheckResult:
    rmpd: str
    plate: str
    tracker_number: str
    tracker_id: int
    status: str            # signal_ok | signal_missing | invalid_data | site_error | unknown_response
    last_position: datetime | None
    message: str
    raw_text: str
    latitude: float | None = None
    longitude: float | None = None
    signal_age_min: float | None = None   # minutes since last GPS report (page clock)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_datetime_pl(text: str) -> datetime | None:
    # Normalise the PUESC "godz." notation: "10.06.2026, godz.12:35:26" -> "10.06.2026 12:35:26"
    t = text.strip().replace(",", " ")
    t = re.sub(r"\bgodz\.?\s*", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    for fmt in [
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
        "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M",
    ]:
        try:
            return datetime.strptime(t, fmt)
        except ValueError:
            pass
    return None


def _make_result(
    rmpd: str, plate: str, tracker_number: str, tracker_id: int,
    status: str, last_position, raw_text: str, message: str,
    latitude=None, longitude=None, signal_age_min=None,
) -> CheckResult:
    return CheckResult(
        rmpd=rmpd, plate=plate,
        tracker_number=tracker_number, tracker_id=tracker_id,
        status=status, last_position=last_position,
        message=message, raw_text=raw_text,
        latitude=latitude, longitude=longitude,
        signal_age_min=signal_age_min,
    )


async def _dismiss_cookies(page):
    """Close the Angular cookie banner that intercepts clicks (best-effort)."""
    try:
        await page.evaluate(
            "() => { const c = document.querySelector('ang-cookies');"
            " if (c) { const b = c.querySelector('button'); if (b) b.click(); else c.remove(); } }"
        )
    except Exception:
        pass


def _interpret(page_text: str, base: dict) -> CheckResult:
    """Map the RMPD-406 result page text to a CheckResult.

    Real response shapes (verified on the live portal, 2026-06):
      * success  — heading 'RMPD416 ...', 'Status zgłoszenia: <X>',
                   'Ostatnia zapisana w GEO lokalizacja ...' + coordinates +
                   'Czas: DD.MM.YYYY, godz.HH:MM:SS'
      * no signal — error 'błąd 200' (per the client: correct data, but the
                   locator is not transmitting) → signal_missing
      * bad data  — alert 'Błąd: ... not found' (wrong RMPD/plate/locator)
                   or 'Błąd: ... niezgodny ze schematem XSD' (bad locator format)
    """
    low = page_text.lower()

    # --- No-signal error: PUESC returns "błąd 200" when the locator isn't sending.
    # Data is correct, the device just isn't transmitting → treat as signal_missing.
    if re.search(r"b[łl][ąa]d[\s:]*200", low):
        return _make_result(**base, status="signal_missing", last_position=None,
                            raw_text=page_text[:500],
                            message="PUESC błąd 200 — lokalizator nie przesyła sygnału.")

    # --- Wrong data / bad format (form stays, shows a 'Błąd:' alert) ---
    if "not found" in low or "niezgodny ze schematem xsd" in low or "cvc-pattern-valid" in low:
        msg = "PUESC: dane nie znalezione (RMPD/nr auta/lokalizator) lub zły format."
        return _make_result(**base, status="invalid_data", last_position=None,
                            raw_text=page_text[:500], message=msg)

    # --- Success view present? ---
    has_result = "rmpd416" in low or "numer referencyjny zgłoszenia" in low

    # Reference "now" — the page prints current Warsaw time; using it avoids any
    # timezone mismatch between our clock and the GPS timestamp.
    now_ref = None
    m_now = re.search(r"strefa czasowa warszawa\)\s*:?\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", low)
    if m_now:
        now_ref = _parse_datetime_pl(m_now.group(1))
    if now_ref is None:
        now_ref = _warsaw_now()

    # Status zgłoszenia (Kompletne / Niekompletne / Zamknięte ...)
    decl_status = None
    m_st = re.search(r"status zgłoszenia\s*:?\s*\n?\s*([A-Za-zżźćńółęąśŻŹĆŃÓŁĘĄŚ ]+)", page_text, re.I)
    if m_st:
        decl_status = m_st.group(1).strip().split("\n")[0].strip()

    # Last GPS position time: "Czas: 10.06.2026, godz.12:35:26"
    last_position = None
    m_czas = re.search(r"Czas\s*:?\s*(\d{2}\.\d{2}\.\d{4}[, ]+godz\.?\s*\d{2}:\d{2}:\d{2})", page_text, re.I)
    if m_czas:
        last_position = _parse_datetime_pl(m_czas.group(1))

    # Coordinates: "Szerokość geograficzna: 51.3809360000" / "Długość geograficzna: 21.1242050000"
    lat = lon = None
    m_lat = re.search(r"szerokość geograficzna\s*:?\s*(-?\d+[.,]\d+)", page_text, re.I)
    m_lon = re.search(r"długość geograficzna\s*:?\s*(-?\d+[.,]\d+)", page_text, re.I)
    if m_lat:
        lat = float(m_lat.group(1).replace(",", "."))
    if m_lon:
        lon = float(m_lon.group(1).replace(",", "."))
    has_coords = lat is not None and lon is not None

    if has_result:
        # Result page rendered. Decide based on presence/freshness of GPS data.
        if has_coords and last_position:
            age_min = (now_ref - last_position).total_seconds() / 60
            if age_min > SIGNAL_FRESH_MINUTES:
                msg = (f"Lokalizator nie przesyła od {age_min:.0f} min "
                       f"(ostatnio {last_position:%d.%m %H:%M}). Status: {decl_status or '?'}.")
                return _make_result(**base, status="signal_missing", last_position=last_position,
                                    raw_text=page_text[:500], message=msg,
                                    latitude=lat, longitude=lon, signal_age_min=age_min)
            msg = f"GPS OK. Ostatnia pozycja: {last_position:%d.%m.%Y %H:%M}. Status: {decl_status or '?'}."
            return _make_result(**base, status="signal_ok", last_position=last_position,
                                raw_text=page_text[:500], message=msg,
                                latitude=lat, longitude=lon, signal_age_min=age_min)

        if has_coords and not last_position:
            # Coordinates but no parseable time — treat as OK, flag for review.
            return _make_result(**base, status="signal_ok", last_position=None,
                                raw_text=page_text[:500], latitude=lat, longitude=lon,
                                message=f"GPS pozycja jest, brak czasu. Status: {decl_status or '?'}.")

        # Result page but NO coordinates → locator not transmitting to GEO.
        return _make_result(**base, status="signal_missing", last_position=None,
                            raw_text=page_text[:500],
                            message=f"Brak pozycji GPS w GEO. Status zgłoszenia: {decl_status or '?'}.")

    return _make_result(**base, status="unknown_response", last_position=None,
                        raw_text=page_text[:500], message="Nie rozpoznano odpowiedzi PUESC.")


async def _check_one_page(page, rmpd: str, plate: str, tracker_number: str, tracker_id: int) -> CheckResult:
    base = dict(rmpd=rmpd, plate=plate, tracker_number=tracker_number, tracker_id=tracker_id)

    # Cheap client-side guard: reject obviously malformed locator numbers up front.
    if not LOCATOR_RE.match(tracker_number.strip()):
        log.warning("Locator %s does not match PUESC format", tracker_number)
        return _make_result(**base, status="invalid_data", last_position=None, raw_text="",
                            message=f"Numer lokalizatora '{tracker_number}' ma zły format (oczekiwane np. Z21-AF67XZ-8).")

    try:
        log.info("Checking RMPD=%s plate=%s tracker=%s", rmpd, plate, tracker_number)
        await page.goto(FORM_URL, timeout=30_000)
        await page.wait_for_selector(SEL_RMPD, timeout=20_000)
        await _dismiss_cookies(page)
    except PlaywrightTimeoutError:
        return _make_result(**base, status="site_error", last_position=None, raw_text="", message="Form page timeout.")
    except Exception as exc:
        return _make_result(**base, status="site_error", last_position=None, raw_text="", message=str(exc))

    try:
        await page.fill(SEL_RMPD, rmpd.strip())
        await page.fill(SEL_PLATE, plate.strip())
        await page.fill(SEL_LOCATOR, tracker_number.strip())
        await _dismiss_cookies(page)
        await page.click(SEL_SUBMIT)
        # Submit triggers a navigation to the RMPD416 result OR shows a 'Błąd:' alert.
        # Don't wait for networkidle — the result page keeps the map/Angular sockets
        # busy, so networkidle never fires. Wait for the concrete markers instead.
        await page.wait_for_selector(f"{SEL_RESULT}, {SEL_ALERT}", timeout=30_000)
        await asyncio.sleep(1)   # let Angular paint the values
    except PlaywrightTimeoutError:
        return _make_result(**base, status="site_error", last_position=None, raw_text="", message="Form submit timeout.")
    except Exception as exc:
        log.warning("Form fill error for RMPD=%s: %s", rmpd, exc)
        return _make_result(**base, status="site_error", last_position=None, raw_text="", message=str(exc))

    try:
        page_text = await page.inner_text("main")
    except Exception:
        try:
            page_text = await page.inner_text("body")
        except Exception as exc:
            return _make_result(**base, status="site_error", last_position=None, raw_text="", message=f"Read error: {exc}")

    return _interpret(page_text, base)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def check_one(rmpd: str, plate: str, tracker_number: str, tracker_id: int = 0) -> CheckResult:
    """Check a single tracker. Opens a browser, checks, closes (no login)."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=HEADLESS)
        # Force the browser timezone to Warsaw: PUESC renders the GPS position
        # time ("Czas:") in the BROWSER's local timezone via JavaScript. On a UTC
        # server (Railway) that made fresh positions look ~2 h stale → false
        # "no signal". Pinning the browser tz to Warsaw keeps it consistent with
        # the page's Warsaw "now", so the signal age is computed correctly.
        ctx = await browser.new_context(locale="pl-PL", timezone_id="Europe/Warsaw")
        page = await ctx.new_page()
        try:
            return await _check_one_page(page, rmpd, plate, tracker_number, tracker_id)
        finally:
            await browser.close()


async def check_trip_trackers(trip: dict, trackers: list[dict]) -> list[CheckResult]:
    """
    Check all trackers for one trip in a single browser session.

    trip     — dict with keys: rmpd_number, plate_number, vehicle_name
    trackers — list of dicts with keys: id, tracker_number, provider
    """
    if not trackers:
        return []

    results: list[CheckResult] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=HEADLESS)
        # Force the browser timezone to Warsaw: PUESC renders the GPS position
        # time ("Czas:") in the BROWSER's local timezone via JavaScript. On a UTC
        # server (Railway) that made fresh positions look ~2 h stale → false
        # "no signal". Pinning the browser tz to Warsaw keeps it consistent with
        # the page's Warsaw "now", so the signal age is computed correctly.
        ctx = await browser.new_context(locale="pl-PL", timezone_id="Europe/Warsaw")
        page = await ctx.new_page()
        try:
            for t in trackers:
                r = await _check_one_page(
                    page,
                    trip["rmpd_number"],
                    trip["plate_number"],
                    t["tracker_number"],
                    t["id"],
                )
                results.append(r)
                # Short pause between requests to be polite to the server
                await asyncio.sleep(2)

        finally:
            await browser.close()

    return results
