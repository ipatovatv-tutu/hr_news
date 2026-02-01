print("ФАЙЛ main.py ЗАПУСТИЛСЯ")

import warnings
warnings.filterwarnings("ignore", category=UserWarning, module="urllib3")

import re
import json
import os
import time
import threading
import feedparser
import requests
from datetime import datetime
from urllib.parse import parse_qs, urlparse, unquote
from openai import OpenAI
import asyncio
from telethon import TelegramClient
import warnings

# =====================
# IGNORE URLLIB3 WARNINGS
# =====================
warnings.filterwarnings("ignore", category=UserWarning, module="urllib3")

# =====================
# TELEGRAM BOT
# =====================
# Для GitHub Actions задай секреты: TELEGRAM_TOKEN, CHAT_ID, OPENAI_API_KEY
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN") or "8519637323:AAHgqQfKuk8Hvw1kmbFe4Ck_stEm4xMC4Zo"
CHAT_ID = os.environ.get("CHAT_ID") or "281610747"

# Расписание: понедельник и четверг в 09:00
DIGEST_WEEKDAYS = (0, 3)  # 0 = Monday, 3 = Thursday
DIGEST_TIME = "09:00"  # локальное время

# Уже отправленные ссылки — чтобы новости не повторялись (файл рядом со скриптом)
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SENT_ARTICLES_FILE = os.path.join(_SCRIPT_DIR, "sent_articles.json")
MAX_SENT_ARTICLES = 500


def load_sent_articles() -> set:
    """Загружает множество уже отправленных URL из JSON."""
    if not os.path.isfile(SENT_ARTICLES_FILE):
        return set()
    try:
        with open(SENT_ARTICLES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        links = data.get("links", [])
        return set(links[-MAX_SENT_ARTICLES:])
    except Exception:
        return set()


def save_sent_articles(links: list):
    """Сохраняет список отправленных URL (храним последние MAX_SENT_ARTICLES)."""
    try:
        with open(SENT_ARTICLES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        existing = data.get("links", [])
    except (FileNotFoundError, json.JSONDecodeError):
        existing = []
    existing = (existing + links)[-MAX_SENT_ARTICLES:]
    with open(SENT_ARTICLES_FILE, "w", encoding="utf-8") as f:
        json.dump({"links": existing, "updated": datetime.now().isoformat()}, f, ensure_ascii=False)
    print("Сохранено отправленных ссылок:", len(existing))
    return


def extract_article_links(text: str) -> list:
    """Извлекает из текста дайджеста ссылки на статьи (vc.ru, career.habr, habr.com)."""
    if not text:
        return []
    pattern = r"https?://(?:vc\.ru/hr/\d[^\s\)\]]*|career\.habr\.com/[^\s\)\]]*|habr\.com/ru/[^\s\)\]]*)"
    return list(dict.fromkeys(re.findall(pattern, text)))


# Лимит Telegram — 4096 символов на сообщение; оставляем запас под HTML
TELEGRAM_MAX_MESSAGE_LENGTH = 4000


def _split_message_for_telegram(text: str, max_len: int = TELEGRAM_MAX_MESSAGE_LENGTH) -> list:
    """Разбивает длинный текст на части по max_len, режет по границам абзацев (\\n\\n)."""
    if not text or len(text) <= max_len:
        return [text] if text else []
    chunks = []
    rest = text
    while rest:
        if len(rest) <= max_len:
            chunks.append(rest)
            break
        part = rest[:max_len]
        # Ищем последний двойной перенос, чтобы не резать посередине новости
        last_para = part.rfind("\n\n")
        if last_para > max_len // 2:
            part = part[:last_para + 2].rstrip()
            rest = rest[len(part):].lstrip()
        else:
            # Режем по одиночному переносу
            last_n = part.rfind("\n")
            if last_n > max_len // 2:
                part = part[:last_n + 1].rstrip()
                rest = rest[len(part):].lstrip()
            else:
                part = part.rstrip()
                rest = rest[len(part):].lstrip()
        chunks.append(part)
    return chunks


def send_to_telegram(text, chat_id=None):
    """Отправляет сообщение в Telegram. Если текст длиннее лимита — шлёт несколькими сообщениями."""
    if not text or not text.strip():
        return
    chat_id = chat_id or CHAT_ID
    for chunk in _split_message_for_telegram(text):
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        try:
            r = requests.post(
                url,
                json={
                    "chat_id": chat_id,
                    "text": chunk,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True
                },
                timeout=30
            )
            if r.status_code != 200 or not r.json().get("ok"):
                print("Telegram ошибка:", r.status_code, r.text[:500])
        except Exception as e:
            print("Telegram отправка ошибка:", e)


def clean_digest_block(text: str) -> str:
    """
    Убирает артефакты из ответа ИИ: обёртки ```html и ```, превращает
    «Источник: label (URL)» в кликабельные ссылки. Заменяет placeholder 123456-slug на ссылку на раздел.
    """
    if not text or not text.strip():
        return text
    s = text.strip()
    # Убираем открывающий ```html или ``` в начале
    s = re.sub(r"^\s*```html?\s*\n?", "", s, flags=re.I)
    # Убираем закрывающий ``` в конце
    s = re.sub(r"\n?\s*```\s*$", "", s)
    s = s.strip()
    # Placeholder от ИИ (123456-slug) ведёт не туда — заменяем на раздел
    s = re.sub(r'href="https://vc\.ru/hr/123456-slug"', 'href="https://vc.ru/hr"', s, flags=re.I)
    # «Источник: label (https://...)» → <a href="URL">Источник: label</a>
    def _link(m):
        label = m.group(1).strip()
        url = m.group(2)
        if "123456-slug" in url:
            url = "https://vc.ru/hr" if "vc.ru" in url else "https://career.habr.com/journal"
        return f'<a href="{url}">Источник: {label}</a>'
    s = re.sub(
        r"Источник: ([^\n]+?) \((https?://[^)\s]+)\)",
        _link,
        s,
    )
    # В готовом тексте заменяем оставшиеся api.vc.ru/redirect на реальный URL
    s = re.sub(r'href="(https?://api\.vc\.ru/[^"]+)"', lambda m: f'href="{_normalize_vc_redirect_link(m.group(1))}"', s)
    return s

# =====================
# OPENAI
# =====================
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY)

def hr_insight_ai(title, summary):
    prompt = f"""
Ты — HR-стратег для крупной tech-компании.

Проанализируй новость и ответь строго в таком формате:

HR-сигнал: да / нет
Категория: рынок труда / компенсации / культура / менеджмент / автоматизация / регуляторика / другое
Комментарий для HR: 1–2 предложения простым языком

Новость:
{title}

Описание:
{summary}
"""
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Ты опытный HR-директор и стратег."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3,
        )
        return response.choices[0].message.content.strip()
    except Exception:
        return "Материал по теме HR и карьеры."

