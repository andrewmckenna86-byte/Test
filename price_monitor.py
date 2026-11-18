#!/usr/bin/env python3
"""Price drop monitor.

Scrapes product pages listed in a YAML config, remembers the last seen price
for each one, and sends a notification (email, ntfy push, and/or webhook)
when a price goes down.

Usage:
    python price_monitor.py                 # check once (for cron / GitHub Actions)
    python price_monitor.py --loop 4h       # keep running, check every 4 hours
    python price_monitor.py --test-notify   # send a test notification and exit
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import smtplib
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Iterable

import requests
import yaml
from bs4 import BeautifulSoup

log = logging.getLogger("price_monitor")

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


# --------------------------------------------------------------------------- #
# Price parsing
# --------------------------------------------------------------------------- #
_NUMBER_RE = re.compile(r"\d[\d.,\s  ']*")


def parse_price(text: str | float | int | None) -> float | None:
    """Turn a price string like '$1,299.99', '1.299,99 €' or '£15' into a float."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    match = _NUMBER_RE.search(str(text))
    if not match:
        return None
    num = re.sub(r"[\s  ']", "", match.group()).rstrip(".,")
    if not num:
        return None

    last_dot, last_comma = num.rfind("."), num.rfind(",")
    if last_dot != -1 and last_comma != -1:
        # Whichever separator appears last is the decimal separator.
        if last_comma > last_dot:
            num = num.replace(".", "").replace(",", ".")
        else:
            num = num.replace(",", "")
    elif last_comma != -1:
        # Only commas: "1,299" (thousands) vs "12,99" (decimal).
        decimals = len(num) - last_comma - 1
        if num.count(",") == 1 and decimals in (1, 2):
            num = num.replace(",", ".")
        else:
            num = num.replace(",", "")
    elif num.count(".") > 1:
        # "1.299.000" -> thousands separators
        num = num.replace(".", "")
    try:
        return float(num)
    except ValueError:
        return None


def _iter_jsonld_offers(node: Any) -> Iterable[Any]:
    """Yield every price-looking value found under 'offers' in JSON-LD data."""
    if isinstance(node, list):
        for item in node:
            yield from _iter_jsonld_offers(item)
    elif isinstance(node, dict):
        if "@graph" in node:
            yield from _iter_jsonld_offers(node["@graph"])
        offers = node.get("offers")
        if offers is not None:
            for offer in offers if isinstance(offers, list) else [offers]:
                if isinstance(offer, dict):
                    for key in ("price", "lowPrice"):
                        if offer.get(key) not in (None, ""):
                            yield offer[key]
                    spec = offer.get("priceSpecification")
                    if isinstance(spec, dict) and spec.get("price") not in (None, ""):
                        yield spec["price"]


