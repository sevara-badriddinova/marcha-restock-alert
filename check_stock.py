import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time, timezone
from typing import Dict, List, Optional, Tuple
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


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").lower() in {"1", "true", "yes"}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_state_shape(state: Dict) -> Dict:
    state.setdefault("products", {})
    state.setdefault("telegram", {})
    state["telegram"].setdefault("subscribers", {})
    state["telegram"].setdefault("last_update_id", None)
    return state


def load_state() -> Dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            state = json.load(file)
    except FileNotFoundError:
        state = {}
    except json.JSONDecodeError:
        print("state.json is not valid JSON. Starting with an empty state.")
        state = {}

    return ensure_state_shape(state)


def save_state(state: Dict) -> None:
    ensure_state_shape(state)
    state["last_checked_at"] = utc_now_iso()
    with open(STATE_FILE, "w", encoding="utf-8") as file:
        json.dump(state, file, indent=2, sort_keys=True)
        file.write("\n")


def is_business_hours_japan_now() -> bool:
    if env_flag("SKIP_BUSINESS_HOURS_CHECK"):
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
            "last_seen_at": utc_now_iso(),
        }

    current_ids = {result["id"] for result in results}
    missing_ids = sorted(set(products_state) - current_ids)
    if missing_ids:
        print(f"{len(missing_ids)} products are no longer on the catalog page. Their saved state was kept.")