# =====================
# RSS SOURCES
# =====================
SOURCES = [
    "https://www.rbc.ru/rss",
    "https://www.vedomosti.ru/rss",
    "https://www.forbes.ru/newrss",
    "https://www.kommersant.ru/RSS/main.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml",
]

# vc.ru — Playwright собирает ссылки и заголовки, ИИ формирует дайджест за неделю по HR
VC_CHANNEL_URL = "https://vc.ru/hr"
VC_CHANNEL_MAX = 20   # собираем статей с ленты, ИИ выберет до 10 релевантных за неделю
VC_DISCOVERY_QUERY = "HR"
VC_DISCOVERY_MAX = 10
# Если False — все статьи с vc.ru попадают в дайджест без проверки по HR_KEYWORDS (удобно для проверки).
VC_FILTER_BY_HR_KEYWORDS = False

# Хабр Карьера — журнал (карьера, HR, образование в IT)
HABR_JOURNAL_URL = "https://career.habr.com/journal"

# Критерии отбора для HR-дайджеста: что включать и что отсекать (используется в промптах ИИ)
HR_DIGEST_CRITERIA = """
Цель дайджеста — помочь HR-специалисту оставаться в курсе трендов, изменений в профессии и рынке труда, развиваться в коммуникациях, ИИ, постановке целей и управлении людьми.

ВКЛЮЧАЙ:
- HR-тренды, изменения в профессии HR, рынок труда (найм, увольнения, зарплаты, спрос на специалистов)
- Рекрутинг, отбор, интервью, employer brand
- Управление людьми, команды, лидерство, обратная связь, сложные разговоры
- ИИ в HR: автоматизация найма, HR-инструменты, нейросети в рекрутинге и оценке
- Постановка целей (OKR, KPI), оценка эффективности, performance management
- Обучение и развитие сотрудников (L&D), адаптация, онбординг, удержание
- Компенсации, мотивация, бенефиты, культура, вовлечённость
- Исследования по рынку труда, опросы работодателей и сотрудников, регуляторика для HR

НЕ ВКЛЮЧАЙ:
- Карьерные гайды для не-HR ролей: «карьера в маркетинге», «как стать разработчиком», «карьера бэкендера/тестировщика/дизайнера» — если материал не про работу HR-функции
- Общие статьи «как войти в IT» для программистов без привязки к HR
- Узкие советы по карьере в конкретной не-HR специальности (если не про то, как HR с этим работать)
"""

