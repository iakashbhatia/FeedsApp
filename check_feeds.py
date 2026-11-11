#!/usr/bin/env python3
"""Watch RSS/Atom feeds and send a push notification for each new item.

Feeds are listed in feeds.json. Items that have already been announced are
remembered in state/seen.json. Notifications go out through ntfy
(https://ntfy.sh). Standard library only, Python 3.9+.

Environment variables:
  NTFY_TOPIC   required  ntfy topic to publish to (treat it like a password)
  NTFY_SERVER  optional  ntfy server, default https://ntfy.sh
  NTFY_TOKEN   optional  ntfy access token, for protected topics
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import html
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional

USER_AGENT = "Mozilla/5.0 (compatible; feed-notifier/1.0)"
DEFAULT_SERVER = "https://ntfy.sh"
DEFAULT_MAX_INDIVIDUAL = 10
MAX_SEEN = 2000  # item ids remembered per feed
MAX_TITLE_BYTES = 250
MAX_SUMMARY_BYTES = 400
MAX_MESSAGE_BYTES = 3000  # ntfy turns messages over 4096 bytes into attachments
FETCH_ATTEMPTS = 3
PUBLISH_ATTEMPTS = 4
TIMEOUT_SECONDS = 30

sleep = time.sleep  # swapped out in tests


class ConfigError(Exception):
    """The configuration or environment is unusable; nothing was attempted."""


class RequestFailed(Exception):
    """An HTTP request did not succeed after retries."""


class FeedError(Exception):
    """A feed was fetched but could not be understood."""


# --------------------------------------------------------------------------
# Configuration and state
# --------------------------------------------------------------------------


@dataclass
class Feed:
    name: str
    url: str
    site: str = ""
    max_individual: int = DEFAULT_MAX_INDIVIDUAL


def _require_http_url(value: str, what: str) -> str:
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ConfigError(f"{what} must be an http(s) URL, got {value!r}")
    return value


def load_config(path: Path) -> list[Feed]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"{path} not found") from None
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from None

    entries = raw.get("feeds") if isinstance(raw, dict) else raw
    if not isinstance(entries, list) or not entries:
        raise ConfigError(f'{path} must contain a non-empty "feeds" list')

    feeds: list[Feed] = []
    seen_urls: set[str] = set()
    for number, entry in enumerate(entries, 1):
        where = f"{path} feed #{number}"
        if not isinstance(entry, dict) or not str(entry.get("url", "")).strip():
            raise ConfigError(f'{where} needs a "url"')
        url = _require_http_url(str(entry["url"]).strip(), f"{where} url")
        if url in seen_urls:
            raise ConfigError(f"{where} repeats {url}")
        seen_urls.add(url)

        site = str(entry.get("site", "")).strip()
        if site:
            _require_http_url(site, f"{where} site")

        max_individual = entry.get("max_individual", DEFAULT_MAX_INDIVIDUAL)
        if isinstance(max_individual, bool) or not isinstance(max_individual, int) or max_individual < 0:
            raise ConfigError(f'{where} "max_individual" must be a whole number, 0 or more')

        name = str(entry.get("name", "")).strip() or urllib.parse.urlparse(url).netloc
        feeds.append(Feed(name=name, url=url, site=site, max_individual=max_individual))
    return feeds


def load_state(path: Path) -> dict:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 1, "feeds": {}}
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON ({exc}); delete it to start fresh") from None
    if not isinstance(state, dict) or not isinstance(state.get("feeds"), dict):
        raise ConfigError(f"{path} has an unexpected shape; delete it to start fresh")
    return state


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


def _retry_after(error: urllib.error.HTTPError) -> Optional[float]:
    value = (error.headers.get("Retry-After") or "").strip() if error.headers else ""
    return float(value) if value.isdigit() else None


def http_send(request: urllib.request.Request, *, attempts: int, what: str) -> bytes:
    """Send a request, retrying on rate limits, server errors and network failures."""
    problem = "no attempt made"
    for attempt in range(1, attempts + 1):
        delay = float(2**attempt)
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                body = response.read()
                if (response.headers.get("Content-Encoding") or "").lower() == "gzip":
                    body = gzip.decompress(body)
                return body
        except urllib.error.HTTPError as exc:
            problem = f"HTTP {exc.code} {exc.reason}"
            if exc.code != 429 and exc.code < 500:
                break  # our request is wrong; retrying will not help
            delay = _retry_after(exc) or delay
        except (OSError, http.client.HTTPException) as exc:
            problem = str(getattr(exc, "reason", exc)) or type(exc).__name__
        if attempt < attempts:
            sleep(min(delay, 60.0))
    raise RequestFailed(f"{what}: {problem}")


def fetch_feed(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8",
        },
    )
    return http_send(request, attempts=FETCH_ATTEMPTS, what="fetch failed")


# --------------------------------------------------------------------------
# Feed parsing
# --------------------------------------------------------------------------


@dataclass
class Item:
    id: str
    title: str
    link: str
    published: Optional[datetime]
    summary: str


_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")
_SPACE_RE = re.compile(r"\s+")
_CORE_NAMESPACES = ("", "http://www.w3.org/2005/Atom", "http://purl.org/rss/1.0/")


def clean_text(value: str) -> str:
    """Turn feed markup into one line of plain text."""
    text = _TAG_RE.sub(" ", value or "")
    text = html.unescape(text)
    text = _TAG_RE.sub(" ", text)  # markup that was escaped twice
    return _SPACE_RE.sub(" ", text).strip()


def clip(text: str, max_bytes: int) -> str:
    """Shorten text to at most max_bytes of UTF-8 without splitting a character."""
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    return raw[: max_bytes - 3].decode("utf-8", "ignore").rstrip() + "…"


def _split(tag: object) -> tuple[str, str]:
    if not isinstance(tag, str):  # comments and processing instructions
        return "", ""
    if tag.startswith("{"):
        namespace, _, local = tag[1:].partition("}")
        return namespace, local.lower()
    return "", tag.lower()


def _children(element: ET.Element, name: str) -> list[ET.Element]:
    """Direct children called `name`, core RSS/Atom elements before extensions."""
    core, extension = [], []
    for child in element:
        namespace, local = _split(child.tag)
        if local == name:
            (core if namespace in _CORE_NAMESPACES else extension).append(child)
    return core + extension


def _text(element: ET.Element, *names: str) -> str:
    for name in names:
        for child in _children(element, name):
            text = "".join(child.itertext()).strip()
            if text:
                return text
    return ""


def _link(element: ET.Element) -> str:
    fallback = ""
    for child in _children(element, "link"):
        href = (child.get("href") or "").strip()
        if href:  # Atom style
            if child.get("rel", "alternate") == "alternate":
                return href
            fallback = fallback or href
        elif (child.text or "").strip():  # RSS style
            return child.text.strip()
    return fallback


def _parse_date(value: str) -> Optional[datetime]:
    value = value.strip()
    if not value:
        return None
    parsed = None
    try:
        parsed = parsedate_to_datetime(value)  # RFC 822, used by RSS
    except (TypeError, ValueError, IndexError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))  # Atom
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def parse_feed(data: bytes) -> list[Item]:
    """Return the feed's items in the order the feed lists them."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise FeedError(f"not valid XML ({exc})") from None

    if _split(root.tag)[1] not in ("rss", "feed", "rdf"):
        raise FeedError(f"not an RSS or Atom feed (root element <{_split(root.tag)[1]}>)")

    items: list[Item] = []
    ids: set[str] = set()
    for element in root.iter():
        if _split(element.tag)[1] not in ("item", "entry"):
            continue
        title = clean_text(_text(element, "title"))
        link = _link(element)
        date_text = _text(element, "pubdate", "published", "updated", "date")
        item_id = _text(element, "guid", "id") or link
        if not item_id:
            if not title:
                continue
            item_id = "sha1:" + hashlib.sha1(f"{title}|{date_text}".encode("utf-8")).hexdigest()
        if item_id in ids:
            continue
        ids.add(item_id)
        if link and urllib.parse.urlparse(link).scheme not in ("http", "https"):
            link = ""
        items.append(
            Item(
                id=item_id,
                title=title,
                link=link,
                published=_parse_date(date_text),
                summary=clean_text(_text(element, "description", "summary", "content", "encoded")),
            )
        )
    return items


