#!/usr/bin/env python3
"""
Trackr Internship Watcher
-------------------------
Daily (twice-daily) scrape of The Trackr's UK Finance summer-internship listings.
Alerts on:
  (a) brand-new programme IDs        -> labelled "newly listed"
  (b) openingDate null -> real date  -> labelled "now open"  (the strong "just opened" signal)

Notifies via mobile push (ntfy) + email digest (Gmail SMTP). First run saves a
silent baseline. Configuration is via environment variables (see README).
"""

import os
import sys
import json
import time
import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

# --------------------------------------------------------------------------- #
# Configuration (env-driven)
# --------------------------------------------------------------------------- #
API_URL = (
    "https://api.the-trackr.com/programmes"
    "?region=UK&industry=Finance&season=2027&type=summer-internships"
)
PUBLIC_PAGE = "https://app.the-trackr.com/uk-finance/summer-internships"
STATE_FILE = os.environ.get("STATE_FILE", "state.json")

# Sanity threshold: warn if fewer rows than this come back (early API-change/rate-limit signal).
MIN_EXPECTED_ROWS = int(os.environ.get("MIN_EXPECTED_ROWS", "300"))

# ntfy push
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")          # e.g. "trackr-hey-9fj3"
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")

# Gmail SMTP
GMAIL_USER = os.environ.get("GMAIL_USER", "")          # your.address@gmail.com
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
EMAIL_TO = os.environ.get("EMAIL_TO", GMAIL_USER)      # defaults to self

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
)
log = logging.getLogger("trackr")


# --------------------------------------------------------------------------- #
# Fetch
# --------------------------------------------------------------------------- #
def fetch_rows():
    """Return (rows, raw_byte_count). Raises on hard failure."""
    req = Request(API_URL, headers={"User-Agent": "trackr-watcher/1.0"})
    last_err = None
    for attempt in range(1, 4):
        try:
            with urlopen(req, timeout=30) as resp:
                raw = resp.read()
            data = json.loads(raw)
            rows = data if isinstance(data, list) else (
                data.get("data") or data.get("programmes") or []
            )
            return rows, len(raw)
        except (URLError, HTTPError, json.JSONDecodeError) as e:
            last_err = e
            log.warning("Fetch attempt %d/3 failed: %s", attempt, e)
            time.sleep(2 * attempt)
    raise RuntimeError(f"All fetch attempts failed: {last_err}")


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
def load_state():
    if not os.path.exists(STATE_FILE):
        return None  # signals first run
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.error("State file unreadable (%s); treating as first run to avoid spurious alerts.", e)
        return None


def build_snapshot(rows):
    """id -> {openingDate, name, company} — the minimal fields the diff needs."""
    snap = {}
    for r in rows:
        rid = r.get("id")
        if not rid:
            continue
        snap[rid] = {
            "openingDate": r.get("openingDate"),
            "name": r.get("name"),
            "company": (r.get("company") or {}).get("name"),
        }
    return snap


def save_state(rows, raw_bytes):
    snapshot = build_snapshot(rows)
    payload = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "row_count": len(rows),
        "raw_bytes": raw_bytes,
        "programmes": snapshot,
    }
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, STATE_FILE)  # atomic
    log.info("State saved: %d programmes.", len(snapshot))


# --------------------------------------------------------------------------- #
# Diff
# --------------------------------------------------------------------------- #
def diff(prev_snapshot, rows):
    """Return (newly_listed, now_open) lists of row dicts."""
    by_id = {r.get("id"): r for r in rows if r.get("id")}
    newly_listed, now_open = [], []

    for rid, row in by_id.items():
        prev = prev_snapshot.get(rid)
        if prev is None:
            newly_listed.append(row)
        else:
            # null/empty -> real date transition
            if not prev.get("openingDate") and row.get("openingDate"):
                now_open.append(row)

    # Sort each by openingDate desc (most recent first), nulls last.
    def key(r):
        return r.get("openingDate") or ""
    newly_listed.sort(key=key, reverse=True)
    now_open.sort(key=key, reverse=True)
    return newly_listed, now_open


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #
def fmt_date(iso):
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%d %b %Y")
    except (ValueError, AttributeError):
        return iso


def apply_link(row):
    return row.get("url") or PUBLIC_PAGE


