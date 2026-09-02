#!/usr/bin/env python3
"""Generate the weekly executive briefing (data/pulse.json) from data/news.json.

Uses Google's Gemini API (free tier via AI Studio) through its OpenAI-compatible
endpoint. GitHub Models was retired on 30 July 2026, so a provider key is now
required; the Gemini free tier needs no credit card and its limits comfortably
cover a once-a-week job.

Reads the aggregated raw news feed produced by fetch_news.py, asks a model to
synthesise an executive briefing, validates the result against the schema that
index.html expects, and writes data/pulse.json.

Design notes:
- Pure standard library (urllib) — no pip dependencies.
- The model only supplies analytical content. `updated` and `period` are set in
  Python so they are always correct and deterministic.
- Story source URLs are constrained to URLs that actually appear in news.json, so
  the "Source" links on the site are real, never hallucinated.
- On any validation failure the script exits non-zero and does NOT write the file,
  so the workflow surfaces the problem instead of committing a broken briefing.

Environment:
- GEMINI_API_KEY      (required)  — free key from https://aistudio.google.com/apikey,
                                    stored as a GitHub Actions secret.
- PULSE_MODEL         (optional)  — model id, default "gemini-2.5-flash".
- PULSE_ENDPOINT      (optional)  — override the OpenAI-compatible endpoint.
- PULSE_STORY_COUNT   (optional)  — target number of top stories, default 10.
"""

import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NEWS_PATH = ROOT / "data" / "news.json"
OUT_PATH = ROOT / "data" / "pulse.json"

API_URL = os.environ.get(
    "PULSE_ENDPOINT",
    "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
)
MODEL = os.environ.get("PULSE_MODEL", "gemini-2.5-flash")
STORY_COUNT = int(os.environ.get("PULSE_STORY_COUNT", "10"))
MAX_TOKENS = 8000

SYSTEM_PROMPT = (
    "You are a senior GCC strategy analyst producing a weekly executive briefing on "
    "Gulf technology, finance, and government developments for a C-suite and board "
    "audience at banks and government-technology organisations. You are neutral, "
    "analytical, and precise, and you return only what is asked for."
)


def week_period(now: datetime) -> str:
    """Human label for the trailing 7-day window, e.g. 'Week of 12-18 August 2026'."""
    start = now - timedelta(days=6)
    if start.month == now.month:
        return f"Week of {start.day}\u2013{now.day} {now:%B %Y}"
    return f"Week of {start.day} {start:%B}\u2013{now.day} {now:%B %Y}"


def load_news() -> dict:
    if not NEWS_PATH.exists():
        sys.exit(f"ERROR: {NEWS_PATH} not found. Run fetch_news.py first.")
    data = json.loads(NEWS_PATH.read_text(encoding="utf-8"))
    if not data.get("items"):
        sys.exit("ERROR: news.json has no items to summarise.")
    return data


def build_user_prompt(news: dict) -> str:
    items = news["items"]
    lines = []
    for it in items:
        flag = " [BREAKING]" if it.get("b") else ""
        lines.append(
            f'- title: {it.get("t","")}{flag}\n'
            f'  country: {it.get("c","")} | category: {it.get("k","")} | '
            f'source: {it.get("s","")} | published: {it.get("p","")}\n'
            f'  url: {it.get("u","")}'
        )
    feed = "\n".join(lines)

    return f"""Below is this week's aggregated Gulf news feed (raw headlines from official
agencies, regulators, exchanges, and regional outlets). Work ONLY from these items.

=== NEWS FEED ({len(items)} items) ===
{feed}
=== END FEED ===

Write the briefing and return it as a SINGLE JSON object and NOTHING else — no
markdown, no code fences, no commentary before or after. Use this exact schema:

{{
  "summary": "<one paragraph, 150-220 words: synthesise the week's most important
              developments and what they mean for executives. Neutral, no hype.>",
  "stories": [
    {{
      "r": <int rank, 1 = highest impact>,
      "c": "<country: UAE / KSA / Qatar / Oman / Bahrain / Regional>",
      "k": "<short category: AI / Government / Banking / Fintech / Digital ID / ...>",
      "i": <int 1-10 executive-impact score>,
      "t": "<concise story title>",
      "b": "<2-3 factual sentences describing what happened>",
      "w": "<1-2 sentences: why it matters strategically for a GCC executive>",
      "s": "<source name, from the feed>",
      "u": "<source URL — MUST be one of the url values from the feed above>"
    }}
    // top {STORY_COUNT} most consequential stories; fewer only if fewer substantive items exist
  ],
  "themes": [
    {{ "t": "<theme title>", "b": "<2-3 sentences tying several stories together>" }}
    // exactly 3 themes
  ],
  "actions": [
    {{ "role": "CIO", "items": ["<action>", "<action>", "<action>"] }},
    {{ "role": "CISO", "items": ["<action>", "<action>", "<action>"] }},
    {{ "role": "Chief Data Officer", "items": ["<action>", "<action>", "<action>"] }},
    {{ "role": "Transformation Director", "items": ["<action>", "<action>", "<action>"] }}
    // exactly these 4 roles, in this order, 3 concrete actions each
  ],
  "market": [
    {{ "a": "<area: AI / Government Technology / Digital ID / Banking / Payments / ...>",
       "b": "<1-2 sentences on the week's movement in this area>" }}
    // 4-5 areas
  ],
  "ceo": {{
    "lead": "If I were advising the CEO of a Gulf bank or government technology organisation, the three issues I would put on the leadership agenda this week are...",
    "items": ["<issue 1>", "<issue 2>", "<issue 3>"]
  }}
}}

Rules:
- Every story's "u" MUST be copied verbatim from a url in the feed. Never invent URLs.
- Do not include "updated" or "period" — those are added programmatically.
- Return valid JSON only.
"""


