import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time, timezone
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse, urlunparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup


CATALOG_URL = "https://www.marukyu-koyamaen.co.jp/english/shop/products/catalog/matcha?viewall=1"
STATE_FILE = "state.json"

IN_STOCK = "IN_STOCK"
OUT_OF_STOCK = "OUT_OF_STOCK"
UNKNOWN = "UNKNOWN"
REQUEST_FAILED = "REQUEST_FAILED"

MAX_WORKERS = 6
REQUEST_TIMEOUT_SECONDS = 20
TELEGRAM_CHUNK_LIMIT = 3500


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


def is_business_hours_japan_now() -> bool:
    if os.environ.get("SKIP_BUSINESS_HOURS_CHECK", "").lower() in {"1", "true", "yes"}:
        print("Skipping Japan business-hours guard because SKIP_BUSINESS_HOURS_CHECK is enabled.")
        return True

    now = datetime.now(ZoneInfo("Asia/Tokyo"))
    start = time(9, 0)
    end = time(17, 30)

    if now.weekday() >= 5:
        print(f"Outside business hours in Japan: {now:%Y-%m-%d %H:%M:%S %Z} is a weekend.")
        return False

    if not (start <= now.time() <= end):
        print(
            f"Outside business hours in Japan: {now:%Y-%m-%d %H:%M:%S %Z}. "
            "Allowed window is Monday-Friday 09:00-17:30 JST."
        )
        return False

    print(f"Inside business hours in Japan: {now:%Y-%m-%d %H:%M:%S %Z}.")
    return True


def normalize_product_url(url: str) -> str:
    parsed = urlparse(urljoin(CATALOG_URL, url))
    path = parsed.path.rstrip("/")
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def product_id_from_url(url: str) -> str:
    return urlparse(url).path.rstrip("/").split("/")[-1].lower()


def load_state() -> Dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            state = json.load(file)
    except FileNotFoundError:
        return {"products": {}}
    except json.JSONDecodeError:
        print("state.json is not valid JSON. Keeping all previous statuses as UNKNOWN.")
        return {"products": {}}

    if "products" not in state:
        old_status = state.get("last_status", UNKNOWN)
        return {"products": {}, "old_last_status": old_status}

    return state


def save_state(state: Dict) -> None:
    state["last_checked_at"] = datetime.now(timezone.utc).isoformat()
    with open(STATE_FILE, "w", encoding="utf-8") as file:
        json.dump(state, file, indent=2, sort_keys=True)
        file.write("\n")


def fetch_html(session: requests.Session, url: str) -> str:
    response = session.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT_SECONDS, allow_redirects=True)
    response.raise_for_status()

    html = response.text
    if not html.strip():
        raise ValueError("Empty HTML response.")

    if "Just a moment..." in html and "challenges.cloudflare.com" in html:
        raise ValueError("Cloudflare challenge page received instead of shop HTML.")

    return html


