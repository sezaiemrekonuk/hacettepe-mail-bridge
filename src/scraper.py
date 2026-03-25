"""
Scrapes new messages from the Hacettepe OWA inbox.

Supports two OWA rendering modes:
  - **premium** (OWA 2016 / Exchange 2019): SPA with reading pane.
  - **basic**   (OWA 2010 / 2013 fallback): table layout, page navigation.

The premium view is served when a realistic Chrome user-agent is set;
the basic view is a fallback for unrecognised browsers.
"""
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import List
from urllib.parse import urljoin, unquote

from playwright.sync_api import Page, TimeoutError as PWTimeout

logger = logging.getLogger(__name__)

MAX_MESSAGES_PER_POLL = 10
INTER_MESSAGE_DELAY = 3  # seconds between message clicks to avoid MAPI session limits
_SCREENSHOT_DIR = os.environ.get("SCREENSHOT_DIR", "/app/data")


@dataclass
class Message:
    id: str
    subject: str
    sender_name: str
    sender_email: str
    date: str
    body_html: str
    body_text: str
    attachments: List[str] = field(default_factory=list)


# ------------------------------------------------------------------ #
# Debug helpers
# ------------------------------------------------------------------ #

def _save_debug(page: Page, name: str = "debug") -> None:
    try:
        os.makedirs(_SCREENSHOT_DIR, exist_ok=True)
        page.screenshot(
            path=os.path.join(_SCREENSHOT_DIR, f"{name}.png"), full_page=True
        )
        with open(os.path.join(_SCREENSHOT_DIR, f"{name}.html"), "w", encoding="utf-8") as f:
            f.write(page.content())
        logger.info("Debug artifacts saved → %s/{%s.png, %s.html}", _SCREENSHOT_DIR, name, name)
    except Exception as exc:
        logger.warning("Could not save debug artifacts: %s", exc)


# ------------------------------------------------------------------ #
# Inbox detection
# ------------------------------------------------------------------ #

_PREMIUM_SELECTORS = ["div[role='listbox']", "div[data-convid]"]
_BASIC_SELECTORS = [
    "a[href*='ae=Item']",
    "a[href*='ae=PreFormAction']",
    "#tblMailList",
    "table.lvw",
]


def _wait_for_inbox(page: Page) -> str:
    """Block until the inbox is ready; return ``"premium"`` or ``"basic"``."""
    for sel in _PREMIUM_SELECTORS:
        try:
            page.wait_for_selector(sel, timeout=8_000)
            logger.info("Inbox ready (premium — %s)", sel)
            return "premium"
        except PWTimeout:
            pass

    for sel in _BASIC_SELECTORS:
        try:
            page.wait_for_selector(sel, timeout=10_000)
            logger.info("Inbox ready (basic — %s)", sel)
            return "basic"
        except PWTimeout:
            pass

    logger.error("No inbox detected. url=%s  title=%s", page.url, page.title())
    _save_debug(page, "inbox_timeout")
    raise RuntimeError(
        f"Inbox not detected. URL: {page.url} | Title: {page.title()}"
    )


# ================================================================== #
#  PREMIUM helpers  (OWA 2016 / Exchange 2019 SPA)
# ================================================================== #

def _get_rows_premium(page: Page) -> List[dict]:
    """Parse message metadata from the ``aria-label`` on each list item.

    aria-label format:
      "1 Unread, From {sender}[, Files Attached], Subject {subject},
       Last message {date}. "
    """
    raw = page.evaluate(
        """() => {
        const results = [], seen = new Set();
        document.querySelectorAll('div[data-convid]').forEach(wrapper => {
            const id = wrapper.getAttribute('data-convid');
            if (!id || seen.has(id)) return;
            seen.add(id);
            const opt = wrapper.querySelector("div[role='option']");
            const al  = opt ? (opt.getAttribute('aria-label') || '') : '';
            results.push({ id, ariaLabel: al });
        });
        return results.slice(0, """
        + str(MAX_MESSAGES_PER_POLL)
        + """);
    }"""
    )

    rows: List[dict] = []
    for item in raw:
        al = item.get("ariaLabel", "")
        sender = _re_first(r"From\s+(.+?),\s+(?:Files Attached,\s+)?Subject\s+", al)
        subject = _re_first(r"Subject\s+(.+?),\s+Last message\s+", al)
        date = _re_first(r"Last message\s+(.+?)\.\s*$", al)
        rows.append(
            {
                "id": item["id"],
                "sender": sender,
                "subject": subject,
                "date": date,
                "view": "premium",
            }
        )
    return rows


def _re_first(pattern: str, text: str) -> str:
    m = re.search(pattern, text)
    return m.group(1).strip() if m else ""


