#!/usr/bin/env python3
"""Aggregate Gulf tech/finance/government news into data/news.json.

Runs hourly via GitHub Actions. Sources are Google News RSS queries scoped
to official news agencies, government portals, regulators, exchanges, and
major English-language papers per country — most Gulf outlets no longer
publish working direct RSS feeds, and Google News indexes them all with a
single stable format. Domains Google News does not index simply contribute
nothing; listing them is harmless.

Only items matching one of the topic categories below are kept (curated
to strategy / digital / AI / financial-services domains). Items are tagged
with country, category, and a "breaking" flag for very recent news.
Regional (pan-GCC) sources get their country detected from the headline,
falling back to a "GCC" tag that appears under the All filter only.

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

# Per-country source scoping. Queries are Google News RSS searches
# restricted to these domains (chunked — Google caps query terms).
COUNTRY_SOURCES = {
    "UAE": [
        # agencies & papers
        "wam.ae", "gulfnews.com", "khaleejtimes.com", "thenationalnews.com",
        "arabianbusiness.com",
        # federal & emirate government / media offices
        "u.ae", "uaecabinet.ae", "mofa.gov.ae", "mediaoffice.abudhabi",
        "mediaoffice.ae", "sgmb.ae", "ajmanmedia.ae", "rakmediaoffice.ae",
        "fujairah.ae", "digitaluaq.ae",
        # financial regulators & centres
        "centralbank.ae", "sca.gov.ae", "difc.com", "dfsa.ae", "adgm.com",
        "vara.ae",
        # tax, fiscal & economic policy
        "mof.gov.ae", "tax.gov.ae", "moet.gov.ae", "moiat.gov.ae",
        # investment & economic development
        "mubadala.com", "adq.ae", "added.gov.ae", "dubaichambers.com",
        # markets & exchanges
        "dfm.ae", "adx.ae", "nasdaqdubai.com", "borsedubai.ae",
    ],
    "KSA": [
        "spa.gov.sa", "arabnews.com", "saudigazette.com.sa", "argaam.com",
        "my.gov.sa", "media.gov.sa", "mofa.gov.sa", "vision2030.gov.sa",
    ],
    "Qatar": ["qna.org.qa", "gulf-times.com", "thepeninsulaqatar.com", "dohanews.co"],
    "Oman": ["omannews.gov.om", "timesofoman.com", "omanobserver.om"],
    "Bahrain": ["bna.bh", "gdnonline.com", "newsofbahrain.com", "tradearabia.com"],
    "Kuwait": ["kuna.net.kw", "kuwaittimes.com", "arabtimesonline.com"],
}

# Pan-GCC business wires — country detected from the headline.
REGIONAL_SOURCES = ["zawya.com", "meed.com", "gulfbusiness.com", "wamda.com", "magnitt.com"]

# Ordered country detection for regional items; first match wins.
COUNTRY_HINTS = [
    ("UAE", re.compile(r"uae|dubai|abu dhabi|sharjah|emirat|ajman|ras al khaimah|fujairah|difc|adgm|adx\b|dfm\b", re.I)),
    ("KSA", re.compile(r"saudi|ksa\b|riyadh|jeddah|neom|tadawul|vision 2030", re.I)),
    ("Qatar", re.compile(r"qatar|doha", re.I)),
    ("Oman", re.compile(r"oman\b|omani|muscat", re.I)),
    ("Bahrain", re.compile(r"bahrain|manama", re.I)),
    ("Kuwait", re.compile(r"kuwait", re.I)),
]

QUERY_CHUNK = 8  # site: terms per Google News query (32-term query limit)

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
    r"japan|canada|toronto|india\b|china|chinese|hong kong|france|french|germany|europe|european"
    r"|pakistan|egypt|cairo|jordan|lebanon|syria|iran|iraq|yemen|turkey|russia|ukraine"
    r"|australia|brazil|africa|burkina|nigeria|kenya|korea|singapore|malaysia|indonesia",
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
    ("Digital ID", r"digital identit|digital id\b|identity verification|national id\b|uae pass|ekey|e-?kyc|biometric"),
    ("Payments", r"payment|digital wallet|remittance|bnpl|buy now pay later|card scheme|instant transfer|contactless"),
    ("Fintech", r"fintech|financial innovation|regtech|insurtech|neobank|digital bank|open banking|open finance|crowdfund|venture capital|startup fund|seed round|series [ab]\b|blockchain|crypto|virtual asset|digital asset|tokeni[sz]"),
    ("Cyber", r"cyber|ransomware|hacking|data breach|data protection|infosec|phishing"),
    ("AI", r"\bai\b|ai-|artificial intelligence|machine learning|genai|generative|llm|chatbot|robot|autonomous|quantum"),
    ("Cloud", r"cloud (comput|servic|infrastructur|region|provider|market|adoption|strategy)|sovereign cloud|data cent|datacenter|hyperscaler|\baws\b|azure|google cloud|oracle cloud"),
    ("Banking", r"\bbank|cbuae|central bank|lender|cbdc|mortgage|sukuk|islamic finance|tadawul|bourse|stock (market|exchange)|ipo\b|dividend"),
    ("Regulation", r"regulator|regulation|regulatory (framework|sandbox)|compliance|licensing|corporate tax|vat\b|enforcement"),
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
    "u.ae": "UAE Government",
    "uaecabinet.ae": "UAE Cabinet",
    "centralbank.ae": "Central Bank of the UAE",
    "mediaoffice.ae": "Dubai Media Office",
    "mediaoffice.abudhabi": "Abu Dhabi Media Office",
    "my.gov.sa": "Saudi National Portal",
    "media.gov.sa": "Saudi Ministry of Media",
    "مكتب أبوظبي الإعلامي": "Abu Dhabi Media Office",
    "المكتب الإعلامي لحكومة عجمان": "Ajman Media Office",
    "وزارة الاقتصاد والسياحة": "UAE Ministry of Economy & Tourism",
    "وزارة الخارجية الإماراتية": "UAE Ministry of Foreign Affairs",
    "Qatar news agency": "Qatar News Agency",
    "ZAWYA": "Zawya",
}

# e.g. KUNA appends " - Politics - 13/07/2026" to headlines.
SECTION_DATE_SUFFIX = re.compile(r"\s+-\s+[A-Za-z &]+\s+-\s+\d{2}/\d{2}/\d{4}$")

BREAKING_WINDOW_HOURS = 12
MAX_PER_COUNTRY = 12
MAX_TOTAL = 80
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


def chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def fetch_feed(domains):
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
        items.append({"title": title, "url": link, "source": source or country or "GCC",
                      "country": country, "published": published})
    return items


def detect_country(title):
    for name, pattern in COUNTRY_HINTS:
        if pattern.search(title):
            return name
    return "GCC"


def categorize(title):
    if NOISE.search(title):
        return None
    if NON_GULF.search(title) and not GULF_ANCHOR.search(title):
        return None
    if " | " in title:  # nav/page titles that leak in from indexed portals
        return None
    low = title.lower()
    for name, pattern in CATEGORY_RULES:
        if re.search(pattern, low):
            return name
    return None


def gather(group_name, domains, country):
    """Fetch a source group (chunked) and return categorized items."""
    items = []
    for chunk in chunks(domains, QUERY_CHUNK):
        try:
            items.extend(parse_items(fetch_feed(chunk), country))
        except Exception as exc:  # one dead chunk must not sink the run
            print(f"WARN {group_name} ({chunk[0]}...): {exc}")
    kept = []
    for it in items:
        cat = categorize(it["title"])
        if not cat:
            continue
        it["category"] = cat
        if it["country"] is None:
            # Regional wires carry global PR content: require an explicit
            # Gulf anchor, then pin to a country (or GCC-wide).
            if not GULF_ANCHOR.search(it["title"]):
                continue
            it["country"] = detect_country(it["title"])
        kept.append(it)
    print(f"{group_name}: {len(items)} fetched, {len(kept)} kept")
    return kept


def main():
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=FRESHNESS_DAYS)

    all_items = []
    for country, domains in COUNTRY_SOURCES.items():
        all_items.extend(gather(country, domains, country))
    all_items.extend(gather("Regional", REGIONAL_SOURCES, None))

    all_items = [it for it in all_items if it["published"] >= cutoff]
    all_items.sort(key=lambda x: x["published"], reverse=True)

    deduped, seen, per_country = [], set(), {}
    for it in all_items:
        key = it["title"].lower()[:80]
        if key in seen:
            continue
        if per_country.get(it["country"], 0) >= MAX_PER_COUNTRY:
            continue
        seen.add(key)
        per_country[it["country"]] = per_country.get(it["country"], 0) + 1
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
    print(f"Wrote {len(payload['items'])} items to {OUT_PATH} — by country: {per_country}")


if __name__ == "__main__":
    main()
