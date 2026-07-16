#!/usr/bin/env python3
"""Aggregate Gulf tech/finance/government news into data/news.json.

Runs hourly via GitHub Actions. Sources are Google News RSS queries scoped
to official news agencies and major English-language papers per country —
most Gulf outlets no longer publish working direct RSS feeds, and Google
News indexes them all with a single stable format.

Only items matching one of the topic categories below are kept (curated
to strategy / digital / AI / financial-services domains). Items are tagged
with country, category, and a "breaking" flag for very recent news.

Stdlib only — no pip installs needed in CI.
"""

import html
import json
import os
import re
import ssl
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

OUT_PATH = Path(__file__).resolve().parent.parent / "data" / "news.json"

# Per-country source scoping. Query is a Google News RSS search restricted
# to these domains; edit the site: lists to add or remove sources.
COUNTRY_SOURCES = {
    "UAE": ["wam.ae", "gulfnews.com", "khaleejtimes.com", "thenationalnews.com"],
    "KSA": ["spa.gov.sa", "arabnews.com", "saudigazette.com.sa"],
    "Qatar": ["qna.org.qa", "gulf-times.com", "thepeninsulaqatar.com"],
    "Oman": ["omannews.gov.om", "timesofoman.com", "omanobserver.om"],
    "Bahrain": ["bna.bh", "gdnonline.com", "newsofbahrain.com"],
    "Kuwait": ["kuna.net.kw", "kuwaittimes.com", "arabtimesonline.com"],
}

# Diplomatic / protocol / general-news noise: any title matching this is
# dropped before categorization, whatever else it matches.
NOISE = re.compile(
    r"receives|meets with|meets\b|holds (official )?talks|offers condolence|congratulat"
    r"|condemns|phone call|welcomes|bids farewell|extends greetings|deliver.{0,12}(aid|relief)"
    r"|ksrelief|weather|rainfall|temperature|prayer time|horoscope|football|cricket|tennis"
    r"|missile|drone attack|air strike|ceasefire|assisted dying"
    r"|visits\b|visit to|inaugurates|shooting|killed|injured|death|arrested|crash"
    r"|closes (lower|higher)|index (slips|gains|closes)",
    re.IGNORECASE,
)

# Items about clearly non-Gulf geographies are dropped unless the title
# also anchors to the region.
NON_GULF = re.compile(
    r"japan|canada|toronto|india\b|china|chinese|france|french|germany|europe|european"
    r"|pakistan|egypt|jordan|lebanon|syria|iran|iraq|yemen|turkey|russia|ukraine"
    r"|australia|brazil|africa|korea|singapore|malaysia|indonesia",
    re.IGNORECASE,
)
GULF_ANCHOR = re.compile(
    r"uae|dubai|abu dhabi|sharjah|emirat|saudi|ksa\b|riyadh|jeddah|neom|qatar|doha"
    r"|oman\b|muscat|bahrain|manama|kuwait|gcc\b|gulf|mena\b",
    re.IGNORECASE,
)

# Category rules, checked in order — first match wins. Keyword matching is
# case-insensitive on title text; word boundaries where it matters.
CATEGORY_RULES = [
    ("Digital ID", r"digital identit|digital id\b|uae pass|e-?kyc|biometric"),
    ("Payments", r"payment|digital wallet|remittance|bnpl|buy now pay later|card scheme|instant transfer|contactless"),
    ("Fintech", r"fintech|neobank|digital bank|open banking|crowdfund|venture capital|startup fund|seed round|series [ab]\b|blockchain|crypto"),
    ("Cyber", r"cyber|ransomware|hacking|data breach|data protection|infosec|phishing"),
    ("AI", r"\bai\b|ai-|artificial intelligence|machine learning|genai|generative|llm|chatbot|robot|autonomous|quantum"),
    ("Cloud", r"cloud (comput|servic|infrastructur|region|provider)|data cent|datacenter|hyperscaler|\baws\b|azure|google cloud|oracle cloud"),
    ("Banking", r"\bbank|cbuae|central bank|lender|cbdc|mortgage|sukuk|islamic finance|tadawul|bourse|stock (market|exchange)"),
    ("Regulation", r"regulator|regulation|regulatory (framework|sandbox)|compliance|licensing|corporate tax|vat\b"),
    ("Events", r"summit|conference|forum\b|expo\b|gitex|leap\b|web summit|cop2\d"),
    ("Government", r"e-?government|digital government|govtech|smart cit|digital econom|digital transformation"
                   r"|digital service|government (platform|app|portal|service|entity|digital)"
                   r"|national (ai|digital|data|tech|cyber|fintech|innovation) strateg"),
]

