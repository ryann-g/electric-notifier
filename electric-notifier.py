#!/usr/bin/env python3
"""
Electric Notifier — pops up power-outage / billing-increase posts from your
electrical provider's Facebook page every time you power on the PC.

Modes
  boot        (default) fetch, filter, pop up new matching posts
  test-feed   show what the configured RSS feed(s) return
  test-notify show a sample popup
  test-rules  classify a text sample against the keyword rules

Stdlib only — no pip packages needed.
"""

import argparse
import fcntl
import hashlib
import html
import json
import logging
import re
import subprocess
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.request import Request, urlopen

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
STATE_DIR = Path.home() / ".local/share/electric-notifier"
STATE_PATH = STATE_DIR / "state.json"
HISTORY_PATH = STATE_DIR / "history.log"
LOG_PATH = STATE_DIR / "notifier.log"
LOCK_PATH = STATE_DIR / ".lock"

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) ElectricNotifier/1.0"
CATEGORY_LABELS = {"outage": "Power outage", "billing": "Billing update"}
ATOM_NS = "http://www.w3.org/2005/Atom"

log = logging.getLogger("electric-notifier")


class FetchError(RuntimeError):
    """No feed could provide usable posts."""


# --------------------------------------------------------------------------- #
# config / state / history
# --------------------------------------------------------------------------- #

def load_config(path: Path) -> dict:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    tmp.replace(STATE_PATH)


def prune_seen(state: dict, keep_days: float = 30.0) -> None:
    """Drop seen-post entries older than keep_days so state.json stays small.
    Timestamps are stored as YYYY-MM-DD HH:MM, so strings sort as dates."""
    seen = state.get("seen")
    if not isinstance(seen, dict):
        return
    cutoff = (datetime.now() - timedelta(days=keep_days)).strftime("%Y-%m-%d %H:%M")
    state["seen"] = {
        post_id: ts for post_id, ts in seen.items() if isinstance(ts, str) and ts >= cutoff
    }


def log_history(line: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(HISTORY_PATH, "a", encoding="utf-8") as handle:
        handle.write(f"{timestamp} | {line}\n")


# --------------------------------------------------------------------------- #
# fetching
# --------------------------------------------------------------------------- #

def http_get(url: str, timeout: float) -> bytes:
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": (
                "application/rss+xml, application/atom+xml, "
                "application/xml;q=0.9, text/xml;q=0.8, */*;q=0.7"
            ),
            "Accept-Language": "en",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        return response.read()


