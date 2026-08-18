#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import html
import json
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup
from ebooklib import epub
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.json"
STATE_DIR = ROOT / "state"
SITE_DIR = ROOT / "site"
BOOKS_DIR = SITE_DIR / "books"
PENDING_PATH = STATE_DIR / "pending.json"
PUBLISHED_PATH = STATE_DIR / "published.json"

BASE = "https://www.infzm.com"


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=1.2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 11) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": BASE + "/",
        }
    )
    return session


def get_html(session: requests.Session, url: str, delay: float = 0.0) -> str:
    if delay:
        time.sleep(delay)
    r = session.get(url, timeout=(10, 25))
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    return r.text


def normalize_content_url(href: str) -> str | None:
    if not href:
        return None
    full = urljoin(BASE, href)
    parsed = urlparse(full)
    if parsed.netloc not in {"www.infzm.com", "infzm.com"}:
        return None
    m = re.search(r"/contents/(\d+)", parsed.path)
    if not m:
        return None
    return f"{BASE}/contents/{m.group(1)}"


def content_id(url: str) -> str:
    m = re.search(r"/contents/(\d+)", url)
    return m.group(1) if m else hashlib.sha1(url.encode()).hexdigest()[:12]


def collect_candidates(session: requests.Session, cfg: dict) -> list[dict]:
    found: dict[str, dict] = {}
    delay = float(cfg.get("request_delay_seconds", 1.0))
    per_topic = int(cfg.get("max_candidates_per_topic", 20))
    total_cap = int(cfg.get("max_total_candidates", 80))

    for topic in cfg["topics"]:
        print(f"[topic] {topic['name']}: {topic['url']}")
        raw = get_html(session, topic["url"], delay=delay)
        soup = BeautifulSoup(raw, "html.parser")
        count = 0
        for a in soup.find_all("a", href=True):
            url = normalize_content_url(a.get("href"))
            if not url:
                continue
            cid = content_id(url)
            card_text = " ".join(a.stripped_strings)
            if cid not in found:
                found[cid] = {
                    "id": cid,
                    "url": url,
                    "topic": topic["name"],
                    "card_text": card_text,
                }
                count += 1
            if count >= per_topic or len(found) >= total_cap:
                break
        print(f"  candidates: {count}")
        if len(found) >= total_cap:
            break

    return list(found.values())


def infer_year_for_mmdd(mm: int, dd: int, now_local: datetime) -> date:
    candidate = date(now_local.year, mm, dd)
    # Around New Year: a December item seen in January is probably previous year.
    if candidate > now_local.date() + timedelta(days=45):
        candidate = date(now_local.year - 1, mm, dd)
    return candidate


def parse_date_from_jsonld(soup: BeautifulSoup) -> date | None:
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        items = obj if isinstance(obj, list) else [obj]
        for item in items:
            if not isinstance(item, dict):
                continue
            val = item.get("datePublished") or item.get("dateCreated")
            if not val:
                continue
            try:
                return datetime.fromisoformat(str(val).replace("Z", "+00:00")).date()
            except Exception:
                pass
    return None


def meta_content(soup: BeautifulSoup, *, name=None, prop=None) -> str:
    tag = None
    if name:
        tag = soup.find("meta", attrs={"name": name})
    elif prop:
        tag = soup.find("meta", attrs={"property": prop})
    if not tag:
        return ""
    return (tag.get("content") or "").strip()


