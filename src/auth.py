"""
Handles Microsoft SSO login for posta.hacettepe.edu.tr (Outlook Web App).

The browser context uses a persistent user_data_dir so the session cookie
survives restarts.  On the very first run (or after a session expiry) the
script drives the full login flow automatically.  If MFA is required the
caller should set HEADLESS=0 and complete the challenge interactively.
"""
import logging
import os

from playwright.sync_api import Page, TimeoutError as PWTimeout

logger = logging.getLogger(__name__)

MAIL_URL = "https://posta.hacettepe.edu.tr"
# OWA lands here after a successful login
INBOX_URL_FRAGMENT = "/owa/"


def is_logged_in(page: Page) -> bool:
    """Return True if we are already looking at the OWA inbox."""
    return INBOX_URL_FRAGMENT in page.url


def login(page: Page, email: str, password: str) -> None:
    """
    Drive the Microsoft SSO flow:
      1. Navigate to the portal
      2. Enter username  → click Next
      3. Enter password  → click Sign in
      4. Dismiss 'Stay signed in?' prompt
    """
    logger.info("Navigating to %s", MAIL_URL)
    page.goto(MAIL_URL, wait_until="networkidle", timeout=60_000)

    if is_logged_in(page):
        logger.info("Already logged in (session cookie still valid)")
        return

    # ------------------------------------------------------------------ #
    # Hacettepe ADFS (sso.hacettepe.edu.tr/adfs/ls/) is a single-page
    # paginated form. Both #usernamePage and #passwordPage exist in the DOM
    # from the start; JavaScript toggles display:none/block between them.
    #
    # Flow:
    #   1. Fill #userNameInput → click span#nextButton (JS, no page reload)
    #   2. Wait for #passwordPage to become visible → fill #passwordInput
    #   3. Click span#submitButton  (calls Login.submitLoginRequest())
    #   4. Wait for OWA redirect
    # ------------------------------------------------------------------ #

    # Step 1 – username
    try:
        logger.info("Waiting for ADFS login form at %s …", page.url)
        page.wait_for_selector("#userNameInput", state="visible", timeout=30_000)
        logger.info("Entering username …")
        page.fill("#userNameInput", email)
        page.click("span#nextButton")   # JS pagination – reveals password div
    except PWTimeout:
        logger.error("Username field / Next button not found. URL: %s", page.url)
        raise RuntimeError(
            f"Could not find the ADFS username field within 30s (URL: {page.url}). "
            "Run --auth locally to log in manually."
        )

    # Step 2 – password (revealed by JS after clicking Next)
    try:
        logger.info("Waiting for password page to appear …")
        page.wait_for_selector("#passwordInput", state="visible", timeout=10_000)
        logger.info("Entering password …")
        page.fill("#passwordInput", password)
        page.click("span#submitButton")   # calls Login.submitLoginRequest()
        page.wait_for_load_state("networkidle", timeout=20_000)
    except PWTimeout:
        logger.error("Password field or submit button not found. URL: %s", page.url)
        raise RuntimeError("Could not interact with the ADFS password page.")

    # Step 3 – wait for OWA inbox
    try:
        page.wait_for_url(f"**{INBOX_URL_FRAGMENT}**", timeout=45_000)
        logger.info("Login successful – OWA inbox loaded")
    except PWTimeout:
        logger.warning(
            "Did not reach the inbox after ADFS login. Current URL: %s\n"
            "Run --auth locally (HEADLESS=0) to log in manually, then redeploy.",
            page.url,
        )
        raise RuntimeError(
            "ADFS login did not complete automatically. "
            "Run --auth locally (HEADLESS=0) then redeploy."
        )


def ensure_logged_in(page: Page, email: str, password: str) -> None:
    """Navigate to the inbox; login if the session has expired."""
    page.goto(MAIL_URL, wait_until="networkidle", timeout=60_000)
    if not is_logged_in(page):
        login(page, email, password)
