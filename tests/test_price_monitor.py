import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import price_monitor as pm  # noqa: E402


@pytest.mark.parametrize("text,expected", [
    ("$1,299.99", 1299.99),
    ("1.299,99 €", 1299.99),
    ("£15", 15.0),
    ("12,99", 12.99),
    ("1,299", 1299.0),
    ("Now only USD 49.50!", 49.5),
    ("1 299,00 kr", 1299.0),
    (19.95, 19.95),
    ("Sold out", None),
    (None, None),
])
def test_parse_price(text, expected):
    assert pm.parse_price(text) == expected


def test_extract_with_selector():
    html = '<div><span class="p">$10.00</span><span id="x">$7.49</span></div>'
    assert pm.extract_price(html, "#x") == 7.49
    assert pm.extract_price(html, "#missing") is None


def test_extract_jsonld():
    data = {"@context": "https://schema.org", "@type": "Product",
            "offers": {"@type": "Offer", "price": "249.00", "priceCurrency": "USD"}}
    html = f'<script type="application/ld+json">{json.dumps(data)}</script><p class="price">$999</p>'
    assert pm.extract_price(html) == 249.0


def test_extract_meta_and_class():
    assert pm.extract_price('<meta property="product:price:amount" content="33.10">') == 33.10
    assert pm.extract_price('<span class="price">€ 5,49</span>') == 5.49


def test_is_reportable_thresholds():
    assert pm.is_reportable(100, 90, {}, {})
    assert not pm.is_reportable(100, 100, {}, {})
    assert not pm.is_reportable(100, 110, {}, {})
    assert not pm.is_reportable(100, 98, {"min_drop_percent": 5}, {})
    assert pm.is_reportable(100, 94, {}, {"min_drop_percent": 5})
    assert not pm.is_reportable(100, 90, {"target_price": 80, "only_below_target": True}, {})
    assert pm.is_reportable(100, 79, {"target_price": 80, "only_below_target": True}, {})


def test_check_all_detects_drop(tmp_path, monkeypatch):
    prices = iter(["$100.00", "$100.00", "$85.00", "$90.00"])
    monkeypatch.setattr(pm, "fetch", lambda url, headers: f'<span class="price">{next(prices)}</span>')
    monkeypatch.setattr(pm.time, "sleep", lambda s: None)
    config = {"products": [{"name": "Thing", "url": "https://shop/x", "currency": "$"}]}
    state = tmp_path / "prices.json"
    sent = []

    def run():
        return pm.check_all(config, pm.PriceStore(state), notify_fn=lambda c, d: sent.extend(d))

    assert run() == []            # first sighting: baseline only
    assert run() == []            # unchanged
    drops = run()                 # 100 -> 85
    assert len(drops) == 1 and drops[0].old_price == 100 and drops[0].new_price == 85
    assert "-15.0%" in drops[0].summary()
    assert run() == []            # increase is not a drop
    saved = json.loads(state.read_text())["https://shop/x"]
    assert saved["last_price"] == 90 and saved["lowest_price"] == 85
    assert [h["price"] for h in saved["history"]] == [100, 85, 90]
    assert len(sent) == 1


def test_parse_interval():
    assert pm.parse_interval("4h") == 14400
    assert pm.parse_interval("30m") == 1800
    assert pm.parse_interval("90") == 90