# Лимиты дайджеста (раньше было 7 по RSS — из-за этого новостей почти не было)
MAX_RSS_ITEMS = 15          # макс. новостей из всех RSS-источников
MAX_VC_AI_ITEMS = 10        # макс. статей в дайджесте vc.ru от ИИ
HR_KEYWORDS = [
    "hr", "employee", "job", "layoff", "hiring",
    "сотрудник", "персонал", "увольн", "найм",
    "команда", "мотивац", "культура", "менеджер",
    "рынок труда", "кадры", "recruit"
]
# Если True — в RSS попадают только статьи, где есть хотя бы одно слово из HR_KEYWORDS.
# Если дайджест пустой — поставь False: тогда в дайджест попадут все новости из RSS (до MAX_RSS_ITEMS).
RSS_FILTER_BY_HR_KEYWORDS = False


def collect_rss_news():
    block = ""
    used_titles = set()
    total_seen = 0
    skipped_hr_filter = 0

    for source in SOURCES:
        feed = feedparser.parse(source)
        for entry in feed.entries:
            title = entry.title
            summary = entry.summary if hasattr(entry, "summary") else ""
            link = entry.get("link", "")
            text = (title + " " + summary).lower()
            total_seen += 1

            if RSS_FILTER_BY_HR_KEYWORDS and not any(word in text for word in HR_KEYWORDS):
                skipped_hr_filter += 1
                continue
            if title in used_titles:
                continue

            analysis = hr_insight_ai(title, summary)

            # временно НЕ отфильтровываем "HR-сигнал: нет"
            # if "hr-сигнал: нет" in analysis.lower():
            #     continue

            used_titles.add(title)
            source_line = f'  <a href="{link}">Источник</a>\n' if link else ""
            block += (
                f"• {title}\n"
                f"  <i>{analysis}</i>\n"
                f"{source_line}\n"
            )

            if len(used_titles) >= MAX_RSS_ITEMS:
                print("RSS: всего записей", total_seen, "| отброшено по HR-фильтру:", skipped_hr_filter, "| в дайджест:", len(used_titles))
                return block, used_titles

    print("RSS: всего записей", total_seen, "| отброшено по HR-фильтру:", skipped_hr_filter, "| в дайджест:", len(used_titles))
    return block, used_titles


def _normalize_vc_redirect_link(link: str) -> str:
    """Превращает api.vc.ru/redirect?to=... в реальный URL (декодирует to=)."""
    if not link or "api.vc.ru" not in link or "redirect" not in link:
        return link
    try:
        parsed = urlparse(link)
        qs = parse_qs(parsed.query)
        to_list = qs.get("to") or []
        to = (to_list[0] if to_list else None)
        if to:
            return unquote(to)
    except Exception:
        pass
    return link


def _add_vc_articles_to_block(articles, used_titles, max_items, source_label="vc.ru"):
    """Общая логика: опционально фильтр по HR_KEYWORDS, ИИ, добавление в block. Пропускаем статьи с коротким/мусорным заголовком."""
    block = ""
    added = 0
    for item in articles:
        if added >= max_items:
            break
        title = (item.get("title", "") or "").strip()
        link = _normalize_vc_redirect_link(item.get("link", "") or "")
        snippet = item.get("snippet", title)
        # Пропускаем только явный мусор (очень короткие обрезки), иначе в дайджесте ничего не остаётся
        if not title or len(title) < 10 or title in used_titles:
            continue
        if VC_FILTER_BY_HR_KEYWORDS:
            text = (title + " " + snippet).lower()
            if not any(word in text for word in HR_KEYWORDS):
                continue

        analysis = hr_insight_ai(title, snippet)
        used_titles.add(title)
        source_line = f'  <a href="{link}">Источник: {source_label}</a>\n' if link else ""
        block += (
            f"• {title}\n"
            f"  <i>{analysis}</i>\n"
            f"{source_line}\n"
        )
        added += 1
    return block, used_titles, added