def strip_html(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    # ILPI posts often use Unicode bold-font chars ("𝐈𝐋𝐏𝐈"); NFKC turns
    # them back into normal letters so keyword matching works.
    return unicodedata.normalize("NFKC", text).strip()


def clean_summary(text: str, limit: int = 200) -> str:
    text = strip_html(text)
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut + " …"


def parse_date(raw: str):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def format_date(raw: str) -> str:
    dt = parse_date(raw)
    if dt is not None:
        return dt.strftime("%Y-%m-%d %H:%M")
    return (raw or "").strip()[:40]


def parse_feed(raw: bytes) -> list:
    root = ET.fromstring(raw)
    posts = []

    def add(post_id, title, link, published, summary):
        title = strip_html(title) or "(no title)"
        if not post_id:
            fingerprint = f"{title}|{link}|{format_date(published)}"
            post_id = "hash-" + hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()[:16]
        posts.append(
            {
                "id": post_id,
                "title": title,
                "link": (link or "").strip(),
                "published": format_date(published),
                "summary": clean_summary(summary),
            }
        )

    # RSS 2.0
    for item in root.findall(".//item"):
        guid = (item.findtext("guid") or "").strip() or None
        title = item.findtext("title") or ""
        link = item.findtext("link") or ""
        published = (item.findtext("pubDate") or "").strip() or None
        summary = (item.findtext("description") or "").strip()
        add(guid, title, link, published, summary)

    # Atom
    atom = "{{{}}}".format(ATOM_NS)
    for entry in root.findall(f".//{atom}entry"):
        guid = (entry.findtext(f"{atom}id") or "").strip() or None
        title = (entry.findtext(f"{atom}title") or "").strip()
        link = ""
        for link_el in entry.findall(f"{atom}link"):
            rel = (link_el.get("rel") or "").strip()
            if not rel or rel == "alternate":
                link = link_el.get("href") or ""
                break
        published = (
            entry.findtext(f"{atom}published") or entry.findtext(f"{atom}updated") or ""
        ).strip() or None
        summary = (
            entry.findtext(f"{atom}content") or entry.findtext(f"{atom}summary") or ""
        ).strip()
        add(guid, title, link, published, summary)

    unique = {}
    for post in posts:
        unique.setdefault(post["id"], post)
    return list(unique.values())


def fetch_posts(cfg: dict):
    fetch_cfg = cfg.get("fetch", {}) or {}
    feeds = cfg.get("feeds") or []
    attempts = max(1, int(fetch_cfg.get("retries", 3)))
    wait = float(fetch_cfg.get("retry_wait", 5))
    timeout = float(fetch_cfg.get("timeout", 15))
    errors = []
    for url in feeds:
        for attempt in range(1, attempts + 1):
            try:
                posts = parse_feed(http_get(url, timeout))
            except Exception as exc:  # urllib raises many types
                errors.append(f"{url}: {type(exc).__name__}: {exc} (try {attempt}/{attempts})")
            else:
                if posts:
                    return posts, url
                errors.append(f"{url}: feed returned no posts (try {attempt}/{attempts})")
            if attempt < attempts:
                time.sleep(wait)
    raise FetchError("; ".join(errors[-4:]) if errors else "no feeds configured")


def scrape_mbasic(page: str) -> list:
    """Last-resort fallback. Facebook blocks logged-out scraping from server
    IPs, but it sometimes still works from a residential connection."""
    url = f"https://mbasic.facebook.com/{page}?v=timeline"
    markup = http_get(url, 15).decode("utf-8", "replace")
    pattern = re.compile(r'<a[^>]+href="(/story\.php\?story_fbid=(\d+)[^"]*)"[^>]*>(.*?)</a>', re.S)
    posts = []
    for href, story_fbid, block in pattern.findall(markup)[:40]:
        posts.append(
            {
                "id": f"mbasic-{story_fbid}",
                "title": (strip_html(block) or "(no title)")[:160],
                "link": "https://www.facebook.com" + html.unescape(href),
                "published": "",
                "summary": "",
            }
        )
    return posts


def fetch_all(cfg: dict):
    try:
        return fetch_posts(cfg)
    except FetchError as primary:
        if not cfg.get("fallback_scrape"):
            raise
        page = cfg.get("fallback_scrape_page") or ""
        log.warning("feeds failed (%s); trying mbasic fallback", primary)
        try:
            posts = scrape_mbasic(page)
        except Exception as exc:
            raise FetchError(f"{primary} | mbasic fallback: {type(exc).__name__}: {exc}")
        if not posts:
            raise FetchError(f"{primary} | mbasic fallback: no posts parsed")
        return posts, f"mbasic://{page}"


# --------------------------------------------------------------------------- #
# keyword rules
# --------------------------------------------------------------------------- #

def compile_rules(cfg: dict) -> dict:
    compiled = {}
    for category, patterns in (cfg.get("rules") or {}).items():
        if not patterns:
            continue
        joined = "|".join(f"(?:{pattern})" for pattern in patterns)
        compiled[category] = re.compile(joined, re.IGNORECASE)
    return compiled


def classify(post: dict, compiled: dict) -> list:
    text = f"{post['title']}\n{post['summary']}"
    return [category for category, regex in compiled.items() if regex.search(text)]


def label_categories(categories: list) -> str:
    names = [CATEGORY_LABELS.get(c, c.title()) for c in categories]
    return " + ".join(names)


# --------------------------------------------------------------------------- #
# popups
# --------------------------------------------------------------------------- #

def popup_with_action(title: str, body: str, urgency: str, icon: str | None, url: str, wait_seconds: float) -> str:
    """Sends the notification over DBus with a click action ("Open post").
    Returns 'clicked', 'expired' or 'failed'."""
    try:
        import gi  # python3-gi ships with GNOME (present on Zorin)
        from gi.repository import Gio, GLib
    except Exception as exc:
        log.info("python-gi unavailable (%s); using plain popup", exc)
        return "failed"
    try:
        conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    except Exception as exc:
        log.info("no session bus (%s); using plain popup", exc)
        return "failed"

    hint_urgency = GLib.Variant("y", 2 if urgency == "critical" else 1)
    parameters = GLib.Variant(
        "(susssasa{sv}i)",
        (
            "Electric Notifier",
            0,
            icon or "",
            title,
            body,
            ["default", "Open post"],
            {"urgency": hint_urgency},
            -1,  # sticky: banner stays until used or dismissed
        ),
    )
    loop = GLib.MainLoop()
    wanted = {"id": None}
    clicked = {"now": False}

    def on_action(_conn, _sender, _path, _iface, _signal, params):
        try:
            notif_id, _action_key = params.unpack()
        except Exception:
            return
        if wanted["id"] is None or notif_id == wanted["id"]:
            clicked["now"] = True
            loop.quit()

    subscription = conn.signal_subscribe(
        None,
        "org.freedesktop.Notifications",
        "ActionInvoked",
        None,
        None,
        Gio.DBusSignalFlags.NONE,
        on_action,
    )
    try:
        result = conn.call_sync(
            "org.freedesktop.Notifications",
            "/org/freedesktop/Notifications",
            "org.freedesktop.Notifications",
            "Notify",
            parameters,
            None,
            Gio.DBusCallFlags.NONE,
            5000,
            None,
        )
        wanted["id"] = result.unpack()[0]
    except Exception as exc:
        log.warning("DBus Notify failed: %s; using plain popup", exc)
        conn.signal_unsubscribe(subscription)
        return "failed"

    def _quit():
        loop.quit()
        return GLib.SOURCE_REMOVE

    if wait_seconds <= 0:
        wait_seconds = 120.0
    GLib.timeout_add_seconds(max(1, int(wait_seconds)), _quit)
    loop.run()
    conn.signal_unsubscribe(subscription)

    if clicked["now"]:
        subprocess.Popen(
            ["xdg-open", url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log.info("popup clicked; opening %s", url)
        return "clicked"
    log.info("click window elapsed without click (%ss)", wait_seconds)
    return "expired"


def show_popup(title: str, body: str, urgency: str = "normal", icon: str | None = None, cfg: dict | None = None, click_url: str | None = None) -> bool:
    """Shows a popup. With a click_url it first tries the DBus action path
    (click opens the post); falls back to a plain notify-send popup."""
    settings = (cfg or {}).get("notify", {}) or {}
    if click_url:
        outcome = popup_with_action(title, body, urgency, icon, click_url, float(settings.get("click_wait", 120)))
        if outcome in ("clicked", "expired"):
            return True
    args = ["notify-send", f"--urgency={urgency}"]
    if icon:
        args.append(f"--icon={icon}")
    args.extend([title, body])
    try:
        subprocess.run(args, check=True, timeout=15)
    except subprocess.CalledProcessError as exc:
        log.warning("notify-send exited %s", exc.returncode)
        return False
    except FileNotFoundError:
        log.error("notify-send is not installed")
        return False
    except subprocess.TimeoutExpired:
        log.warning("notify-send timed out")
        return False
    return True


def notify_post(post: dict, categories: list, cfg: dict) -> None:
    settings = cfg.get("notify", {}) or {}
    icons = settings.get("icons") or {}
    icon = icons.get(categories[0])
    urgency = "critical" if "outage" in categories else "normal"
    title = f"⚡ {cfg.get('page_name', 'provider')} — {label_categories(categories)}"
    lines = [post["title"]]
    if post.get("summary"):
        lines.append(post["summary"])
    if post.get("published"):
        lines.append(post["published"])
    show_popup(title, "\n".join(lines), urgency=urgency, icon=icon, cfg=cfg, click_url=post.get("link"))
    log_history(f"{label_categories(categories)} | {post['link'] or '(no link)'} | {post['title']}")


# --------------------------------------------------------------------------- #
# boot flow
# --------------------------------------------------------------------------- #

def within_recent_days(post: dict, days: float) -> bool:
    if days <= 0:
        return True
    if not post.get("published"):
        return False
    dt = parse_date(post["published"])
    if dt is None:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    age = (datetime.now(dt.tzinfo) - dt).total_seconds()
    return age <= days * 86400


def is_stale_post(post: dict, recent_days: float) -> bool:
    """True when the post has a real published date but is older than
    recent_days. Posts without any date (fallback scrape) are never stale."""
    return (
        bool(post.get("published"))
        and parse_date(post["published"]) is not None
        and not within_recent_days(post, recent_days)
    )


def published_date(post: dict) -> datetime:
    """Publication time as a naive local datetime, for newest-first sorting.
    Posts without a date sort last."""
    dt = parse_date(post.get("published") or "")
    if dt is None:
        return datetime.min
    return dt.astimezone().replace(tzinfo=None) if dt.tzinfo else dt


class BootLock:
    """Prevent two runs popping double notifications."""

    def __init__(self):
        self.handle = None

    def __enter__(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.handle = open(LOCK_PATH, "w")
        try:
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log.info("another boot run holds the lock; exiting")
            self.handle.close()
            self.handle = None
            return None
        return self

    def __exit__(self, *_):
        if self.handle:
            fcntl.flock(self.handle, fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


def cmd_boot(cfg: dict, compiled: dict) -> int:
    try:
        posts, source = fetch_all(cfg)
    except FetchError as exc:
        log.warning("feed fetch failed: %s", exc)
        settings = cfg.get("notify", {}) or {}
        if settings.get("notify_on_feed_error"):
            show_popup(
                "Electric Notifier",
                f"Could not fetch provider updates.\nSee log: {LOG_PATH}",
                urgency="normal",
            )
        print(f"Feed fetch failed — see {LOG_PATH}")
        return 0

    log.info("boot: %s posts via %s", len(posts), source)
    state = load_state()
    prune_seen(state)
    seen = state.setdefault("seen", {})
    fresh = [post for post in posts if post["id"] not in seen]

    limit = max(1, int((cfg.get("notify", {}) or {}).get("max_popups", 3)))

    if not state.get("bootstrapped"):
        # First run: mark everything seen, but still pop REALLY recent matches
        # (last N days) so you can see the feature working.
        popped = 0
        for post in fresh:
            categories = classify(post, compiled)
            if categories and within_recent_days(post, float(cfg.get("bootstrap_days", 3))):
                if popped >= limit:
                    break
                notify_post(post, categories, cfg)
                popped += 1
        state["bootstrapped"] = True
        for post in fresh:
            seen[post["id"]] = datetime.now().strftime("%Y-%m-%d %H:%M")
        save_state(state)
        log.info("bootstrap: %s posts seen; popped %s recent matches", len(posts), popped)
        print(f"First run done: {len(posts)} posts marked as seen, {popped} recent matches popped.")
        return 0

    matches = [(post, classify(post, compiled)) for post in fresh]
    matches = [(post, cats) for post, cats in matches if cats]

    # The RSS bridge sometimes drops old posts and re-serves them later, so a
    # not-yet-seen ID alone proves nothing. Skip matched posts published before
    # notify.recent_days — they are marked seen silently instead of popping as
    # days-old "new" advisories. recent_days <= 0 keeps the old behaviour.
    settings = cfg.get("notify", {}) or {}
    recent_days = max(0.0, float(settings.get("recent_days", 2)))

    if recent_days > 0:
        stale = [pair for pair in matches if is_stale_post(pair[0], recent_days)]
        if stale:
            log.info(
                "boot: skipped %s stale matched posts (older than %s days): %s",
                len(stale),
                recent_days,
                "; ".join(post["title"][:50] for post, _ in stale[:4]),
            )
            matches = [pair for pair in matches if not is_stale_post(pair[0], recent_days)]

    # Newest first, so max_popups always keeps the freshest posts.
    matches.sort(key=lambda pair: published_date(pair[0]), reverse=True)

    for post, categories in matches[:limit]:
        notify_post(post, categories, cfg)
        seen[post["id"]] = datetime.now().strftime("%Y-%m-%d %H:%M")
        save_state(state)  # persist per popup so a shutdown mid-run never re-pops them

    if len(matches) > limit:
        titles = "; ".join(post["title"][:60] for post, _ in matches)
        any_outage = any("outage" in cats for _, cats in matches)
        show_popup(
            f"⚡ {cfg.get('page_name', 'provider')} — {len(matches)} new updates",
            f"{titles}\nFull list: {HISTORY_PATH}",
            urgency="critical" if any_outage else "normal",
        )
        log_history(f"summary | {len(matches)} new matching posts (see above lines)")

    for post in fresh:
        seen[post["id"]] = datetime.now().strftime("%Y-%m-%d %H:%M")
    save_state(state)
    log.info("boot: %s fresh, %s matched, %s popped", len(fresh), len(matches), min(len(matches), limit))
    print(f"Checked {len(posts)} posts: {len(fresh)} new, {len(matches)} matched, {min(len(matches), limit)} popped.")
    return 0


# --------------------------------------------------------------------------- #
# test / cli
# --------------------------------------------------------------------------- #

def cmd_test_feed(cfg: dict, compiled: dict) -> int:
    try:
        posts, source = fetch_all(cfg)
    except FetchError as exc:
        print(f"FEED FAIL: {exc}")
        print("Hint: see 'Feed setup' in README.md (local RSSHub not running yet?).")
        return 1
    print(f"FEED OK: {len(posts)} posts from {source}")
    for post in posts[:8]:
        cats = classify(post, compiled)
        tag = "[{}]".format(",".join(cats)) if cats else "[-]"
        print(f"  {tag} {post['published'] or 'no date':<18} | {post['title'][:80]}")
    return 0


def cmd_test_notify(cfg: dict) -> int:
    wait = max(10.0, float((cfg.get("notify", {}) or {}).get("click_wait", 120)))
    okay = show_popup(
        f"⚡ {cfg.get('page_name', 'provider')} — test",
        f"Popups are working!\nClick \"Open post\" (or the banner) within {int(wait)}s —"
        " your browser should open the ILPI Facebook page.",
        urgency="critical",
        icon="dialog-warning",
        cfg=cfg,
        click_url="https://www.facebook.com/iliganlight",
    )
    print("popup sent" if okay else "popup FAILED — see notifier.log")
    return 0 if okay else 1


def cmd_open(cfg: dict) -> int:
    try:
        lines = [line for line in HISTORY_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    except FileNotFoundError:
        lines = []
    url = None
    for line in reversed(lines):
        match = re.search(r"https?://\S+", line)
        if match:
            url = match.group(0)
            break
    if not url:
        print("No popped posts yet — nothing to open.")
        return 1
    try:
        subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        log.error("xdg-open not found")
        print("xdg-open missing")
        return 1
    print(f"Opening: {url}")
    return 0


def cmd_test_rules(text: str, compiled: dict) -> int:
    post = {"id": "test", "title": text, "summary": "", "link": "", "published": ""}
    categories = classify(post, compiled)
    if categories:
        print(f"matched: {', '.join(categories)}")
        return 0
    print("matched: nothing")
    return 2


def main(argv: list) -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(LOG_PATH),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    parser = argparse.ArgumentParser(
        prog="electric-notifier",
        description="Outage/billing popups from your electric provider's Facebook page at boot.",
    )
    parser.add_argument("mode", nargs="?", default="boot", choices=("boot", "test-feed", "test-notify", "test-rules", "open"))
    parser.add_argument("--text", help="sample text for test-rules")
    parser.add_argument("--config", default=str(CONFIG_PATH), help="path to config.json")
    args = parser.parse_args(argv)

    cfg = load_config(Path(args.config))
    compiled = compile_rules(cfg)

    if args.mode == "test-notify":
        return cmd_test_notify(cfg)
    if args.mode == "test-rules":
        sample = args.text or (sys.stdin.read().strip() if not sys.stdin.isatty() else "")
        if not sample:
            print('usage: electric-notifier.py test-rules --text "post text"')
            return 2
        return cmd_test_rules(sample, compiled)
    if args.mode == "open":
        return cmd_open(cfg)
    if args.mode == "test-feed":
        return cmd_test_feed(cfg, compiled)

    with BootLock() as lock:
        if lock is None:
            return 0
        return cmd_boot(cfg, compiled)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(130)