def parse_article(session: requests.Session, item: dict, cfg: dict, now_local: datetime) -> dict:
    raw = get_html(session, item["url"], delay=float(cfg.get("request_delay_seconds", 1.0)))
    soup = BeautifulSoup(raw, "html.parser")
    text = " ".join(soup.stripped_strings)

    h1 = soup.find("h1")
    title = h1.get_text(" ", strip=True) if h1 else ""
    if not title:
        title = meta_content(soup, prop="og:title") or meta_content(soup, name="title")
    if not title:
        raise ValueError(f"No title: {item['url']}")

    pub_date = parse_date_from_jsonld(soup)

    if pub_date is None:
        for prop in ("article:published_time", "og:published_time"):
            val = meta_content(soup, prop=prop)
            if val:
                try:
                    pub_date = datetime.fromisoformat(val.replace("Z", "+00:00")).date()
                    break
                except Exception:
                    pass

    if pub_date is None:
        m = re.search(r"\b(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b", text)
        if m:
            pub_date = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))

    if pub_date is None:
        # South Weekend article pages commonly display "原创 08-17".
        m = re.search(r"(?:原创\s*)?(\d{1,2})-(\d{1,2})(?:\s|$)", text)
        if m:
            pub_date = infer_year_for_mmdd(int(m.group(1)), int(m.group(2)), now_local)

    description = (
        meta_content(soup, prop="og:description")
        or meta_content(soup, name="description")
    )

    if not description:
        # Public lead/summary lines are often rendered as blockquotes.
        quotes = [q.get_text(" ", strip=True) for q in soup.find_all("blockquote")]
        description = " ".join(x for x in quotes if x)

    description = re.sub(r"\s+", " ", description).strip()
    max_chars = int(cfg.get("excerpt_max_chars", 360))
    if len(description) > max_chars:
        description = description[:max_chars].rstrip() + "…"

    author = meta_content(soup, name="author")
    member_more = "登录后获取更多权限" in text or "立即登录" in text

    return {
        **item,
        "title": title,
        "date": pub_date.isoformat() if pub_date else None,
        "description": description,
        "author": author,
        "member_more": member_more,
    }


def safe_text(s: str) -> str:
    return html.escape(s or "", quote=True)


def make_epub(target_date: date, articles: list[dict], out_path: Path):
    book = epub.EpubBook()
    book.set_identifier(f"infzm-public-digest-{target_date.isoformat()}")
    book.set_title(f"南方周末公开内容摘要 · {target_date.isoformat()}")
    book.set_language("zh-CN")
    book.add_author("个人 OPDS 阅读器")

    intro = epub.EpubHtml(
        title="说明",
        file_name="intro.xhtml",
        lang="zh-CN",
    )
    intro.content = f"""
    <html xmlns="http://www.w3.org/1999/xhtml">
    <head><title>说明</title></head>
    <body>
      <h1>南方周末公开内容摘要 · {target_date.isoformat()}</h1>
      <p>本电子书用于个人阅读整理，仅收录南方周末网页公开显示的标题、
      简短摘要和原文链接，不尝试绕过登录、会员或其他访问限制。</p>
      <p>共 {len(articles)} 篇。完整内容请通过每篇文章中的“打开原文”链接访问南方周末网站。</p>
    </body>
    </html>
    """
    book.add_item(intro)

    chapters = [intro]
    toc = [intro]

    for i, a in enumerate(articles, 1):
        chap = epub.EpubHtml(
            title=a["title"],
            file_name=f"article_{i:03d}.xhtml",
            lang="zh-CN",
        )
        desc = safe_text(a.get("description") or "网页未提供公开摘要。")
        topic = safe_text(a.get("topic") or "")
        title = safe_text(a["title"])
        url = safe_text(a["url"])
        member_note = (
            "<p><strong>提示：</strong>网页显示登录后可获取更多内容。</p>"
            if a.get("member_more")
            else ""
        )
        chap.content = f"""
        <html xmlns="http://www.w3.org/1999/xhtml">
        <head><title>{title}</title></head>
        <body>
          <h1>{title}</h1>
          <p><strong>栏目：</strong>{topic}</p>
          <p>{desc}</p>
          {member_note}
          <p><a href="{url}">打开南方周末原文</a></p>
        </body>
        </html>
        """
        book.add_item(chap)
        chapters.append(chap)
        toc.append(chap)

    book.toc = tuple(toc)
    book.spine = ["nav"] + chapters
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    style = """
    body { font-family: serif; line-height: 1.65; margin: 5%; }
    h1 { line-height: 1.35; }
    p { margin: 0.8em 0; }
    a { text-decoration: underline; }
    """
    css = epub.EpubItem(
        uid="style",
        file_name="style/main.css",
        media_type="text/css",
        content=style,
    )
    book.add_item(css)
    for c in chapters:
        c.add_item(css)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    epub.write_epub(str(out_path), book, {})