def hr_digest_from_vc_articles(articles: list) -> str:
    """
    ИИ получает список статей с vc.ru (заголовок + ссылка + сниппет).
    Выбирает релевантные для HR за последнюю неделю, пишет короткий комментарий по каждой,
    возвращает готовый HTML-блок для Telegram. Ссылки используем те, что передали — не выдумывает.
    """
    if not articles:
        return ""
    # Строим список для промпта: номер, заголовок, ссылка, сниппет
    lines = []
    for i, a in enumerate(articles[:30], 1):  # не больше 30 в промпт
        title = (a.get("title") or "").strip()
        link = (a.get("link") or "").strip()
        snippet = (a.get("snippet") or title or "")[:500]
        if not title:
            continue
        lines.append(f"{i}. {title}\n   Ссылка: {link}\n   Текст: {snippet}")
    text_list = "\n\n".join(lines)
    if not text_list:
        return ""

    prompt = f"""Ниже список статей с vc.ru (раздел HR/Карьера). Лента показывает свежие первыми — считай, что это материалы за последнюю неделю.

Критерии отбора:
{HR_DIGEST_CRITERIA}

Выбери до 10 статей, которые соответствуют критериям (HR-тренды, рынок труда, рекрутинг, управление людьми, ИИ в HR, коммуникации, цели, L&D и т.д.). Не включай статьи про «карьера в маркетинге/разработке» для не-HR специалистов.
По каждой выбранной статье напиши короткий комментарий для HR (1–2 предложения).
Важно: используй в ответе только те ссылки, что указаны в списке (поле «Ссылка:»), не придумывай URL.

В каждую ссылку <a href="..."> вставляй только URL из поля «Ссылка:» ниже — копируй его буквально. Запрещено подставлять примеры вроде 123456-slug.

Формат вывода — готовый HTML для Telegram, только этот блок (сырой HTML, без обёртки в ```html или ```):
• 📰 <b>Заголовок</b>
  Комментарий для HR: ...
  <a href="URL_ИЗ_ПОЛЯ_ССЫЛКА_ВЫШЕ">Источник: vc.ru</a>

Список статей:
---
{text_list}
---"""

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Ты HR-директор. Отбираешь статьи для дайджеста. В <a href=\"...\"> вставляешь только URL из поля «Ссылка:» списка — копируешь буквально, никогда не используешь примеры типа 123456-slug. Формат — HTML для Telegram."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
        )
        block = (response.choices[0].message.content or "").strip()
        if block and ("• " in block or "Источник" in block or "vc.ru" in block):
            return block + "\n\n"
        return ""
    except Exception as e:
        print(f"vc.ru ИИ-дайджест из списка статей ошибка: {e}")
        return ""


def _fetch_headers():
    return {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    }


def get_page_html(url: str, timeout: int = 15) -> str:
    """Загружает страницу и возвращает сырой HTML (для парсинга пар заголовок–ссылка)."""
    try:
        r = requests.get(url, headers=_fetch_headers(), timeout=timeout)
        r.raise_for_status()
        html = r.text
        return html if len(html) >= 500 else ""
    except Exception as e:
        print(f"get_page_html {url[:50]}... ошибка: {e}")
        return ""


def parse_vc_articles_from_html(html: str, max_articles: int = 25) -> list:
    """Извлекает из HTML vc.ru/hr пары (заголовок, ссылка), чтобы заголовки и ссылки не путались."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return []
    articles = []
    seen = set()
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if href.startswith("/"):
            href = "https://vc.ru" + href
        if not re.match(r"https?://vc\.ru/hr/\d", href):
            continue
        if href in seen:
            continue
        title = (a.get_text(strip=True) or "").strip()
        if not title or len(title) < 10 or len(title) > 400:
            continue
        seen.add(href)
        articles.append({"title": title[:200], "link": href, "snippet": title[:300]})
        if len(articles) >= max_articles:
            break
    return articles


def _is_habr_article_url(href: str) -> bool:
    """Проверяет, что ссылка ведёт на статью, а не на страницу компании (habr.com/ru/company/habr_career)."""
    h = href.split("?")[0].rstrip("/")
    # Исключаем страницу компании — туда ведут общие ссылки «Хабр Карьера»
    if "habr.com/ru/company/habr_career" in h and (h.endswith("habr_career") or h.endswith("habr_career/")):
        return False
    if "habr.com/ru/company/habr_career" in h and not re.search(r"/articles/\d+", h) and not re.search(r"habr_career/\d+", h):
        return False
    # Статья: habr.com/ru/articles/123456 или .../company/.../articles/123456 или career.habr.com/.../id
    if re.search(r"/articles/\d+", h):
        return True
    if re.search(r"/p/\d+", h):
        return True
    if re.search(r"habr_career/\d+", h):
        return True
    if "career.habr.com" in h and re.search(r"/[a-z]+/\d+", h):
        return True
    return False


def parse_habr_articles_from_html(html: str, max_articles: int = 25) -> list:
    """Извлекает из HTML career.habr.com/journal пары (заголовок, ссылка). Только ссылки на статьи, не на страницу компании."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return []
    articles = []
    seen = set()
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href or href in seen:
            continue
        if href.startswith("/"):
            href = "https://career.habr.com" + href
        if "career.habr.com" not in href and "habr.com" not in href:
            continue
        if not _is_habr_article_url(href):
            continue
        if href.rstrip("/").endswith("journal"):
            continue
        if len(href) < 30:
            continue
        title = (a.get_text(strip=True) or "").strip()
        if not title or len(title) < 10 or len(title) > 400:
            continue
        seen.add(href)
        articles.append({"title": title[:200], "link": href, "snippet": title[:300]})
        if len(articles) >= max_articles:
            break
    return articles