_BODY_SELECTOR = (
    'div[id="Item.MessageUniqueBody"], '
    'div[id="Item.MessagePartBody"]'
)
_SUBJECT_SELECTOR = "span.rpHighlightSubjectClass"


def _extract_premium_message(page: Page, row: dict) -> Message:
    """Click a list item, wait for the reading pane, and extract body + email."""
    safe_id = row["id"].replace("'", "\\'")
    clicked = page.evaluate(
        f"""() => {{
        const w = document.querySelector("div[data-convid='{safe_id}']");
        if (!w) return false;
        (w.querySelector("div[role='option']") || w).click();
        return true;
    }}"""
    )
    if not clicked:
        raise RuntimeError(f"Could not click message {row['id']}")

    # Wait for reading pane body (not networkidle — OWA never truly idles).
    try:
        page.wait_for_selector(_BODY_SELECTOR, state="attached", timeout=15_000)
    except PWTimeout:
        # Fallback: wait for the subject span in reading pane
        try:
            page.wait_for_selector(_SUBJECT_SELECTOR, timeout=5_000)
        except PWTimeout:
            pass
    time.sleep(1)

    data = page.evaluate(
        r"""() => {
        /* body */
        const bodyEl = document.querySelector('#Item\\.MessageUniqueBody')
                     || document.querySelector('div[id="Item.MessageUniqueBody"]')
                     || document.querySelector('div[id="Item.MessagePartBody"]');
        const bodyHtml = bodyEl ? bodyEl.innerHTML : '';
        const bodyText = bodyEl ? bodyEl.textContent.trim() : '';

        /* subject (fallback if aria-label parsing missed it) */
        const subjEl = document.querySelector('span.rpHighlightSubjectClass');
        const subject = subjEl ? subjEl.textContent.trim() : '';

        /* sender email from persona photo URL */
        let senderEmail = '';
        const imgs = document.querySelectorAll('div._rp_R4 img[src*="GetPersonaPhoto"]');
        for (const img of imgs) {
            const m = (img.getAttribute('src') || '').match(/email=([^&]+)/);
            if (m) { senderEmail = decodeURIComponent(m[1]); break; }
        }
        /* fallback: any persona photo in reading pane */
        if (!senderEmail) {
            const img2 = document.querySelector('div[aria-label="Reading Pane"] img[src*="GetPersonaPhoto"]');
            if (img2) {
                const m2 = (img2.getAttribute('src') || '').match(/email=([^&]+)/);
                if (m2) senderEmail = decodeURIComponent(m2[1]);
            }
        }

        /* date from reading pane header */
        let date = '';
        const dateEl = document.querySelector('div._rp_e8 span.allowTextSelection[title]');
        if (dateEl) date = dateEl.getAttribute('title') || dateEl.textContent.trim();

        return { bodyHtml, bodyText, subject, senderEmail, date };
    }"""
    )

    return Message(
        id=row["id"],
        subject=data.get("subject") or row.get("subject", ""),
        sender_name=row.get("sender", ""),
        sender_email=data.get("senderEmail", ""),
        date=data.get("date") or row.get("date", ""),
        body_html=data.get("bodyHtml", ""),
        body_text=data.get("bodyText", ""),
    )


# ================================================================== #
#  BASIC helpers  (OWA 2010 / 2013 table layout)
# ================================================================== #

def _get_rows_basic(page: Page) -> List[dict]:
    return page.evaluate(
        """() => {
        const results = [], seen = new Set();
        for (const a of document.querySelectorAll('a[href]')) {
            const href = a.getAttribute('href') || '';
            if (!href.includes('ae=Item') && !href.includes('ae=PreFormAction'))
                continue;
            const m = href.match(/[?&]id=([^&]+)/);
            if (!m) continue;
            const id = m[1];
            if (seen.has(id)) continue;
            seen.add(id);
            const tr = a.closest('tr');
            const cells = tr
                ? Array.from(tr.querySelectorAll('td')).map(c => c.textContent.trim())
                : [];
            results.push({
                id, href: a.href, subject: a.textContent.trim(),
                row_cells: cells, view: 'basic',
            });
        }
        return results.slice(0, """
        + str(MAX_MESSAGES_PER_POLL)
        + """);
    }"""
    )


