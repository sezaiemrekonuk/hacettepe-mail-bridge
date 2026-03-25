"""
Entry point for the Hacettepe mail bridge.

Modes
-----
  python -m src.main          – start the scheduler (normal operation)
  python -m src.main --auth   – open a visible browser for one-time login / MFA
"""
import argparse
import logging
import os
import sys
import time

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from src.auth import ensure_logged_in
from src.db import is_seen, mark_seen
from src.forwarder import forward_message
from src.scraper import scrape_new_messages

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("main")

# ------------------------------------------------------------------ #
# Config
# ------------------------------------------------------------------ #
HU_EMAIL        = os.environ["HU_EMAIL"]
HU_PASSWORD     = os.environ["HU_PASSWORD"]
GMAIL_SENDER    = os.environ["GMAIL_SENDER"]
GMAIL_APP_PASS  = os.environ["GMAIL_APP_PASSWORD"]
GMAIL_TARGET    = os.environ["GMAIL_TARGET"]
POLL_INTERVAL   = int(os.environ.get("POLL_INTERVAL", "900"))
HEADLESS        = os.environ.get("HEADLESS", "1") not in ("0", "false", "False")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_USER_DATA_DIR = os.path.join(BASE_DIR, ".playwright", "user_data")

# Playwright uses this folder to persist cookies/session state between runs.
# Default to a writable path inside the repo (so local dev works out of the box).
_user_data_dir_env = os.environ.get("USER_DATA_DIR")
if _user_data_dir_env:
    USER_DATA_DIR = (
        _user_data_dir_env
        if os.path.isabs(_user_data_dir_env)
        else os.path.join(BASE_DIR, _user_data_dir_env)
    )
else:
    USER_DATA_DIR = DEFAULT_USER_DATA_DIR

try:
    os.makedirs(USER_DATA_DIR, exist_ok=True)
except OSError as exc:
    logger.warning(
        "Could not create USER_DATA_DIR=%s (will likely fail): %s", USER_DATA_DIR, exc
    )


def poll_once(page) -> None:
    """One full cycle: ensure login → scrape → forward."""
    try:
        ensure_logged_in(page, HU_EMAIL, HU_PASSWORD)
    except RuntimeError as exc:
        logger.error("Login failed: %s", exc)
        return

    try:
        new_msgs = scrape_new_messages(page, is_seen, mark_seen)
    except Exception as exc:
        logger.error("Scrape failed: %s", exc, exc_info=True)
        return
    logger.info("%d new message(s) found", len(new_msgs))

    for msg in new_msgs:
        try:
            forward_message(
                msg,
                smtp_user=GMAIL_SENDER,
                smtp_password=GMAIL_APP_PASS,
                target_address=GMAIL_TARGET,
            )
        except Exception as exc:
            logger.error("Failed to forward '%s': %s", msg.subject, exc)


def run_scheduler(headless: bool = True) -> None:
    logger.info(
        "Starting mail bridge (poll every %ds, headless=%s)", POLL_INTERVAL, headless
    )
    with sync_playwright() as pw:
        browser = pw.chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR,
            headless=headless,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
        )
        page = browser.pages[0] if browser.pages else browser.new_page()

        try:
            while True:
                logger.info("--- Poll cycle starting ---")
                poll_once(page)
                logger.info("--- Poll cycle done; sleeping %ds ---", POLL_INTERVAL)
                time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            logger.info("Interrupted – shutting down")
        finally:
            browser.close()


def run_auth_session() -> None:
    """
    Open a visible (non-headless) browser, navigate to the mail portal,
    and wait for the user to complete login + any MFA challenge manually.
    The session cookie is saved to USER_DATA_DIR for later headless runs.
    """
    logger.info(
        "Auth mode – a browser window will open.\n"
        "  1. Complete the login (and MFA if prompted).\n"
        "  2. Once you see the inbox, press ENTER in this terminal to save the session."
    )
    with sync_playwright() as pw:
        browser = pw.chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR,
            headless=False,
            args=["--no-sandbox"],
        )
        page = browser.pages[0] if browser.pages else browser.new_page()
        page.goto("https://posta.hacettepe.edu.tr", wait_until="networkidle", timeout=60_000)

        input("\nPress ENTER after you have successfully logged in and can see your inbox…\n")
        logger.info("Session saved to %s – you can now run in headless mode.", USER_DATA_DIR)
        browser.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Hacettepe mail bridge")
    parser.add_argument(
        "--auth",
        action="store_true",
        help="Run an interactive auth session to set up / refresh the login cookie",
    )
    args = parser.parse_args()

    if args.auth:
        run_auth_session()
    else:
        run_scheduler(headless=HEADLESS)


if __name__ == "__main__":
    main()