def fetch_page_via_requests(url: str, timeout: int = 15) -> str:
    """
    Загрузка страницы через requests (без Playwright).
    Возвращает текст страницы: ссылки из <a href="..."> вставлены в текст, теги убраны.
    """
    try:
        r = requests.get(url, headers=_fetch_headers(), timeout=timeout)
        r.raise_for_status()
        html = r.text
        if len(html) < 500:
            return ""
        text = re.sub(r'<a\s+href="(https?://[^"]+)"[^>]*>', r" \1 ", html, flags=re.I)
        text = re.sub(r"<script[^>]*>[\s\S]*?</script>", " ", text, flags=re.I)
        text = re.sub(r"<style[^>]*>[\s\S]*?</style>", " ", text, flags=re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:50000] if text else html[:50000]
    except Exception as e:
        print(f"fetch {url[:50]}... ошибка: {e}")
        return ""


def hr_digest_from_page_text(page_text: str, exclude_urls: set = None) -> str:
    """
    Запасной путь: когда Playwright не нашёл статей по ссылкам, передаём ИИ сырой текст
    страницы vc.ru/hr. ИИ извлекает заголовки статей и ссылки из текста и формирует дайджест.
    exclude_urls — уже отправленные ссылки, не включать в дайджест.
    """
    if not page_text or len(page_text.strip()) < 300:
        return ""
    exclude_hint = ""
    if exclude_urls and len(exclude_urls) > 0:
        sample = list(exclude_urls)[:15]
        exclude_hint = f"\nНе включай статьи с этими URL (уже были в прошлом дайджесте): {', '.join(sample)}\n"
    prompt = f"""Ниже текст ленты с vc.ru, раздел «Карьера» (HR). В тексте уже есть полные URL статей (например https://vc.ru/hr/2713395-название-статьи).
{exclude_hint}
Критерии отбора:
{HR_DIGEST_CRITERIA}

Извлеки до 10 статей за последнюю неделю (свежие в начале), которые соответствуют критериям. Не включай статьи про карьеру в маркетинге/разработке для не-HR специалистов.
Для каждой выбранной: заголовок, короткий HR-комментарий (1–2 предложения), и обязательно скопируй в <a href="..."> полный URL этой статьи из текста ниже.

Важно: в href вставляй только реальные ссылки из текста ленты — скопируй их буквально. Запрещено придумывать или подставлять примеры вроде 123456-slug.

Формат вывода — готовый HTML для Telegram, только этот блок (сырой HTML, без обёртки в ```html или ```):
• Заголовок
  Комментарий для HR: ...
  <a href="ПОЛНЫЙ_URL_ИЗ_ТЕКСТА_НИЖЕ">Источник: vc.ru</a>

Текст ленты:
---
{page_text[:35000]}
---"""

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Ты HR-директор. Отбираешь статьи для дайджеста. В каждую ссылку <a href=\"...\"> вставляешь только реальный URL из текста — копируешь его из ленты, никогда не подставляешь примеры типа 123456-slug. Формат — HTML для Telegram."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
        )
        block = (response.choices[0].message.content or "").strip()
        # Принимаем любой осмысленный блок (ИИ может вернуть • или - или 1.)
        if block and len(block) > 80 and ("vc.ru" in block or "Источник" in block or "• " in block or "http" in block):
            return block + "\n\n"
        if block and len(block) > 200:
            return block + "\n\n"
        return ""
    except Exception as e:
        print(f"vc.ru ИИ-дайджест из текста страницы ошибка: {e}")
        return ""


