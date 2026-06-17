"""
PUESC news monitor — watches the "Przewóz towarów objęty monitorowaniem" (SENT)
news category and surfaces new articles, especially SENT-GEO outages.

Why this matters (per client): when PUESC announces a SENT-GEO breakdown
("awaria"), driving without live tracking may be temporarily allowed; when they
announce the end of the outage, tracking is required again. The bot forwards
these announcements so drivers/dispatchers react in time.

Public API:
    fetch_news() -> list[dict]   # [{id, title, date, url, is_alert}], newest first
"""

import logging
import os

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

# Category "Przewóz towarów objęty monitorowaniem" (SENT). Override via .env if PUESC changes it.
NEWS_CATEGORY_URL = os.getenv(
    "NEWS_CATEGORY_URL",
    "https://puesc.gov.pl/aktualnosci/-/categories/603451585?p_r_p_resetCur=true&p_r_p_categoryId=603451585",
)

_ARTICLE_URL = (
    "https://puesc.gov.pl/aktualnosci?"
    "p_p_id=seaplfptlnewspublisher_WAR_seaplfptlnewspublisher&p_p_lifecycle=0&"
    "_seaplfptlnewspublisher_WAR_seaplfptlnewspublisher_action=showArticle&"
    "_seaplfptlnewspublisher_WAR_seaplfptlnewspublisher_articleId={id}"
)

HEADLESS = os.getenv("PUESC_HEADLESS", "1") != "0"

# Words that mark an operational/availability announcement worth highlighting.
ALERT_KEYWORDS = [
    "awari",            # awaria / awarii
    "niedostęp", "niedostep",
    "wstrzyman",        # wstrzymanie
    "przywróc", "przywroc",
    "zakończenie awarii", "zakonczenie awarii",
    "bez lokaliz", "bez monitorow",
]

# JS that returns deduped articles: id + shortest heading title + nearby date.
_JS_EXTRACT = """
() => {
  const byId = {};
  document.querySelectorAll('a[href*="articleId"]').forEach(a => {
    const m = a.href.match(/articleId=(\\d+)/); if (!m) return;
    const id = m[1];
    let txt = (a.innerText || '').trim().replace(/^Więcej\\s+na temat\\s*/i, '');
    if (txt.length < 8) return;
    // climb to a container that has a date
    let node = a, date = null;
    for (let i = 0; i < 6 && node; i++) {
      node = node.parentElement;
      const dm = node && (node.innerText || '').match(/\\d{2}\\.\\d{2}\\.\\d{4}/);
      if (dm) { date = dm[0]; break; }
    }
    // Prefer the heading (shortest meaningful text) over the long abstract link.
    if (!byId[id] || txt.length < byId[id].title.length) {
      byId[id] = { id, title: txt.slice(0, 200), date };
    } else if (byId[id] && !byId[id].date && date) {
      byId[id].date = date;
    }
  });
  return Object.values(byId);
}
"""


def _is_alert(title: str) -> bool:
    low = title.lower()
    return any(k in low for k in ALERT_KEYWORDS)


async def fetch_news() -> list[dict]:
    """Load the SENT news category and return parsed articles (newest first)."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=HEADLESS)
        ctx = await browser.new_context(locale="pl-PL")
        page = await ctx.new_page()
        try:
            await page.goto(NEWS_CATEGORY_URL, timeout=30_000)
            await page.wait_for_selector("a[href*='articleId']", timeout=20_000)
            raw = await page.evaluate(_JS_EXTRACT)
        except PlaywrightTimeoutError:
            log.warning("News page timeout.")
            return []
        except Exception as exc:
            log.warning("News fetch error: %s", exc)
            return []
        finally:
            await browser.close()

    items = []
    for r in raw:
        items.append({
            "id": r["id"],
            "title": r["title"],
            "date": r.get("date"),
            "url": _ARTICLE_URL.format(id=r["id"]),
            "is_alert": _is_alert(r["title"]),
        })
    return items