def extract_price(html: str, selector: str | None = None) -> float | None:
    """Find the price on a page.

    If a CSS selector is given it is used exclusively. Otherwise we try, in
    order: JSON-LD structured data, common meta tags, itemprop="price", and a
    handful of class names that many shops use.
    """
    soup = BeautifulSoup(html, "html.parser")

    if selector:
        el = soup.select_one(selector)
        if el is None:
            return None
        return parse_price(el.get("content") or el.get("value") or el.get_text(" ", strip=True))

    # 1. JSON-LD (schema.org Product / Offer)
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for raw in _iter_jsonld_offers(data):
            price = parse_price(raw)
            if price:
                return price

    # 2. Meta tags
    for attrs in (
        {"property": "product:price:amount"},
        {"property": "og:price:amount"},
        {"itemprop": "price"},
        {"name": "twitter:data1"},
    ):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            price = parse_price(tag["content"])
            if price:
                return price

    # 3. Microdata on a visible element
    el = soup.find(attrs={"itemprop": "price"})
    if el:
        price = parse_price(el.get("content") or el.get_text(" ", strip=True))
        if price:
            return price

    # 4. Common class / id patterns
    for sel in (
        ".a-price .a-offscreen",
        "#priceblock_ourprice",
        "#priceblock_dealprice",
        ".price-current",
        ".product-price",
        ".sale-price",
        ".price",
        "[data-price]",
    ):
        el = soup.select_one(sel)
        if el:
            price = parse_price(el.get("data-price") or el.get_text(" ", strip=True))
            if price:
                return price
    return None


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
class PriceStore:
    """Small JSON file holding the last known price and history per product."""

    def __init__(self, path: Path, history_limit: int = 200):
        self.path = path
        self.history_limit = history_limit
        self.data: dict[str, dict[str, Any]] = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text())
            except json.JSONDecodeError:
                log.warning("State file %s is corrupt; starting fresh", path)

    def get(self, url: str) -> dict[str, Any] | None:
        return self.data.get(url)

    def record(self, url: str, name: str, price: float) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        entry = self.data.setdefault(url, {"name": name, "history": []})
        entry["name"] = name
        entry["last_price"] = price
        entry["last_checked"] = now
        entry["lowest_price"] = min(price, entry.get("lowest_price", price))
        history = entry["history"]
        if not history or history[-1]["price"] != price:
            history.append({"price": price, "time": now})
            del history[: -self.history_limit]

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True))
        tmp.replace(self.path)


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #
@dataclass
class PriceDrop:
    name: str
    url: str
    old_price: float
    new_price: float
    currency: str = ""
    target_price: float | None = None

    @property
    def amount(self) -> float:
        return self.old_price - self.new_price

    @property
    def percent(self) -> float:
        return (self.amount / self.old_price * 100) if self.old_price else 0.0

    def fmt(self, value: float) -> str:
        return f"{self.currency}{value:,.2f}"

    def summary(self) -> str:
        line = (
            f"{self.name}: {self.fmt(self.old_price)} -> {self.fmt(self.new_price)} "
            f"(-{self.fmt(self.amount)}, -{self.percent:.1f}%)"
        )
        if self.target_price is not None and self.new_price <= self.target_price:
            line += f"  [at/below your target of {self.fmt(self.target_price)}]"
        return line


def _env(value: str | None) -> str | None:
    """Allow config values like '${SMTP_PASSWORD}' to come from the environment."""
    if isinstance(value, str):
        return os.path.expandvars(value)
    return value


def send_email(cfg: dict[str, Any], subject: str, body: str) -> None:
    host = _env(cfg.get("smtp_host")) or "smtp.gmail.com"
    port = int(_env(str(cfg.get("smtp_port", 587))))
    user = _env(cfg.get("username"))
    password = _env(cfg.get("password"))
    sender = _env(cfg.get("from")) or user
    recipients = cfg.get("to") or [user]
    if isinstance(recipients, str):
        recipients = [r.strip() for r in _env(recipients).split(",") if r.strip()]

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)

    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30) as smtp:
            if user:
                smtp.login(user, password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls()
            if user:
                smtp.login(user, password)
            smtp.send_message(msg)


def send_ntfy(cfg: dict[str, Any], subject: str, body: str, click_url: str | None) -> None:
    server = (_env(cfg.get("server")) or "https://ntfy.sh").rstrip("/")
    topic = _env(cfg["topic"])
    headers = {"Title": subject.encode("utf-8"), "Tags": "moneybag", "Priority": "high"}
    if click_url:
        headers["Click"] = click_url
    token = _env(cfg.get("token"))
    if token:
        headers["Authorization"] = f"Bearer {token}"
    requests.post(f"{server}/{topic}", data=body.encode("utf-8"), headers=headers, timeout=30).raise_for_status()


def send_webhook(cfg: dict[str, Any], subject: str, body: str) -> None:
    """Works with Slack ('text') and Discord ('content') incoming webhooks."""
    url = _env(cfg["url"])
    text = f"*{subject}*\n{body}"
    requests.post(url, json={"text": text, "content": text}, timeout=30).raise_for_status()


def notify(config: dict[str, Any], drops: list[PriceDrop]) -> None:
    if not drops:
        return
    ncfg = config.get("notifications", {})
    if len(drops) == 1:
        subject = f"Price drop: {drops[0].name}"
    else:
        subject = f"Price drops on {len(drops)} items"
    body = "\n\n".join(f"{d.summary()}\n{d.url}" for d in drops)

    channels = 0
    for name, sender in (
        ("email", lambda c: send_email(c, subject, body)),
        ("ntfy", lambda c: send_ntfy(c, subject, body, drops[0].url)),
        ("webhook", lambda c: send_webhook(c, subject, body)),
    ):
        c = ncfg.get(name)
        if not c or not c.get("enabled", True):
            continue
        channels += 1
        try:
            sender(c)
            log.info("Sent %s notification", name)
        except Exception as exc:  # noqa: BLE001 - keep going with other channels
            log.error("Failed to send %s notification: %s", name, exc)

    if channels == 0:
        log.warning("No notification channels configured; drop(s) only logged:\n%s", body)


# --------------------------------------------------------------------------- #
# Main check
# --------------------------------------------------------------------------- #
def fetch(url: str, headers: dict[str, str], retries: int = 3) -> str:
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"Could not fetch {url}: {last_exc}")