def hr_digest_from_habr_text(page_text: str, exclude_urls: set = None) -> str:
    """
    ИИ извлекает из текста страницы Хабр Карьера (career.habr.com/journal) заголовки статей,
    ссылки и формирует дайджест для HR. exclude_urls — уже отправленные ссылки.
    """
    if not page_text or len(page_text.strip()) < 300:
        return ""
    exclude_hint = ""
    if exclude_urls and len(exclude_urls) > 0:
        sample = list(exclude_urls)[:15]
        exclude_hint = f"\nНе включай статьи с этими URL (уже были в прошлом дайджесте): {', '.join(sample)}\n"
    prompt = f"""Ниже текст ленты с сайта Хабр Карьера (career.habr.com/journal). В тексте уже есть полные URL статей.
{exclude_hint}
Критерии отбора:
{HR_DIGEST_CRITERIA}

Извлеки до 8 статей за последнюю неделю (свежие в начале), которые соответствуют критериям. Не включай статьи про «карьера в маркетинге», «как стать разработчиком» и т.п. — только то, что полезно HR-специалисту.
Для каждой выбранной: заголовок, короткий HR-комментарий (1–2 предложения), и скопируй в <a href="..."> полный URL этой статьи из текста ниже.

Важно: в href вставляй только реальные ссылки из текста — копируй их буквально. Запрещено придумывать URL.

Формат вывода — готовый HTML для Telegram, только этот блок (сырой HTML, без обёртки в ```html или ```):
• Заголовок
  Комментарий для HR: ...
  <a href="ПОЛНЫЙ_URL_ИЗ_ТЕКСТА_НИЖЕ">Источник: Хабр Карьера</a>

Текст ленты:
---
{page_text[:35000]}
---"""

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Ты HR-директор. Отбираешь статьи для дайджеста. В каждую ссылку <a href=\"...\"> вставляешь только реальный URL из текста ленты — копируешь его, никогда не придумываешь. Формат — HTML для Telegram."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
        )
        block = (response.choices[0].message.content or "").strip()
        if block and len(block) > 80 and ("career.habr" in block or "Источник" in block or "• " in block or "http" in block):
            return block + "\n\n"
        if block and len(block) > 200:
            return block + "\n\n"
        return ""
    except Exception as e:
        print(f"Хабр Карьера ИИ-дайджест ошибка: {e}")
        return ""


def hr_digest_from_habr_articles(articles: list) -> str:
    """
    ИИ получает список статей Хабр Карьера (заголовок + ссылка уже сопоставлены).
    Выбирает релевантные для HR, пишет комментарий по каждой. Ссылки не путаются с заголовками.
    """
    if not articles:
        return ""
    lines = []
    for i, a in enumerate(articles[:25], 1):
        title = (a.get("title") or "").strip()
        link = (a.get("link") or "").strip()
        snippet = (a.get("snippet") or title or "")[:500]
        if not title or not link:
            continue
        lines.append(f"{i}. {title}\n   Ссылка: {link}\n   Текст: {snippet}")
    text_list = "\n\n".join(lines)
    if not text_list:
        return ""

    prompt = f"""Ниже список статей с Хабр Карьера (career.habr.com/journal). У каждой статьи заголовок и ссылка уже сопоставлены.

Критерии отбора:
{HR_DIGEST_CRITERIA}

Выбери до 8 статей, релевантных для HR (тренды, рынок труда, рекрутинг, ИИ в HR, коммуникации, цели, L&D). Не включай карьерные гайды для не-HR ролей.
По каждой выбранной напиши короткий HR-комментарий (1–2 предложения).
В каждую ссылку <a href="..."> вставляй только URL из поля «Ссылка:» — копируй буквально, заголовок и ссылка должны соответствовать одной и той же строке списка.

Формат вывода — готовый HTML для Telegram (сырой HTML, без ```html):
• Заголовок (точно из списка)
  Комментарий для HR: ...
  <a href="URL_ИЗ_ПОЛЯ_ССЫЛКА_ЭТОЙ_СТРОКИ">Источник: Хабр Карьера</a>

Список статей:
---
{text_list}
---"""

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Ты HR-директор. Для каждой статьи из списка используй ровно ту ссылку, что указана в поле «Ссылка:» этой же строки. Заголовок и ссылка должны быть из одной строки списка. Формат — HTML для Telegram."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
        )
        block = (response.choices[0].message.content or "").strip()
        if block and ("• " in block or "Источник" in block or "habr" in block):
            return block + "\n\n"
        return ""
    except Exception as e:
        print(f"Хабр Карьера ИИ-дайджест из списка ошибка: {e}")
        return ""