def row_company(row):
    return (row.get("company") or {}).get("name", "?")


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #
def send_ntfy(newly_listed, now_open):
    if not NTFY_TOPIC:
        log.info("NTFY_TOPIC unset; skipping push.")
        return
    total = len(newly_listed) + len(now_open)
    lines = []
    for r in now_open:
        lines.append(f"OPEN: {row_company(r)} — {r.get('name','?')}")
    for r in newly_listed:
        lines.append(f"NEW:  {row_company(r)} — {r.get('name','?')}")
    body = "\n".join(lines[:20])
    if total > 20:
        body += f"\n…and {total - 20} more"

    title = f"{total} internship update{'s' if total != 1 else ''}"
    url = f"{NTFY_SERVER}/{NTFY_TOPIC}"
    req = Request(
        url,
        data=body.encode("utf-8"),
        headers={
            "Title": title,
            "Tags": "briefcase",
            "Click": PUBLIC_PAGE,
            "Priority": "high" if now_open else "default",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=20) as resp:
            resp.read()
        log.info("ntfy push sent (%d items).", total)
    except (URLError, HTTPError) as e:
        log.error("ntfy push failed: %s", e)


def build_email_html(newly_listed, now_open):
    def table(title, rows, accent):
        if not rows:
            return ""
        head = (
            f'<h3 style="font-family:sans-serif;color:{accent};margin:18px 0 6px">{title} '
            f'({len(rows)})</h3>'
            '<table style="border-collapse:collapse;width:100%;font-family:sans-serif;font-size:14px">'
            '<tr style="background:#f2f2f2;text-align:left">'
            '<th style="padding:6px 8px">Company</th>'
            '<th style="padding:6px 8px">Role</th>'
            '<th style="padding:6px 8px">Opens</th>'
            '<th style="padding:6px 8px">Closes</th>'
            '<th style="padding:6px 8px">Apply</th></tr>'
        )
        body = ""
        for r in rows:
            close = r.get("closingDate")
            close_txt = "Rolling" if r.get("rolling") else fmt_date(close)
            link = apply_link(r)
            body += (
                '<tr style="border-bottom:1px solid #e0e0e0">'
                f'<td style="padding:6px 8px"><b>{row_company(r)}</b></td>'
                f'<td style="padding:6px 8px">{r.get("name","?")}</td>'
                f'<td style="padding:6px 8px">{fmt_date(r.get("openingDate"))}</td>'
                f'<td style="padding:6px 8px">{close_txt}</td>'
                f'<td style="padding:6px 8px"><a href="{link}">Apply</a></td>'
                '</tr>'
            )
        return head + body + "</table>"

    parts = ['<div style="max-width:760px;margin:auto">']
    parts.append('<h2 style="font-family:sans-serif">Trackr — UK Finance Summer 2027</h2>')
    parts.append(table("🟢 Now open", now_open, "#1a7f37"))
    parts.append(table("🆕 Newly listed", newly_listed, "#0969da"))
    parts.append(
        f'<p style="font-family:sans-serif;font-size:12px;color:#888;margin-top:20px">'
        f'Generated {datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")} · '
        f'<a href="{PUBLIC_PAGE}">Full tracker</a></p></div>'
    )
    return "\n".join(p for p in parts if p)


def send_email(newly_listed, now_open):
    if not (GMAIL_USER and GMAIL_APP_PASSWORD):
        log.info("Gmail creds unset; skipping email.")
        return
    total = len(newly_listed) + len(now_open)
    msg = MIMEMultipart("alternative")
    bits = []
    if now_open:
        bits.append(f"{len(now_open)} now open")
    if newly_listed:
        bits.append(f"{len(newly_listed)} newly listed")
    msg["Subject"] = "Trackr UK Finance: " + ", ".join(bits)
    msg["From"] = GMAIL_USER
    msg["To"] = EMAIL_TO
    msg.attach(MIMEText(build_email_html(newly_listed, now_open), "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            s.sendmail(GMAIL_USER, [EMAIL_TO], msg.as_string())
        log.info("Email sent (%d items) to %s.", total, EMAIL_TO)
    except (smtplib.SMTPException, OSError) as e:
        log.error("Email send failed: %s", e)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    rows, raw_bytes = fetch_rows()
    log.info("Fetched %d rows, %d raw bytes.", len(rows), raw_bytes)

    if len(rows) < MIN_EXPECTED_ROWS:
        log.warning(
            "Row count %d below expected minimum %d — possible API change or rate-limiting.",
            len(rows), MIN_EXPECTED_ROWS,
        )

    prev = load_state()
    if prev is None:
        save_state(rows, raw_bytes)
        log.info("First run: baseline saved, notifications suppressed.")
        return

    # Optional drift sanity check vs last snapshot.
    prev_count = prev.get("row_count", 0)
    if prev_count and len(rows) < prev_count * 0.5:
        log.warning(
            "Row count dropped sharply (%d -> %d); proceeding but flagging.",
            prev_count, len(rows),
        )

    newly_listed, now_open = diff(prev.get("programmes", {}), rows)
    log.info("Diff: %d newly listed, %d now open.", len(newly_listed), len(now_open))

    if newly_listed or now_open:
        send_ntfy(newly_listed, now_open)
        send_email(newly_listed, now_open)
    else:
        log.info("No changes; no notifications sent.")

    # Persist last so a notify-send crash doesn't swallow the change next run.
    save_state(rows, raw_bytes)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log.error("Fatal: %s", e)
        sys.exit(1)