def generate_opds(published: list[dict]):
    SITE_DIR.mkdir(parents=True, exist_ok=True)
    BOOKS_DIR.mkdir(parents=True, exist_ok=True)

    ATOM = "http://www.w3.org/2005/Atom"
    DC = "http://purl.org/dc/terms/"
    ET.register_namespace("", ATOM)
    ET.register_namespace("dc", DC)

    feed = ET.Element(f"{{{ATOM}}}feed")
    ET.SubElement(feed, f"{{{ATOM}}}id").text = "urn:uuid:infzm-personal-opds"
    ET.SubElement(feed, f"{{{ATOM}}}title").text = "我的南方周末阅读书架"
    updated = (
        published[0].get("updated")
        if published
        else datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )
    ET.SubElement(feed, f"{{{ATOM}}}updated").text = updated
    author = ET.SubElement(feed, f"{{{ATOM}}}author")
    ET.SubElement(author, f"{{{ATOM}}}name").text = "个人 OPDS 阅读器"

    ET.SubElement(
        feed,
        f"{{{ATOM}}}link",
        {
            "rel": "self",
            "href": "catalog.xml",
            "type": "application/atom+xml;profile=opds-catalog;kind=acquisition",
        },
    )

    for issue in published:
        entry = ET.SubElement(feed, f"{{{ATOM}}}entry")
        ET.SubElement(entry, f"{{{ATOM}}}id").text = f"urn:infzm-digest:{issue['date']}"
        ET.SubElement(entry, f"{{{ATOM}}}title").text = issue["title"]
        ET.SubElement(entry, f"{{{ATOM}}}updated").text = issue["updated"]
        au = ET.SubElement(entry, f"{{{ATOM}}}author")
        ET.SubElement(au, f"{{{ATOM}}}name").text = "个人 OPDS 阅读器"
        ET.SubElement(entry, f"{{{DC}}}language").text = "zh-CN"
        ET.SubElement(entry, f"{{{ATOM}}}summary").text = (
            f"公开标题/摘要索引，共 {issue['count']} 篇；完整内容请访问原网站。"
        )
        ET.SubElement(
            entry,
            f"{{{ATOM}}}link",
            {
                "rel": "http://opds-spec.org/acquisition/open-access",
                "href": f"books/{issue['filename']}",
                "type": "application/epub+zip",
            },
        )

    tree = ET.ElementTree(feed)
    ET.indent(tree, space="  ")
    tree.write(SITE_DIR / "catalog.xml", encoding="utf-8", xml_declaration=True)


def generate_index(published: list[dict]):
    rows = []
    for issue in published:
        rows.append(
            f'<li><a href="books/{safe_text(issue["filename"])}">'
            f'{safe_text(issue["title"])}</a> — {issue["count"]} 篇</li>'
        )
    listing = "\n".join(rows) if rows else "<li>还没有发布任何一期；等待完整性检查通过。</li>"
    page = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>我的南方周末阅读书架</title>