def collect_habr_news(used_titles, sent_articles=None):
    """
    Новости с Хабр Карьера: парсим HTML → пары (заголовок, ссылка) → ИИ добавляет комментарии.
    Так заголовки и ссылки не путаются.
    """
    sent_articles = sent_articles or set()
    print("Хабр Карьера: загрузка HTML...")
    html = get_page_html(HABR_JOURNAL_URL)
    if not html:
        return "", used_titles
    articles = parse_habr_articles_from_html(html)
    articles = [a for a in articles if a.get("link") not in sent_articles]
    print("Хабр Карьера: извлечено статей (заголовок+ссылка):", len(articles))
    if articles:
        print("Хабр Карьера: ИИ формирует дайджест из списка...")
        block = hr_digest_from_habr_articles(articles)
        if block:
            print("Хабр Карьера: дайджест готов")
            return block, used_titles
    print("Хабр Карьера: парсинг не дал статей, пробуем по тексту...")
    page_text = fetch_page_via_requests(HABR_JOURNAL_URL)
    if page_text and len(page_text.strip()) >= 500:
        block = hr_digest_from_habr_text(page_text, exclude_urls=sent_articles)
        if block:
            return block, used_titles
    return "", used_titles


def collect_vc_news(used_titles, sent_articles=None):
    """
    Новости с vc.ru: сначала пробуем requests (без Playwright), потом Playwright.
    ИИ формирует дайджест за неделю по HR-тематике. sent_articles — уже отправленные URL, не повторять.
    """
    sent_articles = sent_articles or set()
    # 1) Парсим HTML → пары (заголовок, ссылка), чтобы заголовки и ссылки не путались
    print("vc.ru: загрузка HTML...")
    html = get_page_html(VC_CHANNEL_URL)
    if html:
        articles = parse_vc_articles_from_html(html)
        articles = [a for a in articles if a.get("link") not in sent_articles]
        print("vc.ru: извлечено статей (заголовок+ссылка):", len(articles))
        if articles:
            print("vc.ru: ИИ формирует дайджест из списка...")
            block = hr_digest_from_vc_articles(articles)
            if block:
                print("vc.ru: дайджест готов (парсинг HTML)")
                return block, used_titles
    print("vc.ru: парсинг не дал статей, пробуем по тексту...")
    page_text = fetch_page_via_requests(VC_CHANNEL_URL)
    if page_text and len(page_text.strip()) >= 500:
        block = hr_digest_from_page_text(page_text, exclude_urls=sent_articles)
        if block:
            print("vc.ru: дайджест готов (текст)")
            return block, used_titles
    print("vc.ru: пробуем Playwright...")

    try:
        from vc_discovery import collect_vc_channel, collect_vc_discovery, get_vc_page_text
    except ImportError:
        print("vc.ru: модуль vc_discovery не найден (Playwright не обязателен)")
        return "", used_titles

    print("vc.ru: загрузка ленты vc.ru/hr через Playwright...")
    articles_channel = collect_vc_channel(
        channel_url=VC_CHANNEL_URL,
        max_articles=VC_CHANNEL_MAX,
    )
    print("vc.ru: загрузка discovery по HR...")
    articles_discovery = collect_vc_discovery(
        query=VC_DISCOVERY_QUERY,
        max_articles=VC_DISCOVERY_MAX,
    )
    # Нормализуем ссылки api.vc.ru/redirect?to=... в реальные URL
    def _norm(a_list):
        return [{"title": x.get("title", ""), "link": _normalize_vc_redirect_link((x.get("link") or "").strip()), "snippet": x.get("snippet", x.get("title", ""))} for x in a_list]
    articles_channel = _norm(articles_channel)
    articles_discovery = _norm(articles_discovery)
    # Объединяем, убираем дубли по ссылке (channel первыми — там свежее)
    seen_links = set()
    merged = []
    for a in articles_channel + articles_discovery:
        link = (a.get("link") or "").strip()
        if link and link not in seen_links and (not sent_articles or link not in sent_articles):
            seen_links.add(link)
            merged.append(a)
    print("vc.ru: всего статей для ИИ (без уже отправленных):", len(merged))

    block = ""
    if merged:
        print("vc.ru: ИИ формирует дайджест за неделю по HR...")
        block = hr_digest_from_vc_articles(merged)
        if block:
            print("vc.ru: дайджест готов, символов:", len(block))
            return block, used_titles
        print("vc.ru: ИИ вернул пустой блок, пробуем дайджест из текста страницы (Playwright)...")

    # Запасной путь: дайджест из текста страницы (когда списка статей нет или ИИ вернул пусто)
    page_text = None
    if not merged or not block:
        print("vc.ru: загружаем текст страницы для ИИ...")
        page_text = get_vc_page_text(url=VC_CHANNEL_URL, scroll_times=4)
        print("vc.ru: получено символов:", len(page_text or ""))
    if page_text and len(page_text.strip()) >= 300:
        block = hr_digest_from_page_text(page_text, exclude_urls=sent_articles)
        if block:
            print("vc.ru: дайджест из текста страницы готов")
            return block, used_titles

    # Последний вариант: по каждой статье вызываем ИИ (только с нормальными заголовками)
    block = ""
    b1, used_titles, n1 = _add_vc_articles_to_block(
        articles_channel, used_titles, VC_CHANNEL_MAX, source_label="vc.ru/hr"
    )
    block += b1
    b2, used_titles, n2 = _add_vc_articles_to_block(
        articles_discovery, used_titles, VC_DISCOVERY_MAX, source_label="vc.ru"
    )
    block += b2
    if n1 or n2:
        print("vc.ru: в дайджест добавлено по одной статье:", n1 + n2)
    return block, used_titles