def extract_products_from_catalog(html: str) -> List[Dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    products = []
    seen_urls = set()

    for card in soup.select("li.product"):
        link = card.select_one('a[href*="/english/shop/products/"]')
        if not link or not link.get("href"):
            continue

        url = normalize_product_url(link["href"])
        if "/catalog/" in url or url in seen_urls:
            continue

        seen_urls.add(url)
        products.append(
            {
                "id": product_id_from_url(url),
                "url": url,
                "name": card.get_text(" ", strip=True),
                "catalog_classes": " ".join(card.get("class", [])),
            }
        )

    return products


def parse_structured_data_status(soup: BeautifulSoup) -> Optional[str]:
    for script in soup.select('script[type="application/ld+json"]'):
        text = script.string or script.get_text()
        lowered = text.lower()

        if "schema.org/outofstock" in lowered:
            return OUT_OF_STOCK
        if "schema.org/instock" in lowered:
            return IN_STOCK

    return None


def parse_product_page_status(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

    structured_status = parse_structured_data_status(soup)
    if structured_status:
        return structured_status

    product = soup.select_one('[id^="product-"].product')
    if product:
        classes = set(product.get("class", []))
        if "outofstock" in classes:
            return OUT_OF_STOCK
        if "instock" in classes:
            return IN_STOCK

    for element in soup.select(".stock, .single-stock-status"):
        classes = set(element.get("class", []))
        text = element.get_text(" ", strip=True).lower()
        if "out-of-stock" in classes or "currently out of stock" in text:
            return OUT_OF_STOCK

    main = soup.select_one("#single-product") or soup.select_one("main") or soup
    main_text = main.get_text(" ", strip=True).lower()
    out_of_stock_phrases = [
        "this product is currently out of stock and unavailable",
        "currently out of stock",
        "out of stock and unavailable",
    ]

    if any(phrase in main_text for phrase in out_of_stock_phrases):
        return OUT_OF_STOCK

    return IN_STOCK


def check_product(product: Dict[str, str]) -> Dict[str, str]:
    with requests.Session() as session:
        try:
            html = fetch_html(session, product["url"])
            status = parse_product_page_status(html)
            print(f"{product['id']}: {status} - {product['name'][:80]}")
            return {**product, "status": status}
        except Exception as error:
            print(f"{product['id']}: request/parser failed for {product['url']}: {error}")
            return {**product, "status": REQUEST_FAILED, "error": str(error)}


def find_status_changes(state: Dict, results: List[Dict[str, str]]) -> List[Dict[str, str]]:
    changes = []
    products_state = state.get("products", {})

    for result in results:
        if result["status"] == REQUEST_FAILED:
            continue

        previous = products_state.get(result["id"], {}).get("status", UNKNOWN)
        current = result["status"]

        if previous != current:
            changes.append({**result, "previous_status": previous, "current_status": current})

    return changes


def update_state_with_successful_results(state: Dict, results: List[Dict[str, str]]) -> None:
    products_state = state.setdefault("products", {})

    for result in results:
        if result["status"] == REQUEST_FAILED:
            print(f"{result['id']}: state not updated because the product check failed.")
            continue

        products_state[result["id"]] = {
            "name": result["name"],
            "url": result["url"],
            "status": result["status"],
            "last_seen_at": datetime.now(timezone.utc).isoformat(),
        }

    current_ids = {result["id"] for result in results}
    missing_ids = sorted(set(products_state) - current_ids)
    if missing_ids:
        print(f"{len(missing_ids)} products are no longer on the catalog page. Their saved state was kept.")


def build_change_block(change: Dict[str, str]) -> str:
    arrow = f"{change['previous_status']} -> {change['current_status']}"
    return "\n".join([change["name"], arrow, change["url"]])


def build_alert_messages(changes: List[Dict[str, str]]) -> List[str]:
    body_chunk_limit = TELEGRAM_CHUNK_LIMIT - 100
    header = "\n".join(
        [
            "Marukyu Koyamaen Matcha stock changes",
            f"Detected {len(changes)} change(s).",
            "",
        ]
    )
    chunks = []
    current = header

    for change in changes:
        block = build_change_block(change)
        next_text = f"{current}\n{block}\n"

        if len(next_text) > body_chunk_limit and current.strip() != header.strip():
            chunks.append(current.strip())
            current = f"{header}\n{block}\n"
        else:
            current = next_text

    if current.strip():
        chunks.append(current.strip())

    total = len(chunks)
    return [f"Part {index}/{total}\n\n{chunk}" for index, chunk in enumerate(chunks, start=1)]


def send_telegram_message(bot_token: str, chat_id: str, message: str) -> None:
    telegram_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    print(f"Sending Telegram message with {len(message)} characters.")

    payload = {
        "chat_id": chat_id,
        "text": message,
        "disable_web_page_preview": True,
    }

    response = requests.post(telegram_url, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
    if not response.ok:
        print(f"Telegram response status: {response.status_code}")
        print(f"Telegram response body: {response.text}")

    response.raise_for_status()


def send_telegram_messages(bot_token: str, chat_id: str, messages: List[str]) -> None:
    for index, message in enumerate(messages, start=1):
        print(f"Sending Telegram chunk {index}/{len(messages)}.")
        send_telegram_message(bot_token, chat_id, message)


def run_checks() -> None:
    bot_token = os.environ.get("BOT_TOKEN")
    chat_id = os.environ.get("CHAT_ID")

    if not bot_token:
        raise ValueError("Missing BOT_TOKEN environment variable.")
    if not chat_id:
        raise ValueError("Missing CHAT_ID environment variable.")

    state = load_state()

    with requests.Session() as session:
        try:
            print(f"Fetching catalog: {CATALOG_URL}")
            catalog_html = fetch_html(session, CATALOG_URL)
        except Exception as error:
            print(f"Catalog request failed: {error}")
            print("No product checks ran. State was not changed.")
            return

    products = extract_products_from_catalog(catalog_html)
    if not products:
        print("No products were found on the catalog page. State was not changed.")
        return

    print(f"Found {len(products)} unique Matcha product URLs.")

    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_product = {executor.submit(check_product, product): product for product in products}
        for future in as_completed(future_to_product):
            results.append(future.result())

    changes = find_status_changes(state, results)

    if os.environ.get("BASELINE_ONLY", "").lower() in {"1", "true", "yes"}:
        print("BASELINE_ONLY=true, saving current statuses without Telegram alerts.")
        update_state_with_successful_results(state, results)
        save_state(state)
        print("state.json saved.")
        return

    if changes:
        messages = build_alert_messages(changes)
        try:
            send_telegram_messages(bot_token, chat_id, messages)
            print(f"Telegram alert sent with {len(changes)} change(s) in {len(messages)} chunk(s).")
        except requests.RequestException as error:
            print(f"Telegram failed: {error}")
            print("state.json was not saved so the notification can be retried next time.")
            return
    else:
        print("No Telegram alert needed. No product status changed.")

    update_state_with_successful_results(state, results)
    save_state(state)
    print("state.json saved.")


def main() -> None:
    if not is_business_hours_japan_now():
        return

    run_checks()


if __name__ == "__main__":
    main()