<style>
body {{ max-width: 760px; margin: 40px auto; padding: 0 18px; font-family: system-ui, sans-serif; line-height: 1.65; }}
li {{ margin: 0.7em 0; }}
</style>
</head>
<body>
<h1>我的南方周末阅读书架</h1>
<p><a href="catalog.xml">OPDS catalog.xml</a></p>
<p>这里只保存公开标题、简短摘要和原文链接，不绕过登录或会员限制。</p>
<ul>{listing}</ul>
</body>
</html>
"""
    (SITE_DIR / "index.html").write_text(page, encoding="utf-8")
    (SITE_DIR / ".nojekyll").write_text("", encoding="utf-8")


def prune_old_issues(published: list[dict], keep: int) -> list[dict]:
    published = sorted(published, key=lambda x: x["date"], reverse=True)
    kept = published[:keep]
    kept_names = {x["filename"] for x in kept}
    if BOOKS_DIR.exists():
        for p in BOOKS_DIR.glob("*.epub"):
            if p.name not in kept_names:
                p.unlink(missing_ok=True)
    return kept


def main():
    cfg = load_json(CONFIG_PATH, {})
    if not cfg:
        raise SystemExit("config.json is missing or invalid")

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    SITE_DIR.mkdir(parents=True, exist_ok=True)
    BOOKS_DIR.mkdir(parents=True, exist_ok=True)

    tz = ZoneInfo(cfg.get("timezone", "Asia/Shanghai"))
    now_local = datetime.now(tz)
    target = now_local.date() - timedelta(days=int(cfg.get("target_days_ago", 1)))
    print(f"[target] {target.isoformat()} ({tz.key})")

    published = load_json(PUBLISHED_PATH, [])
    published_by_date = {x["date"]: x for x in published if "date" in x}

    # Keep rebuilding catalog/index even if the target date is already published.
    if target.isoformat() in published_by_date:
        print("[done] target date already published")
        published = prune_old_issues(published, int(cfg.get("keep_issues", 60)))
        save_json(PUBLISHED_PATH, published)
        generate_opds(published)
        generate_index(published)
        return

    session = make_session()
    candidates = collect_candidates(session, cfg)
    if not candidates:
        print("[wait] no candidates found; site may have changed or request was blocked")
        generate_opds(sorted(published, key=lambda x: x["date"], reverse=True))
        generate_index(sorted(published, key=lambda x: x["date"], reverse=True))
        return

    parsed = []
    failures = []
    for idx, item in enumerate(candidates, 1):
        try:
            a = parse_article(session, item, cfg, now_local)
            parsed.append(a)
            print(f"[article {idx}/{len(candidates)}] {a.get('date')} {a['title'][:50]}")
        except Exception as exc:
            failures.append({"url": item["url"], "error": str(exc)})
            print(f"[error] {item['url']}: {exc}")

    # Conservative rule: if any candidate page failed, do not publish this run.
    # This favors completeness over speed.
    if failures:
        print(f"[wait] {len(failures)} candidate pages failed; retry next scheduled run")
        generate_opds(sorted(published, key=lambda x: x["date"], reverse=True))
        generate_index(sorted(published, key=lambda x: x["date"], reverse=True))
        return

    target_articles = [a for a in parsed if a.get("date") == target.isoformat()]
    # Deduplicate article IDs while keeping first-seen topic.
    dedup = {}
    for a in target_articles:
        dedup.setdefault(a["id"], a)
    target_articles = list(dedup.values())

    target_articles.sort(key=lambda x: int(x["id"]) if x["id"].isdigit() else 0)

    min_articles = int(cfg.get("min_articles", 1))
    if len(target_articles) < min_articles:
        print(f"[wait] only {len(target_articles)} target-date articles; minimum is {min_articles}")
        generate_opds(sorted(published, key=lambda x: x["date"], reverse=True))
        generate_index(sorted(published, key=lambda x: x["date"], reverse=True))
        return

    ids = sorted(a["id"] for a in target_articles)
    digest_hash = hashlib.sha256(",".join(ids).encode("utf-8")).hexdigest()
    pending = load_json(PENDING_PATH, {})

    stable = (
        pending.get("date") == target.isoformat()
        and pending.get("hash") == digest_hash
        and pending.get("ids") == ids
    )

    if not stable:
        save_json(
            PENDING_PATH,
            {
                "date": target.isoformat(),
                "hash": digest_hash,
                "ids": ids,
                "count": len(ids),
                "observed_at": now_local.isoformat(timespec="seconds"),
            },
        )
        print(
            f"[wait] first/changed snapshot: {len(ids)} articles. "
            "A matching second scan is required before publishing."
        )
        generate_opds(sorted(published, key=lambda x: x["date"], reverse=True))
        generate_index(sorted(published, key=lambda x: x["date"], reverse=True))
        return

    filename = f"infzm-public-digest-{target.isoformat()}.epub"
    out_path = BOOKS_DIR / filename
    make_epub(target, target_articles, out_path)

    updated = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    issue = {
        "date": target.isoformat(),
        "title": f"南方周末公开内容摘要 · {target.isoformat()}",
        "filename": filename,
        "count": len(target_articles),
        "updated": updated,
    }
    published.append(issue)
    published = prune_old_issues(published, int(cfg.get("keep_issues", 60)))
    save_json(PUBLISHED_PATH, published)
    save_json(PENDING_PATH, {})

    generate_opds(published)
    generate_index(published)

    print(f"[published] {filename} with {len(target_articles)} articles")


if __name__ == "__main__":
    main()
