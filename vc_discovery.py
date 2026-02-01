"""
Забор новостей с vc.ru:
- discovery (поиск: q=HR)
- раздел/сообщество (лента): например https://vc.ru/hr (Карьера)
Страницы подгружают контент через JavaScript — нужен Playwright.
"""

import re
from typing import List, Dict

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

# Ссылки на разделы сайта (не статьи) — исключаем
VC_SKIP_PATHS = (
    "/discovery",
    "/tag/",
    "/login",
    "/signup",
    "/search",
    "/ads",
    "/api/",
)
# /go/ — редирект; но vc.ru/hr/123-slug это статьи (цифры в path)
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def _is_article_url(href: str) -> bool:
    """Проверяет, похожа ли ссылка на статью vc.ru (vc.ru/hr/12345-slug и т.п.)."""
    if not href or "vc.ru" not in href:
        return False
    # Исключаем общие разделы (но не /hr/123-slug)
    for skip in VC_SKIP_PATHS:
        if skip in href:
            return False
    if "/go/" in href and not re.search(r"/go/\d+", href):
        return False
    path = href.split("vc.ru")[-1].split("?")[0].strip("/")
    parts = path.split("/")
    if not parts:
        return False
    # Статьи: vc.ru/123456 или vc.ru/hr/123456-slug — есть цифры в path
    return bool(re.search(r"\d+", path))


def collect_vc_discovery(
    query: str = "HR",
    max_articles: int = 15,
    timeout_ms: int = 25000,
) -> List[Dict[str, str]]:
    """
    Забирает список статей с https://vc.ru/discovery?q=<query>.

    Возвращает список словарей: [{"title": "...", "link": "...", "snippet": "..."}, ...]
    """
    if not HAS_PLAYWRIGHT:
        print("vc.ru discovery: установите playwright (pip install playwright && playwright install chromium)")
        return []

    url = f"https://vc.ru/discovery?q={query}"
    articles: List[Dict[str, str]] = []
    seen_urls: set = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_default_timeout(timeout_ms)
            page.set_extra_http_headers({"User-Agent": USER_AGENT})
            page.goto(url, wait_until="load")
            page.wait_for_selector('a[href*="vc.ru/"]', timeout=timeout_ms)
            page.wait_for_timeout(2000)
            for _ in range(2):
                page.evaluate("window.scrollBy(0, window.innerHeight)")
                page.wait_for_timeout(800)
            page.wait_for_timeout(500)

            links = page.query_selector_all('a[href*="vc.ru/"]')
            for a in links:
                if len(articles) >= max_articles:
                    break
                href = a.get_attribute("href") or ""
                if not href.startswith("http"):
                    href = "https://vc.ru" + href if href.startswith("/") else "https://vc.ru/" + href
                if href in seen_urls or not _is_article_url(href):
                    continue
                title = (a.inner_text() or "").strip()
                if not title or len(title) < 5:
                    continue
                # Короткий сниппет можно взять из родителя (карточка)
                snippet = ""
                try:
                    parent = a.evaluate_handle("el => el.closest('article') || el.closest('[class*=\"feed\"]') || el.closest('[class*=\"card\"]') || el.parentElement")
                    if parent:
                        el = parent.as_element()
                        if el:
                            snippet = (el.inner_text() or "")[:300].strip()
                except Exception:
                    pass

                seen_urls.add(href)
                articles.append({
                    "title": title[:200],
                    "link": href,
                    "snippet": snippet[:300] if snippet else title,
                })
        except Exception as e:
            print(f"vc.ru discovery ошибка: {e}")
        finally:
            browser.close()

    print(f"vc.ru discovery: статей отобрано {len(articles)}")
    return articles


def collect_vc_channel(
    channel_url: str = "https://vc.ru/hr",
    max_articles: int = 15,
    timeout_ms: int = 25000,
) -> List[Dict[str, str]]:
    """
    Забирает список статей из ленты раздела/сообщества vc.ru.
    Например: https://vc.ru/hr (Карьера — всё про HR, персонал, карьеру).

    Возвращает список словарей: [{"title": "...", "link": "...", "snippet": "..."}, ...]
    """
    if not HAS_PLAYWRIGHT:
        print("vc.ru: установите playwright (pip install playwright && playwright install chromium)")
        return []

    articles: List[Dict[str, str]] = []
    seen_urls: set = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_default_timeout(timeout_ms)
            page.set_extra_http_headers({"User-Agent": USER_AGENT})
            page.goto(channel_url, wait_until="load")
            page.wait_for_selector('a[href*="vc.ru/"]', timeout=timeout_ms)
            page.wait_for_timeout(2000)
            for _ in range(3):
                page.evaluate("window.scrollBy(0, window.innerHeight)")
                page.wait_for_timeout(800)
            page.wait_for_timeout(500)

            links = page.query_selector_all('a[href*="vc.ru/"]')
            for a in links:
                if len(articles) >= max_articles:
                    break
                href = a.get_attribute("href") or ""
                if not href.startswith("http"):
                    href = "https://vc.ru" + href if href.startswith("/") else "https://vc.ru/" + href
                if href in seen_urls or not _is_article_url(href):
                    continue
                # На странице канала ссылка на сам канал (vc.ru/hr) — не статья
                if href.rstrip("/") == channel_url.rstrip("/"):
                    continue
                title = (a.inner_text() or "").strip()
                if not title or len(title) < 5:
                    continue
                snippet = ""
                try:
                    parent = a.evaluate_handle(
                        'el => el.closest("article") || el.closest("[class*=\'feed\']") || el.closest("[class*=\'card\']") || el.parentElement'
                    )
                    if parent:
                        el = parent.as_element()
                        if el:
                            snippet = (el.inner_text() or "")[:300].strip()
                except Exception:
                    pass

                seen_urls.add(href)
                articles.append({
                    "title": title[:200],
                    "link": href,
                    "snippet": snippet[:300] if snippet else title,
                })
        except Exception as e:
            print(f"vc.ru channel ошибка ({channel_url}): {e}")
        finally:
            browser.close()

    print(f"vc.ru channel: статей отобрано {len(articles)}")
    return articles


def get_vc_page_text(
    url: str = "https://vc.ru/hr",
    timeout_ms: int = 25000,
    scroll_times: int = 4,
) -> str:
    """
    Открывает страницу vc.ru в Playwright, скроллит, возвращает весь видимый текст (для ИИ).
    """
    if not HAS_PLAYWRIGHT:
        return ""

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_default_timeout(timeout_ms)
            page.set_extra_http_headers({"User-Agent": USER_AGENT})
            page.goto(url, wait_until="load")
            page.wait_for_timeout(3000)
            for _ in range(scroll_times):
                page.evaluate("window.scrollBy(0, window.innerHeight)")
                page.wait_for_timeout(800)
            text = page.evaluate("""() => document.body ? document.body.innerText : ''""")
            return (text or "")[:50000]
        except Exception as e:
            print(f"vc.ru get_vc_page_text ошибка: {e}")
            return ""
        finally:
            browser.close()