def oldest_first(items: list[Item]) -> list[Item]:
    """Order items for delivery. Feeds list newest first, so reverse; use dates when all have one."""
    ordered = list(reversed(items))
    if all(item.published for item in ordered):
        ordered.sort(key=lambda item: item.published)
    return ordered


# --------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------


class Notifier:
    def __init__(self, server: str, topic: str, token: str = "") -> None:
        self.url = server.rstrip("/") + "/"
        self.topic = topic
        self.token = token

    def send(self, payload: dict) -> None:
        body = json.dumps({"topic": self.topic, **payload}).encode("utf-8")
        headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        http_send(request, attempts=PUBLISH_ATTEMPTS, what="notification failed")


class DryRunNotifier:
    def send(self, payload: dict) -> None:
        print("  would send: " + json.dumps(payload, ensure_ascii=False))


def make_notifier(dry_run: bool):
    if dry_run:
        return DryRunNotifier()
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        raise ConfigError(
            "NTFY_TOPIC is not set. In GitHub, add it under "
            "Settings > Secrets and variables > Actions > New repository secret."
        )
    server = os.environ.get("NTFY_SERVER", "").strip() or DEFAULT_SERVER
    _require_http_url(server, "NTFY_SERVER")
    return Notifier(server, topic, os.environ.get("NTFY_TOKEN", "").strip())


def _with_click(payload: dict, url: str) -> dict:
    if url:
        payload["click"] = url
    return payload


def item_payload(feed: Feed, item: Item) -> dict:
    message = feed.name
    if item.summary:
        message += " · " + clip(item.summary, MAX_SUMMARY_BYTES)
    payload = {"title": clip(item.title or "(untitled)", MAX_TITLE_BYTES), "message": message}
    return _with_click(payload, item.link or feed.site)