def is_reportable(old: float, new: float, product: dict[str, Any], defaults: dict[str, Any]) -> bool:
    """Decide whether a drop from `old` to `new` is worth a notification."""
    if new >= old:
        return False
    min_pct = float(product.get("min_drop_percent", defaults.get("min_drop_percent", 0)))
    min_abs = float(product.get("min_drop_amount", defaults.get("min_drop_amount", 0)))
    drop = old - new
    if drop < min_abs or (drop / old * 100) < min_pct:
        return False
    target = product.get("target_price")
    if target is not None and product.get("only_below_target", False):
        return new <= float(target)
    return True


def check_all(config: dict[str, Any], store: PriceStore, notify_fn=notify) -> list[PriceDrop]:
    defaults = config.get("defaults", {})
    headers = {**DEFAULT_HEADERS, **config.get("headers", {})}
    drops: list[PriceDrop] = []

    products = config.get("products", [])
    for i, product in enumerate(products):
        url = product["url"]
        name = product.get("name", url)
        try:
            html = fetch(url, headers)
            price = extract_price(html, product.get("selector"))
        except Exception as exc:  # noqa: BLE001
            log.error("%s: %s", name, exc)
            continue
        if price is None:
            log.error("%s: could not find a price on the page (try setting 'selector')", name)
            continue

        prev = store.get(url)
        old = prev.get("last_price") if prev else None
        if old is None:
            log.info("%s: first check, price is %.2f", name, price)
        elif is_reportable(old, price, product, defaults):
            drop = PriceDrop(
                name=name,
                url=url,
                old_price=old,
                new_price=price,
                currency=product.get("currency", defaults.get("currency", "")),
                target_price=product.get("target_price"),
            )
            log.info("PRICE DROP %s", drop.summary())
            drops.append(drop)
        elif price != old:
            log.info("%s: price changed %.2f -> %.2f (not reported)", name, old, price)
        else:
            log.info("%s: unchanged at %.2f", name, price)
        store.record(url, name, price)

        if i < len(products) - 1:
            time.sleep(random.uniform(1.0, 3.0))  # be polite between requests

    store.save()
    notify_fn(config, drops)
    return drops


def parse_interval(text: str) -> float:
    """'4h' -> 14400, '30m' -> 1800, '90' -> 90 seconds."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*", text.lower())
    if not m:
        raise argparse.ArgumentTypeError(f"invalid interval: {text!r}")
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def load_config(path: Path) -> dict[str, Any]:
    with path.open() as fh:
        return yaml.safe_load(fh) or {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Notify me when a product's price drops.")
    parser.add_argument("-c", "--config", default="config.yaml", type=Path)
    parser.add_argument("-s", "--state", default="prices.json", type=Path)
    parser.add_argument("--loop", type=parse_interval, metavar="INTERVAL",
                        help="keep running, checking every INTERVAL (e.g. 4h, 30m)")
    parser.add_argument("--test-notify", action="store_true",
                        help="send a fake price-drop notification and exit")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    config = load_config(args.config)

    if args.test_notify:
        notify(config, [PriceDrop("Test product", "https://example.com", 100.0, 79.99, "$")])
        return 0

    while True:
        check_all(config, PriceStore(args.state))
        if not args.loop:
            return 0
        # Add up to 10% jitter so requests don't land at exactly the same time.
        wait = args.loop * random.uniform(1.0, 1.1)
        log.info("Next check in %.0f minutes", wait / 60)
        time.sleep(wait)
        config = load_config(args.config)  # pick up edits without restarting


if __name__ == "__main__":
    sys.exit(main())