def get_telegram_updates(bot_token: str, offset: Optional[int]) -> List[Dict]:
    telegram_url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
    params = {"timeout": 0, "allowed_updates": json.dumps(["message"])}
    if offset is not None:
        params["offset"] = offset

    response = requests.get(telegram_url, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
    if not response.ok:
        print(f"Telegram getUpdates status: {response.status_code}")
        print(f"Telegram getUpdates body: {response.text}")
    response.raise_for_status()

    data = response.json()
    if not data.get("ok"):
        raise requests.RequestException(f"Telegram getUpdates returned ok=false: {data}")

    return data.get("result", [])


def send_telegram_message(bot_token: str, chat_id: str, message: str) -> None:
    telegram_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    print(f"Sending Telegram message to {chat_id} with {len(message)} characters.")

    payload = {
        "chat_id": chat_id,
        "text": message,
        "disable_web_page_preview": True,
    }

    response = requests.post(telegram_url, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
    if not response.ok:
        print(f"Telegram sendMessage status: {response.status_code}")
        print(f"Telegram sendMessage body: {response.text}")

    response.raise_for_status()


def split_blocks_into_messages(title: str, blocks: List[str]) -> List[str]:
    body_chunk_limit = TELEGRAM_CHUNK_LIMIT - 100
    chunks = []
    current = title.strip()

    for block in blocks:
        next_text = f"{current}\n\n{block}" if current else block
        if len(next_text) > body_chunk_limit and current != title.strip():
            chunks.append(current)
            current = f"{title.strip()}\n\n{block}"
        else:
            current = next_text

    if current:
        chunks.append(current)

    total = len(chunks)
    return [f"Part {index}/{total}\n\n{chunk}" for index, chunk in enumerate(chunks, start=1)]


def send_telegram_messages(bot_token: str, chat_id: str, messages: List[str]) -> None:
    for index, message in enumerate(messages, start=1):
        print(f"Sending Telegram chunk {index}/{len(messages)} to {chat_id}.")
        send_telegram_message(bot_token, chat_id, message)


def try_send_telegram_messages(bot_token: str, chat_id: str, messages: List[str], context: str) -> bool:
    try:
        send_telegram_messages(bot_token, chat_id, messages)
        return True
    except requests.RequestException as error:
        print(f"Telegram failed while sending {context} to {chat_id}: {error}")
        return False


def command_from_text(text: str) -> str:
    command = text.strip().split()[0].lower()
    return command.split("@")[0]


def welcome_message() -> str:
    return (
        "🍵 Matcha Restock Bot 🍵\n\n"
        "✅ You're subscribed to Marukyu Koyamaen matcha restock alerts 🔔\n\n"
        "📦 I'll notify you only when a product comes back in stock ✨\n\n"
        "🤖 Commands:\n"
        "📊 /status - see currently available products\n"
        "📝 /all - see inventory summary/full list\n"
        "🛑 /stop - unsubscribe"
    )


def help_message() -> str:
    return (
        "Available commands:\n"
        "/start - subscribe to restock alerts\n"
        "/status - see currently available products\n"
        "/all - see inventory summary/full list\n"
        "/stop - unsubscribe"
    )


def product_block(product: Dict) -> str:
    return "\n".join([product.get("name", "Unknown product"), product.get("url", "")]).strip()


def build_status_messages(state: Dict) -> List[str]:
    products = state.get("products", {})
    if not products:
        return ["Inventory has not been checked yet. Try again after the next scheduled check."]

    in_stock_products = [product for product in products.values() if product.get("status") == IN_STOCK]
    if not in_stock_products:
        return ["No matcha products appear in stock right now. I'll notify you when something restocks."]

    blocks = [product_block(product) for product in sorted(in_stock_products, key=lambda item: item.get("name", ""))]
    title = f"Currently IN_STOCK matcha products: {len(in_stock_products)}"
    return split_blocks_into_messages(title, blocks)


def build_all_inventory_messages(state: Dict) -> List[str]:
    products = state.get("products", {})
    if not products:
        return ["Inventory has not been checked yet. Try again after the next scheduled check."]

    grouped = {
        IN_STOCK: [],
        OUT_OF_STOCK: [],
        UNKNOWN: [],
        REQUEST_FAILED: [],
    }
    for product in products.values():
        status = product.get("status", UNKNOWN)
        grouped.setdefault(status, []).append(product)

    unknown_count = len(grouped.get(UNKNOWN, [])) + len(grouped.get(REQUEST_FAILED, []))
    title = "\n".join(
        [
            "Matcha inventory summary",
            f"{IN_STOCK}: {len(grouped.get(IN_STOCK, []))}",
            f"{OUT_OF_STOCK}: {len(grouped.get(OUT_OF_STOCK, []))}",
            f"{UNKNOWN}/REQUEST_FAILED: {unknown_count}",
        ]
    )

    blocks = []
    for status in [IN_STOCK, OUT_OF_STOCK, UNKNOWN, REQUEST_FAILED]:
        status_products = sorted(grouped.get(status, []), key=lambda item: item.get("name", ""))
        if not status_products:
            continue

        blocks.append(status)
        blocks.extend(product_block(product) for product in status_products)

    return split_blocks_into_messages(title, blocks)


def process_telegram_commands(state: Dict, bot_token: str) -> bool:
    telegram_state = state.setdefault("telegram", {})
    subscribers = telegram_state.setdefault("subscribers", {})
    last_update_id = telegram_state.get("last_update_id")
    offset = last_update_id + 1 if last_update_id is not None else None

    try:
        updates = get_telegram_updates(bot_token, offset)
    except requests.RequestException as error:
        print(f"Could not fetch Telegram updates: {error}")
        return False

    if not updates:
        print("No new Telegram commands.")
        return False

    print(f"Processing {len(updates)} Telegram update(s).")
    changed = False
    max_update_id = last_update_id

    for update in updates:
        update_id = update.get("update_id")
        if update_id is not None:
            max_update_id = update_id if max_update_id is None else max(max_update_id, update_id)

        message = update.get("message") or {}
        text = (message.get("text") or "").strip()
        chat = message.get("chat") or {}
        chat_id = chat.get("id")

        if not text.startswith("/") or chat_id is None:
            continue

        chat_id_text = str(chat_id)
        command = command_from_text(text)

        if command == "/start":
            subscribers[chat_id_text] = {
                "first_name": chat.get("first_name"),
                "username": chat.get("username"),
                "subscribed_at": subscribers.get(chat_id_text, {}).get("subscribed_at", utc_now_iso()),
            }
            changed = True
            try_send_telegram_messages(bot_token, chat_id_text, [welcome_message()], "/start welcome")

        elif command == "/status":
            try_send_telegram_messages(bot_token, chat_id_text, build_status_messages(state), "/status")

        elif command == "/all":
            try_send_telegram_messages(bot_token, chat_id_text, build_all_inventory_messages(state), "/all")

        elif command == "/stop":
            if chat_id_text in subscribers:
                del subscribers[chat_id_text]
                changed = True
            try_send_telegram_messages(
                bot_token,
                chat_id_text,
                ["You're unsubscribed. Send /start anytime to subscribe again."],
                "/stop confirmation",
            )

        else:
            try_send_telegram_messages(bot_token, chat_id_text, [help_message()], "unknown command help")

    if max_update_id is not None and max_update_id != last_update_id:
        telegram_state["last_update_id"] = max_update_id
        changed = True

    return changed


def build_change_block(change: Dict[str, str]) -> str:
    arrow = f"{change['previous_status']} -> {change['current_status']}"
    return "\n".join([change["name"], arrow, change["url"]])


def changes_to_messages(title: str, changes: List[Dict[str, str]]) -> List[str]:
    blocks = [build_change_block(change) for change in changes]
    return split_blocks_into_messages(title, blocks)


def subscriber_chat_ids(state: Dict) -> List[str]:
    subscribers = state.get("telegram", {}).get("subscribers", {})
    return sorted(str(chat_id) for chat_id in subscribers)


def alert_chat_ids(state: Dict, fallback_chat_id: Optional[str]) -> List[str]:
    chat_ids = subscriber_chat_ids(state)
    if not chat_ids and fallback_chat_id:
        chat_ids.append(str(fallback_chat_id))
    return chat_ids


def send_stock_alerts(
    state: Dict,
    bot_token: str,
    fallback_chat_id: Optional[str],
    admin_chat_id: Optional[str],
    changes: List[Dict[str, str]],
) -> bool:
    restock_changes = [
        change
        for change in changes
        if change["current_status"] == IN_STOCK
        and change["previous_status"] in {OUT_OF_STOCK, UNKNOWN}
    ]

    success = True
    if restock_changes:
        messages = changes_to_messages("Matcha restock alert", restock_changes)
        for chat_id in alert_chat_ids(state, fallback_chat_id):
            if not try_send_telegram_messages(bot_token, chat_id, messages, "subscriber restock alert"):
                success = False
    else:
        print("No subscriber restock alerts needed.")

    if admin_chat_id:
        admin_messages = changes_to_messages("Admin matcha stock changes", changes)
        if not try_send_telegram_messages(bot_token, str(admin_chat_id), admin_messages, "admin stock alert"):
            success = False

    return success


def run_product_checks(state: Dict, bot_token: str, fallback_chat_id: Optional[str], admin_chat_id: Optional[str]) -> bool:
    with requests.Session() as session:
        try:
            print(f"Fetching catalog: {CATALOG_URL}")
            catalog_html = fetch_html(session, CATALOG_URL)
        except Exception as error:
            print(f"Catalog request failed: {error}")
            print("No product checks ran. Product state was not changed.")
            return False

    products = extract_products_from_catalog(catalog_html)
    if not products:
        print("No products were found on the catalog page. Product state was not changed.")
        return False

    print(f"Found {len(products)} unique Matcha product URLs.")

    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_product = {executor.submit(check_product, product): product for product in products}
        for future in as_completed(future_to_product):
            results.append(future.result())

    changes = find_status_changes(state, results)

    if env_flag("BASELINE_ONLY"):
        print("BASELINE_ONLY=true, saving current statuses without stock-change Telegram alerts.")
        update_state_with_successful_results(state, results)
        return True

    if changes:
        alerts_sent = send_stock_alerts(state, bot_token, fallback_chat_id, admin_chat_id, changes)
        if not alerts_sent:
            print("At least one Telegram stock-change alert failed.")
            print("Product state was not updated so the alert can be retried next time.")
            return False
        print(f"Stock alerts processed for {len(changes)} change(s).")
    else:
        print("No stock alert needed. No product status changed.")

    update_state_with_successful_results(state, results)
    return True


def main() -> None:
    bot_token = os.environ.get("BOT_TOKEN")
    fallback_chat_id = os.environ.get("CHAT_ID")
    admin_chat_id = os.environ.get("ADMIN_CHAT_ID")

    if not bot_token:
        raise ValueError("Missing BOT_TOKEN environment variable.")

    state = load_state()
    telegram_state_changed = process_telegram_commands(state, bot_token)

    if not is_business_hours_japan_now():
        if telegram_state_changed:
            save_state(state)
            print("state.json saved with Telegram command updates.")
        return

    product_state_changed = run_product_checks(state, bot_token, fallback_chat_id, admin_chat_id)

    if telegram_state_changed or product_state_changed:
        save_state(state)
        print("state.json saved.")


if __name__ == "__main__":
    main()