def digest_payload(feed: Feed, items: list[Item]) -> dict:
    lines: list[str] = []
    used = 0
    for item in items:
        line = "• " + clip(item.title or "(untitled)", 160)
        size = len(line.encode("utf-8")) + 1
        if used + size > MAX_MESSAGE_BYTES - 40:  # leave room for the "more" line
            break
        lines.append(line)
        used += size
    left_out = len(items) - len(lines)
    if left_out:
        lines.append(f"…and {left_out} more")
    payload = {"title": f"{feed.name}: {len(items)} new items", "message": "\n".join(lines)}
    return _with_click(payload, feed.site or (items[-1].link if items else ""))


def welcome_payload(feed: Feed, items: list[Item]) -> dict:
    message = "Set up and working. You will be notified about new items from here on."
    latest = max(items, key=lambda item: item.published) if all(i.published for i in items) else items[0]
    if latest.title:
        message += "\nLatest: " + clip(latest.title, 200)
    payload = {"title": f"Now watching {feed.name}", "message": message}
    return _with_click(payload, latest.link or feed.site)


# --------------------------------------------------------------------------
# The check itself
# --------------------------------------------------------------------------


@dataclass
class Result:
    feed: str
    outcome: str
    error: str = ""


def _remember(seen: list[str], new_ids: list[str], current_ids: set[str]) -> list[str]:
    """Append new ids, dropping the oldest ones that have left the feed once over the cap."""
    already = set(seen)
    merged = seen + [item_id for item_id in new_ids if item_id not in already]
    overflow = len(merged) - MAX_SEEN
    if overflow <= 0:
        return merged
    kept = []
    for item_id in merged:
        if overflow > 0 and item_id not in current_ids:
            overflow -= 1
            continue
        kept.append(item_id)
    return kept


def check_feed(feed: Feed, state: dict, notifier) -> Result:
    try:
        items = parse_feed(fetch_feed(feed.url))
    except (RequestFailed, FeedError) as exc:
        return Result(feed.name, "failed", str(exc))
    if not items:
        return Result(feed.name, "feed is empty, nothing to do")

    first_run = feed.url not in state["feeds"]
    seen: list[str] = [] if first_run else list(state["feeds"][feed.url].get("seen", []))
    seen_ids = set(seen)
    new_items = oldest_first([item for item in items if item.id not in seen_ids])

    delivered: list[Item] = []
    error = ""
    try:
        if first_run:
            notifier.send(welcome_payload(feed, items))
            delivered = new_items
            outcome = f"first run: {len(items)} existing items noted, confirmation sent"
        elif not new_items:
            outcome = "nothing new"
        elif len(new_items) > feed.max_individual:
            notifier.send(digest_payload(feed, new_items))
            delivered = new_items
            outcome = f"{len(new_items)} new, sent as one summary"
        else:
            for item in new_items:
                notifier.send(item_payload(feed, item))
                delivered.append(item)
            outcome = f"{len(new_items)} new, sent individually"
    except RequestFailed as exc:
        error = str(exc)
        if first_run:
            outcome = "first run: confirmation could not be sent; will try again next run"
        else:
            outcome = f"{len(delivered)} of {len(new_items)} new items sent; the rest will retry next run"

    if delivered:
        entry = state["feeds"].setdefault(feed.url, {})
        entry["name"] = feed.name
        entry["seen"] = _remember(seen, [item.id for item in delivered], {item.id for item in items})
    return Result(feed.name, outcome, error)


def report(results: list[Result]) -> None:
    in_actions = os.environ.get("GITHUB_ACTIONS") == "true"
    for result in results:
        print(f"{result.feed}: {result.outcome}")
        if result.error:
            print(f"  error: {result.error}", file=sys.stderr)
            if in_actions:
                print(f"::error title={result.feed}::{result.error}")

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        lines = ["| Feed | Result |", "| --- | --- |"]
        for result in results:
            detail = result.outcome + (f" ({result.error})" if result.error else "")
            lines.append(f"| {result.feed} | {detail} |")
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")


def main(argv: Optional[list[str]] = None) -> int:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, default=here / "feeds.json", help="feed list (default: feeds.json)")
    parser.add_argument("--state", type=Path, default=here / "state" / "seen.json", help="seen-item state file")
    parser.add_argument("--dry-run", action="store_true", help="print notifications instead of sending; state is not saved")
    args = parser.parse_args(argv)

    try:
        feeds = load_config(args.config)
        state = load_state(args.state)
        notifier = make_notifier(args.dry_run)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print(f"::error title=Setup::{exc}")
        return 2

    before = json.dumps(state, sort_keys=True)
    results: list[Result] = []
    try:
        for feed in feeds:
            results.append(check_feed(feed, state, notifier))
    finally:
        if not args.dry_run and json.dumps(state, sort_keys=True) != before:
            save_state(args.state, state)

    report(results)
    return 1 if any(result.error for result in results) else 0


if __name__ == "__main__":
    sys.exit(main())