async def collect_telegram_news(used_titles):
    """Сбор новостей из Telegram. Пока заглушка — каналы не подключены."""
    print("Telegram пока не подключён")
    return ""


def run_digest(sent_articles=None):
    """Собирает дайджест и возвращает текст сообщения. sent_articles — множество уже отправленных URL."""
    sent_articles = sent_articles or set()
    today = datetime.now().strftime("%d %B %Y")
    used_titles = set()

    print("Сбор новостей с vc.ru (Карьера + discovery)...")
    vc_block, used_titles = collect_vc_news(used_titles, sent_articles)
    vc_block = clean_digest_block(vc_block)

    print("Сбор новостей с Хабр Карьера (journal)...")
    habr_block, used_titles = collect_habr_news(used_titles, sent_articles)
    habr_block = clean_digest_block(habr_block)

    print("Сбор Telegram новостей...")
    tg_block = asyncio.run(collect_telegram_news(used_titles))

    digest_blocks = [b.strip() for b in (vc_block, habr_block, tg_block) if b and b.strip()]
    body = "\n\n".join(digest_blocks)
    result_text = (
        f"📬 <b>HR-дайджест · {today}</b>\n\n"
        f"🧠 <b>Ключевые сигналы недели</b>\n\n"
        f"{body}"
    )
    if not (vc_block.strip() or habr_block.strip() or tg_block.strip()):
        result_text += "За эту неделю новостей не найдено. Проверь vc.ru, Хабр Карьера и источники.\n"
    return result_text


def send_digest(chat_id=None):
    """Собирает дайджест, отправляет в Telegram и сохраняет ссылки, чтобы новости не повторялись."""
    try:
        sent_articles = load_sent_articles()
        print("Уже отправлено ссылок:", len(sent_articles))
        result_text = run_digest(sent_articles)
        send_to_telegram(result_text, chat_id=chat_id)
        new_links = extract_article_links(result_text)
        if new_links:
            save_sent_articles(new_links)
        print("ГОТОВО ✅")
    except Exception as e:
        print("Ошибка при отправке дайджеста:", e)
        import traceback
        traceback.print_exc()


def bot_polling_loop():
    """Слушает команды в Telegram: /digest или /дайджест — принудительно отправить дайджест."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    offset = None
    while True:
        try:
            params = {"timeout": 30}
            if offset is not None:
                params["offset"] = offset
            r = requests.get(url, params=params, timeout=35)
            data = r.json()
            if not data.get("ok"):
                time.sleep(5)
                continue
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("edited_message")
                if not msg:
                    continue
                text = (msg.get("text") or "").strip()
                chat_id = str(msg.get("chat", {}).get("id"))
                if text.lower().startswith("/digest") or text.strip().lower() == "/дайджест":
                    print("Команда /digest от chat_id:", chat_id)
                    send_to_telegram("Собираю дайджест…", chat_id=chat_id)
                    send_digest(chat_id=chat_id)
                    send_to_telegram("Дайджест отправлен.", chat_id=chat_id)
        except Exception as e:
            print("Bot polling ошибка:", e)
            time.sleep(10)


def scheduler_loop():
    """Запускает дайджест по понедельникам и четвергам в DIGEST_TIME."""
    try:
        import schedule
    except ImportError:
        print("Установите schedule: pip install schedule")
        return
    schedule.every().monday.at(DIGEST_TIME).do(send_digest)
    schedule.every().thursday.at(DIGEST_TIME).do(send_digest)
    print("Расписание: понедельник и четверг в", DIGEST_TIME)
    while True:
        schedule.run_pending()
        time.sleep(60)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--once":
        # Один раз отправить дайджест и выйти (удобно для ручного запуска или cron)
        print("Режим: один запуск дайджеста")
        send_digest()
        sys.exit(0)

    # Режим по умолчанию: бот слушает команды + расписание понедельник/четверг
    print("Запуск: бот (команда /digest) + расписание пн/чт", DIGEST_TIME)
    thread_bot = threading.Thread(target=bot_polling_loop, daemon=True)
    thread_bot.start()
    scheduler_loop()