# Google News reports some agencies under their Arabic names or bare domains.
SOURCE_NAMES = {
    "وكالة الأنباء السعودية": "Saudi Press Agency",
    "وكالة أنباء الإمارات": "WAM",
    "وكالة الأنباء القطرية": "Qatar News Agency",
    "وكالة الأنباء الكويتية": "KUNA",
    "وكالة أنباء البحرين": "Bahrain News Agency",
    "وكالة الأنباء العمانية": "Oman News Agency",
    "kuna.net.kw": "KUNA",
    "bna.bh": "Bahrain News Agency",
    "omannews.gov.om": "Oman News Agency",
    "wam.ae": "WAM",
    "spa.gov.sa": "Saudi Press Agency",
    "qna.org.qa": "Qatar News Agency",
}

# e.g. KUNA appends " - Politics - 13/07/2026" to headlines.
SECTION_DATE_SUFFIX = re.compile(r"\s+-\s+[A-Za-z &]+\s+-\s+\d{2}/\d{2}/\d{4}$")

BREAKING_WINDOW_HOURS = 12
MAX_PER_COUNTRY = 12
MAX_TOTAL = 60
FRESHNESS_DAYS = 7

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 josephkhater.com news bot"


def ssl_context():
    ctx = ssl.create_default_context()
    if ctx.cert_store_stats().get("x509_ca", 0) == 0:
        # Python builds without a linked CA bundle (common on macOS): fall
        # back to the system bundle so local runs work like CI does.
        for bundle in ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt"):
            if os.path.exists(bundle):
                ctx = ssl.create_default_context(cafile=bundle)
                break
    return ctx


def fetch_feed(country, domains):
    sites = " OR ".join(f"site:{d}" for d in domains)
    query = urllib.parse.quote(f"({sites}) when:{FRESHNESS_DAYS}d")
    url = f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30, context=ssl_context()) as resp:
        return resp.read()


def parse_items(xml_bytes, country):
    items = []
    root = ET.fromstring(xml_bytes)
    for node in root.iter("item"):
        title = (node.findtext("title") or "").strip()
        link = (node.findtext("link") or "").strip()
        source = (node.findtext("source") or "").strip()
        pub = (node.findtext("pubDate") or "").strip()
        if not title or not link:
            continue
        # Google News titles end with " - Source Name"
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3].strip()
        title = SECTION_DATE_SUFFIX.sub("", html.unescape(title)).strip()
        try:
            published = parsedate_to_datetime(pub).astimezone(timezone.utc)
        except (TypeError, ValueError):
            continue
        source = SOURCE_NAMES.get(source, source)
        items.append({"title": title, "url": link, "source": source or country,
                      "country": country, "published": published})
    return items


def categorize(title):
    if NOISE.search(title):
        return None
    if NON_GULF.search(title) and not GULF_ANCHOR.search(title):
        return None
    low = title.lower()
    for name, pattern in CATEGORY_RULES:
        if re.search(pattern, low):
            return name
    return None


def main():
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=FRESHNESS_DAYS)
    all_items = []
    for country, domains in COUNTRY_SOURCES.items():
        try:
            raw = fetch_feed(country, domains)
            items = parse_items(raw, country)
        except Exception as exc:  # one dead feed must not sink the run
            print(f"WARN {country}: {exc}")
            continue
        kept = []
        seen_titles = set()
        for it in sorted(items, key=lambda x: x["published"], reverse=True):
            if it["published"] < cutoff:
                continue
            cat = categorize(it["title"])
            if not cat:
                continue
            key = it["title"].lower()[:80]
            if key in seen_titles:
                continue
            seen_titles.add(key)
            it["category"] = cat
            kept.append(it)
            if len(kept) >= MAX_PER_COUNTRY:
                break
        print(f"{country}: {len(items)} fetched, {len(kept)} kept")
        all_items.extend(kept)

    all_items.sort(key=lambda x: x["published"], reverse=True)
    deduped, seen = [], set()
    for it in all_items:
        key = it["title"].lower()[:80]
        if key in seen:
            continue
        seen.add(key)
        deduped.append(it)
    all_items = deduped[:MAX_TOTAL]

    breaking_cutoff = now - timedelta(hours=BREAKING_WINDOW_HOURS)
    payload = {
        "updated": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "items": [
            {
                "t": it["title"],
                "u": it["url"],
                "s": it["source"],
                "c": it["country"],
                "k": it["category"],
                "p": it["published"].strftime("%Y-%m-%dT%H:%M:%SZ"),
                "b": it["published"] >= breaking_cutoff,
            }
            for it in all_items
        ],
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"Wrote {len(payload['items'])} items to {OUT_PATH}")


if __name__ == "__main__":
    main()
