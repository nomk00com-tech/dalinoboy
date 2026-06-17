"""
Phase 1 — standalone PUESC/SENT RMPD-406 checker (CLI).

Thin wrapper around puesc.check_one() so the checker and the bot share ONE
verified implementation (selectors + result parsing live in puesc.py).

Usage:
    python checker.py --rmpd RMPD20260607000433 --plate BC8849PO --tracker Z21-AF67XZ-8
    python checker.py            # uses TEST_* values from .env
    python checker.py --headed   # show the browser (debugging)

Statuses printed:
    signal_ok          — RMPD valid, GPS position present and fresh
    signal_missing     — RMPD valid but no GPS position / position too old
    invalid_data       — RMPD/plate/locator not found, or bad locator format
    site_error         — PUESC unreachable / timeout
    unknown_response   — response received but not recognised

The RMPD-406 form is PUBLIC — "Usługa jest dostępna bez logowania" (no account
needed). It is JS-rendered, so Playwright drives a real browser.
"""

import argparse
import asyncio
import logging
import os
import sys

from dotenv import load_dotenv

import puesc

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


async def _main():
    parser = argparse.ArgumentParser(description="PUESC/SENT RMPD-406 GPS checker")
    parser.add_argument("--rmpd",    default=os.getenv("TEST_RMPD",    ""), help="RMPD declaration number")
    parser.add_argument("--plate",   default=os.getenv("TEST_PLATE",   ""), help="Vehicle registration plate")
    parser.add_argument("--tracker", default=os.getenv("TEST_TRACKER", ""), help="GPS locator number, e.g. Z21-AF67XZ-8")
    parser.add_argument("--headed",  action="store_true", help="Show the browser window (debugging)")
    args = parser.parse_args()

    if not args.rmpd or not args.plate or not args.tracker:
        print("ERROR: --rmpd, --plate and --tracker are required "
              "(or set TEST_RMPD/TEST_PLATE/TEST_TRACKER in .env).")
        sys.exit(1)

    if args.headed:
        # Toggle puesc's browser to visible for this run.
        puesc.HEADLESS = False

    log.info("Checking: RMPD=%s  plate=%s  tracker=%s", args.rmpd, args.plate, args.tracker)
    result = await puesc.check_one(args.rmpd, args.plate, args.tracker)

    print("\n" + "=" * 60)
    print(f"STATUS  : {result.status}")
    print(f"LAST POS: {result.last_position or 'N/A'}")
    print(f"MESSAGE : {result.message}")
    print("=" * 60)

    if result.raw_text:
        print("\nRAW RESPONSE EXCERPT:")
        print(result.raw_text[:600])


if __name__ == "__main__":
    asyncio.run(_main())