def _extract_basic_message(page: Page, row: dict, inbox_url: str) -> Message:
    msg_url = row["href"]
    if not msg_url.startswith("http"):
        msg_url = urljoin(inbox_url, msg_url)

    page.goto(msg_url, wait_until="networkidle", timeout=30_000)
    time.sleep(2)

    data = page.evaluate(
        r"""() => {
        let subject = '';
        for (const sel of ['h1', '#divSubj', '#spnSbj', 'td.hdln']) {
            const el = document.querySelector(sel);
            if (el && el.textContent.trim()) { subject = el.textContent.trim(); break; }
        }
        let senderName = '', senderEmail = '';
        for (const sel of [
            '#divFrom a[href*="mailto:"]', 'a[href*="mailto:"]',
            'span[title*="@"]', '#divFrom span',
        ]) {
            const el = document.querySelector(sel);
            if (!el) continue;
            const href = el.getAttribute('href') || '';
            const title = el.getAttribute('title') || '';
            const text = el.textContent.trim();
            if (href.startsWith('mailto:')) {
                senderEmail = href.replace('mailto:', '').split('?')[0];
                senderName = text || senderEmail; break;
            }
            if (title.includes('@')) { senderEmail = title; senderName = text || title; break; }
            if (text.includes('@')) { senderEmail = text; senderName = text; break; }
        }
        let date = '';
        for (const sel of ['#spnDate', '#divSnt', 'td.snt']) {
            const el = document.querySelector(sel);
            if (el && el.textContent.trim()) {
                date = el.textContent.trim().replace(/^Sent:\s*/i, ''); break;
            }
        }
        let bodyHtml = '', bodyText = '';
        for (const sel of ['#divBdy', '#MsgContainer', 'div[id*="Body"]', 'td.bdy']) {
            const el = document.querySelector(sel);
            if (el && el.textContent.trim()) {
                bodyHtml = el.innerHTML; bodyText = el.textContent.trim(); break;
            }
        }
        return { subject, senderName, senderEmail, date, bodyHtml, bodyText };
    }"""
    )

    return Message(
        id=row["id"],
        subject=data.get("subject") or row.get("subject", ""),
        sender_name=data.get("senderName", ""),
        sender_email=data.get("senderEmail", ""),
        date=data.get("date", ""),
        body_html=data.get("bodyHtml", ""),
        body_text=data.get("bodyText", ""),
    )


# ================================================================== #
#  Public entry point
# ================================================================== #

def _is_exchange_error(page: Page) -> bool:
    """Detect Exchange server-side error pages (session limit, 500, etc.)."""
    url = page.url
    return "errorfe.aspx" in url or "httpCode=500" in url


def _navigate_to_inbox(page: Page, retries: int = 2) -> None:
    """Navigate to OWA inbox with retry on Exchange transient errors."""
    OWA_INBOX = "https://posta.hacettepe.edu.tr/owa/"

    for attempt in range(1 + retries):
        if "/owa/" in page.url and not _is_exchange_error(page):
            page.reload(wait_until="networkidle", timeout=60_000)
        else:
            page.goto(OWA_INBOX, wait_until="networkidle", timeout=60_000)

        time.sleep(5)
        page.wait_for_load_state("domcontentloaded", timeout=30_000)

        if not _is_exchange_error(page):
            return

        if attempt < retries:
            wait = 30 * (attempt + 1)
            logger.warning(
                "Exchange error (attempt %d/%d): %s — retrying in %ds",
                attempt + 1, 1 + retries, page.url.split("?")[0], wait,
            )
            page.goto("about:blank")
            time.sleep(wait)

    logger.error("Exchange error persists after retries: %s", page.url)
    _save_debug(page, "exchange_error")
    raise RuntimeError(
        f"Exchange server error after {1 + retries} attempts. "
        "The server may be rate-limiting MAPI sessions. "
        "It usually recovers within 15-30 minutes."
    )


def scrape_new_messages(page: Page, is_seen_fn, mark_seen_fn) -> List[Message]:
    _navigate_to_inbox(page)

    view = _wait_for_inbox(page)
    inbox_url = page.url

    rows = _get_rows_premium(page) if view == "premium" else _get_rows_basic(page)
    logger.info("Found %d message(s) in inbox (%s view)", len(rows), view)

    new_messages: List[Message] = []

    for i, row in enumerate(rows):
        msg_id = row["id"]
        if is_seen_fn(msg_id):
            logger.debug("Skipping seen %s", msg_id)
            continue

        # Throttle clicks to avoid hitting Exchange MAPI session limits
        if i > 0 and new_messages:
            time.sleep(INTER_MESSAGE_DELAY)

        logger.info("Processing new message %s", msg_id)
        try:
            if view == "premium":
                msg = _extract_premium_message(page, row)
            else:
                msg = _extract_basic_message(page, row, inbox_url)

            new_messages.append(msg)
            mark_seen_fn(msg_id)
            logger.info("Captured: [%s] from %s", msg.subject, msg.sender_email)

        except Exception as exc:
            logger.error("Failed to process %s: %s", msg_id, exc)
            _save_debug(page, f"msg_error_{msg_id[:20]}")
            mark_seen_fn(msg_id)

    # Release MAPI sessions by navigating away from OWA
    try:
        page.goto("about:blank", wait_until="commit", timeout=5_000)
    except Exception:
        pass

    return new_messages
