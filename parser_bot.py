import os
import json
import logging
import asyncio
import hashlib
from datetime import datetime
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
from telegram import Bot
from telegram.constants import ParseMode

# ─── Логирование ─────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s │ %(levelname)s │ %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

# ─── Конфиг из переменных окружения ──────────────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]   # Токен от @BotFather
MY_CHAT_ID     = os.environ["MY_CHAT_ID"]       # Ваш личный Telegram ID
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "300"))  # Секунд между проверками (300 = 5 мин)

# ─── Файл для хранения уже отправленных объявлений ───────────────────────────
SEEN_FILE = Path("seen_listings.json")

# ─── URL страниц 999.md которые мониторим ────────────────────────────────────
# o_33[]=2&o_33[]=3  — тип сделки: продажа
# is_owner=1         — только от собственника (без агентств)
PAGES_TO_MONITOR = [
    {
        "url": "https://999.md/ru/list/real-estate/apartments-and-rooms?applied=1&o_33[]=2&o_33[]=3&is_owner=1",
        "label": "Продажа от собственника",
        "emoji": "🏠"
    },
]

# Фильтр "от собственника" уже встроен в URL через параметр is_owner=1
# Дополнительная проверка по тексту НЕ используется —
# собственники часто пишут "агентствам не беспокоить" и бот ошибочно пропускал бы их

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ─── Загрузка / сохранение уже виденных объявлений ───────────────────────────
def load_seen() -> set:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()))
        except Exception:
            return set()
    return set()

def save_seen(seen: set):
    SEEN_FILE.write_text(json.dumps(list(seen)))

# ─── Парсинг страницы 999.md ─────────────────────────────────────────────────
async def fetch_listings(url: str) -> list[dict]:
    """Скачивает страницу и извлекает объявления."""
    listings = []
    try:
        async with httpx.AsyncClient(headers=HEADERS, timeout=30, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
    except Exception as e:
        log.error(f"Ошибка загрузки {url}: {e}")
        return listings

    soup = BeautifulSoup(resp.text, "html.parser")

    # Находим все карточки объявлений
    cards = soup.select("li.ads-list-photo-item, article.listing-item, .ads-list-photo-item")

    if not cards:
        # Альтернативный селектор
        cards = soup.select("[class*='listing'], [class*='ads-list']")

    log.info(f"Найдено карточек: {len(cards)}")

    for card in cards:
        try:
            listing = parse_card(card)
            if listing:
                listings.append(listing)
        except Exception as e:
            log.debug(f"Ошибка парсинга карточки: {e}")

    return listings

def parse_card(card) -> dict | None:
    """Извлекает данные из одной карточки объявления."""
    # Ссылка и ID
    link_el = card.select_one("a[href*='/ru/'], a[href*='/ro/']")
    if not link_el:
        link_el = card.select_one("a")
    if not link_el:
        return None

    href = link_el.get("href", "")
    if not href:
        return None
    if not href.startswith("http"):
        href = "https://999.md" + href

    # Уникальный ID объявления из URL
    listing_id = hashlib.md5(href.encode()).hexdigest()[:12]

    # Заголовок
    title_el = card.select_one(".ads-list-photo-item-title, h2, h3, .title, [class*='title']")
    title = title_el.get_text(strip=True) if title_el else "Без названия"

    # Цена
    price_el = card.select_one(".ads-list-photo-item-price, .price, [class*='price']")
    price = price_el.get_text(strip=True) if price_el else "Цена не указана"

    # Описание / параметры
    desc_el = card.select_one(".ads-list-photo-item-description, .description, [class*='desc']")
    desc = desc_el.get_text(strip=True) if desc_el else ""

    # Район / локация
    loc_el = card.select_one(".ads-list-photo-item-region, .region, [class*='region'], [class*='location']")
    location = loc_el.get_text(strip=True) if loc_el else ""

    # Дата
    date_el = card.select_one(".ads-list-photo-item-date, .date, time, [class*='date']")
    date = date_el.get_text(strip=True) if date_el else ""

    # Фото
    img_el = card.select_one("img")
    photo = img_el.get("src", "") if img_el else ""

    return {
        "id":       listing_id,
        "url":      href,
        "title":    title[:200],
        "price":    price[:100],
        "desc":     desc[:300],
        "location": location[:150],
        "date":     date[:50],
        "photo":    photo,
    }

# ─── Форматирование Telegram сообщения ───────────────────────────────────────
def format_message(listing: dict, label: str, emoji: str) -> str:
    lines = [
        f"{emoji} *Новое объявление — {label}*",
        "",
        f"📌 *{listing['title']}*",
    ]

    if listing["price"]:
        lines.append(f"💰 {listing['price']}")

    if listing["location"]:
        lines.append(f"📍 {listing['location']}")

    if listing["desc"]:
        lines.append(f"📝 _{listing['desc'][:200]}_")

    if listing["date"]:
        lines.append(f"🕐 {listing['date']}")

    lines += [
        "",
        f"🔗 [Открыть объявление]({listing['url']})",
    ]

    return "\n".join(lines)

# ─── Основной цикл мониторинга ────────────────────────────────────────────────
async def monitor():
    bot  = Bot(token=TELEGRAM_TOKEN)
    seen = load_seen()

    log.info(f"🚀 Мониторинг запущен. Уже в базе: {len(seen)} объявлений")

    # При первом запуске — просто сохраняем все текущие объявления, не шлём
    first_run = len(seen) == 0

    if first_run:
        log.info("Первый запуск — собираю текущие объявления (без уведомлений)...")
        for page in PAGES_TO_MONITOR:
            listings = await fetch_listings(page["url"])
            for l in listings:
                seen.add(l["id"])
        save_seen(seen)
        log.info(f"✅ Первый запуск завершён. Сохранено {len(seen)} объявлений. Жду новых...")

        # Отправляем стартовое сообщение
        await bot.send_message(
            chat_id=MY_CHAT_ID,
            text=f"✅ *RealtyBot запущен!*\n\nСлежу за 999\\.md — пришлю новые объявления о продаже квартир *только от собственников* \\(без агентств\\)\\.\n\nТекущих объявлений в базе: {len(seen)}",
            parse_mode=ParseMode.MARKDOWN_V2
        )

    while True:
        log.info("🔍 Проверяю 999.md...")
        new_count = 0

        for page in PAGES_TO_MONITOR:
            listings = await fetch_listings(page["url"])

            for listing in listings:
                if listing["id"] not in seen:
                    seen.add(listing["id"])
                    new_count += 1

                    # Отправляем уведомление
                    msg = format_message(listing, page["label"], page["emoji"])
                    try:
                        await bot.send_message(
                            chat_id=MY_CHAT_ID,
                            text=msg,
                            parse_mode=ParseMode.MARKDOWN,
                            disable_web_page_preview=False,
                        )
                        log.info(f"✅ Отправлено: {listing['title'][:60]}")
                        await asyncio.sleep(1)  # Пауза между сообщениями
                    except Exception as e:
                        log.error(f"Ошибка отправки: {e}")

        save_seen(seen)

        if new_count > 0:
            log.info(f"📨 Отправлено {new_count} новых объявлений")
        else:
            log.info("Новых объявлений нет")

        log.info(f"⏳ Следующая проверка через {CHECK_INTERVAL // 60} мин...")
        await asyncio.sleep(CHECK_INTERVAL)

# ─── Запуск ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    asyncio.run(monitor())
