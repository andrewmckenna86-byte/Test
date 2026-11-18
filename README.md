# Price Drop Monitor

Checks product pages on a schedule and sends you an **email**, a **phone push
notification** ([ntfy](https://ntfy.sh)), or a **Slack/Discord message** when a
price goes down.

## How it works

1. `config.yaml` lists the product URLs to watch.
2. Each run fetches every page and finds the price. It uses your CSS
   `selector` if you give one. Otherwise it looks for the price automatically,
   in this order: schema.org JSON-LD, price meta tags, `itemprop="price"`, then
   common class names.
3. The last price, the lowest price and the price history are saved in
   `prices.json`.
4. If the new price is lower than the last one (and passes any thresholds you
   set), a notification goes out on every channel you enabled.

## Quick start (local)

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml     # then edit the products and notifications
export SMTP_USERNAME=you@gmail.com SMTP_PASSWORD=your-app-password EMAIL_TO=you@gmail.com

python price_monitor.py --test-notify  # check that notifications reach you
python price_monitor.py                # run one check
python price_monitor.py --loop 4h      # or keep it running, checking every ~4 hours
```

To use cron instead of `--loop`, this runs at 8am, 12pm, 4pm and 8pm:

```cron
0 8,12,16,20 * * * cd /path/to/repo && /usr/bin/python3 price_monitor.py >> monitor.log 2>&1
```

## Run it free on GitHub Actions (no server needed)

The workflow `.github/workflows/price-monitor.yml` runs every 4 hours and
commits the updated `prices.json` back to the repository, so the next run
knows the previous prices.

1. Commit your `config.yaml`. Keep secrets out of it: use `${VARS}` as in the
   example.
2. In the repository, go to **Settings → Secrets and variables → Actions** and
   add whichever of these you use: `SMTP_USERNAME`, `SMTP_PASSWORD`, `EMAIL_TO`,
   `NTFY_TOPIC`, `WEBHOOK_URL`.
3. Go to **Settings → Actions → General → Workflow permissions** and choose
   **Read and write**.
4. Open the **Actions** tab and click **Run workflow** to test it right away.

To change how often it runs, edit the `cron:` line. Times are in UTC.

## Notification channels

| Channel | Setup |
|---|---|
| Email | Any SMTP server. For Gmail, turn on 2-step verification, then create an [App Password](https://myaccount.google.com/apppasswords) and use it as `SMTP_PASSWORD`. |
| Push (ntfy) | Install the ntfy app on your phone and subscribe to a topic name that is hard to guess. Set `NTFY_TOPIC` to that name and set `enabled: true`. |
| Slack / Discord | Create an incoming webhook, set `WEBHOOK_URL` to its URL and set `enabled: true`. |

## Per-product options

```yaml
- name: Some gadget
  url: https://shop.example.com/gadget
  selector: ".product-price"   # optional CSS selector
  currency: "$"
  target_price: 199.99         # alert says when the price is at or below this
  only_below_target: true      # alert only when the price is at or below the target
  min_drop_percent: 5          # ignore small fluctuations
  min_drop_amount: 2
```

## Tips and limits

- **If the price isn't found**, open the page in your browser, right-click the
  price, choose *Inspect*, and copy a selector for it into `selector`.
- Some large retailers (Amazon, for example) block automated requests or
  build the price with JavaScript. A plain HTTP scraper may not work on those
  sites. Check each site's terms of service.
- Checking a few times a day is gentle enough. Don't poll every minute.

## Tests

```bash
pip install pytest && python -m pytest -q
```