def call_model(user_prompt: str) -> str:
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        sys.exit("ERROR: GEMINI_API_KEY is not set (add it as a GitHub Actions secret).")

    body = json.dumps({
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "temperature": 0.4,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    }).encode("utf-8")

    req = urllib.request.Request(
        API_URL,
        data=body,
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {api_key}",
            "accept": "application/json",
        },
        method="POST",
    )

    last_err = None
    for _ in range(3):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            text = (data.get("choices", [{}])[0].get("message", {}) or {}).get("content", "").strip()
            if not text:
                raise ValueError("empty model response")
            return text
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            last_err = f"HTTP {e.code}: {detail}"
            if 400 <= e.code < 500 and e.code != 429:
                break
        except Exception as e:  # noqa: BLE001 - network/parse errors -> retry
            last_err = str(e)
    sys.exit(f"ERROR: model call failed after retries: {last_err}")


def extract_json(text: str) -> dict:
    """Pull the JSON object out of the model response, tolerating stray wrapping."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1]
        if t.lstrip().lower().startswith("json"):
            t = t.lstrip()[4:]
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end == -1:
        sys.exit("ERROR: no JSON object found in model response.")
    try:
        return json.loads(t[start:end + 1])
    except json.JSONDecodeError as e:
        sys.exit(f"ERROR: model did not return valid JSON: {e}")


def validate(pulse: dict, allowed_urls: set) -> None:
    """Raise SystemExit on any structural problem so the workflow fails loudly."""
    def need(cond, msg):
        if not cond:
            sys.exit(f"ERROR: validation failed - {msg}")

    need(isinstance(pulse.get("summary"), str) and len(pulse["summary"]) > 60,
         "missing or too-short 'summary'")

    stories = pulse.get("stories")
    need(isinstance(stories, list) and stories, "'stories' must be a non-empty list")
    for i, s in enumerate(stories):
        for key in ("r", "c", "k", "i", "t", "b", "w", "s", "u"):
            need(key in s, f"story {i} missing '{key}'")
        need(isinstance(s["t"], str) and s["t"], f"story {i} empty title")
        if isinstance(s["u"], str) and s["u"].startswith("http"):
            need(s["u"] in allowed_urls, f"story {i} uses a URL not present in news.json")

    themes = pulse.get("themes")
    need(isinstance(themes, list) and themes, "'themes' must be a non-empty list")
    for i, t in enumerate(themes):
        need("t" in t and "b" in t, f"theme {i} missing 't'/'b'")

    actions = pulse.get("actions")
    need(isinstance(actions, list) and actions, "'actions' must be a non-empty list")
    for i, a in enumerate(actions):
        need("role" in a and isinstance(a.get("items"), list) and a["items"],
             f"action {i} missing 'role' or non-empty 'items'")

    market = pulse.get("market")
    need(isinstance(market, list) and market, "'market' must be a non-empty list")
    for i, m in enumerate(market):
        need("a" in m and "b" in m, f"market {i} missing 'a'/'b'")

    ceo = pulse.get("ceo")
    need(isinstance(ceo, dict) and ceo.get("lead") and isinstance(ceo.get("items"), list) and ceo["items"],
         "'ceo' must have 'lead' and non-empty 'items'")


def main():
    now = datetime.now(timezone.utc)
    news = load_news()
    allowed_urls = {it.get("u") for it in news["items"] if it.get("u")}

    raw = call_model(build_user_prompt(news))
    pulse = extract_json(raw)
    validate(pulse, allowed_urls)

    for s in pulse["stories"]:
        try:
            s["r"] = int(s["r"])
            s["i"] = int(s["i"])
        except (TypeError, ValueError):
            pass
    pulse["stories"].sort(key=lambda s: s.get("r", 999))

    ordered = {
        "updated": now.strftime("%Y-%m-%d"),
        "period": week_period(now),
        "summary": pulse["summary"],
        "stories": pulse["stories"],
        "themes": pulse["themes"],
        "actions": pulse["actions"],
        "market": pulse["market"],
        "ceo": pulse["ceo"],
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(
        json.dumps(ordered, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote briefing to {OUT_PATH}: {len(ordered['stories'])} stories, "
          f"period '{ordered['period']}'.")


if __name__ == "__main__":
    main()
