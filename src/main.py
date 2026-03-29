"""
Entry point. Runs the FastAPI web server (which spawns the scraper loop as a background thread).

  python -m src.main          – start web server + scraper
  python -m src.main --auth <hu_email>  – interactive auth for one account
"""
import argparse
import datetime
import logging
import os
import time
import threading

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from src.auth import ensure_logged_in
from src.forwarder import forward_message
from src.scraper import scrape_new_messages

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("main")

POLL_INTERVAL  = int(os.environ.get("POLL_INTERVAL", "900"))
HEADLESS       = os.environ.get("HEADLESS", "1") not in ("0", "false", "False")
GMAIL_SENDER   = os.environ.get("GMAIL_SENDER", "")
GMAIL_APP_PASS = os.environ.get("GMAIL_APP_PASSWORD", "")

BASE_DIR           = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
USER_DATA_BASE_DIR = os.path.join(BASE_DIR, "user_data")

# Per-user fetch state: {app_id: {"time": "...", "status": "ok"|"running"|"error: ..."}}
_fetch_state: dict[int, dict] = {}
_fetch_lock = threading.Lock()


def _now() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")


def get_fetch_state() -> dict[int, dict]:
    with _fetch_lock:
        return dict(_fetch_state)


def _set_fetch_state(app_id: int, status: str) -> None:
    with _fetch_lock:
        _fetch_state[app_id] = {"time": _now(), "status": status}


def _user_data_dir(app_id: int) -> str:
    d = os.path.join(USER_DATA_BASE_DIR, str(app_id))
    os.makedirs(d, exist_ok=True)
    return d


def poll_user(pw, user_row) -> None:
    """Run one full scrape+forward cycle for a single user."""
    from src.web.db import is_seen, mark_seen, decrypt_password
    app_id       = user_row["id"]
    hu_email     = user_row["hu_email"]
    hu_pass      = decrypt_password(user_row["hu_password_enc"])
    gmail_target = user_row["gmail_target"]

    _set_fetch_state(app_id, "running")
    udd = _user_data_dir(app_id)
    try:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=udd,
            headless=HEADLESS,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            ensure_logged_in(page, hu_email, hu_pass)
        except RuntimeError as exc:
            logger.error("[%s] Login failed: %s", hu_email, exc)
            _set_fetch_state(app_id, f"error: login failed")
            ctx.close()
            return

        def _is_seen(msg_id):   return is_seen(app_id, msg_id)
        def _mark_seen(msg_id): return mark_seen(app_id, msg_id)

        try:
            new_msgs = scrape_new_messages(page, _is_seen, _mark_seen)
        except Exception as exc:
            logger.error("[%s] Scrape failed: %s", hu_email, exc)
            _set_fetch_state(app_id, f"error: scrape failed")
            ctx.close()
            return

        logger.info("[%s] %d new message(s)", hu_email, len(new_msgs))
        for msg in new_msgs:
            try:
                forward_message(
                    msg,
                    smtp_user=GMAIL_SENDER,
                    smtp_password=GMAIL_APP_PASS,
                    target_address=gmail_target,
                )
            except Exception as exc:
                logger.error("[%s] Forward failed '%s': %s", hu_email, msg.subject, exc)
        ctx.close()
        _set_fetch_state(app_id, f"ok ({len(new_msgs)} new)")
    except Exception as exc:
        logger.error("[%s] Unexpected error: %s", hu_email, exc)
        _set_fetch_state(app_id, f"error: {exc}")


def trigger_fetch_async(app_id: int | None = None) -> None:
    """Trigger an immediate fetch in a background thread (non-blocking)."""
    from src.web.db import list_applications

    def _run():
        users = list_applications(status="active")
        if app_id is not None:
            users = [u for u in users if u["id"] == app_id]
        if not users:
            return
        with sync_playwright() as pw:
            for user in users:
                poll_user(pw, user)

    threading.Thread(target=_run, daemon=True).start()


def run_scraper_loop() -> None:
    """Background thread: poll all active users every POLL_INTERVAL seconds."""
    from src.web.db import list_applications
    logger.info("Scraper loop started (interval=%ds, headless=%s)", POLL_INTERVAL, HEADLESS)
    while True:
        try:
            users = list_applications(status="active")
            if not users:
                logger.info("No active users, sleeping.")
            else:
                logger.info("Polling %d active user(s) …", len(users))
                with sync_playwright() as pw:
                    for user in users:
                        poll_user(pw, user)
        except Exception as exc:
            logger.error("Scraper loop error: %s", exc)
        time.sleep(POLL_INTERVAL)


def run_auth_session(hu_email: str) -> None:
    """
    Establish/refresh the Playwright session for one HU account.

    Runs headless (the VPS has no display server).  The automated ADFS login
    flow is used, which works as long as MFA is not required.  If MFA is
    enforced on the account this command will fail — in that case you need to
    run it locally with HEADLESS=0 after copying user_data back from the VPS.
    """
    from src.web.db import list_applications, decrypt_password
    from src.web.db import init_db
    init_db()
    users = [u for u in list_applications() if u["hu_email"] == hu_email]
    if not users:
        print(f"No application found for {hu_email}.")
        print("Add the account via the web UI first, then run --auth.")
        return
    user = users[0]
    hu_pass = decrypt_password(user["hu_password_enc"])
    udd = _user_data_dir(user["id"])
    print(f"Logging in for {hu_email} (headless, user_data: {udd}) …")
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=udd,
            headless=True,   # VPS has no display — always headless here
            args=["--no-sandbox", "--disable-dev-shm-usage"],
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            ensure_logged_in(page, hu_email, hu_pass)
            print(f"Login successful for {hu_email}. Session saved to {udd}")
        except Exception as exc:
            print(f"Login failed: {exc}")
        finally:
            ctx.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Hacettepe mail bridge")
    parser.add_argument("--auth", metavar="HU_EMAIL", help="Interactive auth for one account")
    args = parser.parse_args()

    if args.auth:
        load_dotenv()
        run_auth_session(args.auth)
    else:
        import uvicorn
        from src.web.app import app
        uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")


if __name__ == "__main__":
    main()
