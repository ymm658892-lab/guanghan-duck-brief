#!/usr/bin/env python3
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

TAIPEI = timezone(timedelta(hours=8))
INDEX_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "index.html")

DIRECT_FEEDS = {
    "markets": {"url": "https://feeds.feedburner.com/rsscna/finance", "fallback_source": "中央社"},
    "tech": {"url": "https://technews.tw/feed/", "fallback_source": "科技新報"},
}

ROBOTICS_QUERY = "機器人 OR 人形機器人 OR 機器人產業 OR 協作機器人 OR 自動化設備"
ROBOTICS_KEYWORDS = ["機器人", "人形", "機械臂", "協作機器人", "仿生", "cobot", "humanoid", "Robot", "無人機", "自駕"]
ROBOTICS_FALLBACK_FEEDS = [
    {"url": "https://technews.tw/feed/", "fallback_source": "科技新報"},
    {"url": "https://www.ithome.com.tw/rss", "fallback_source": "iThome"},
]

TILES = [
    {"symbol": "%5ETWII", "label": "台股加權指數"},
    {"symbol": "%5EIXIC", "label": "那斯達克"},
    {"symbol": "%5ESOX", "label": "費半指數"},
    {"symbol": "TSM", "label": "台積電ADR"},
]

WEEKDAYS = ["一", "二", "三", "四", "五", "六", "日"]

MASCOT_ACTIONS = ["jump", "spin", "bow", "wave", "shiver", "salute", "dance"]


def http_get(url, headers=None, timeout=20, retries=3, backoff_base=4):
    headers = headers or {}
    headers.setdefault("User-Agent", "Mozilla/5.0 (compatible; GuanghanDuckBrief/1.0)")
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:500]
            last_err = f"{e.code} {e.reason}: {body}"
        except Exception as e:
            last_err = e
        if attempt < retries - 1:
            time.sleep(backoff_base * (attempt + 1))
    raise RuntimeError(f"GET failed after {retries} tries: {url} ({last_err})")


def parse_pubdate(raw):
    raw = raw.strip()
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TAIPEI)
        return dt.astimezone(timezone.utc)
    return None


def parse_rss_items(data):
    root = ET.fromstring(data)
    items = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_raw = (item.findtext("pubDate") or "").strip()
        source_el = item.find("source")
        source_name = (source_el.text or "").strip() if source_el is not None else ""
        if not title or not link:
            continue
        pub_dt = parse_pubdate(pub_raw)
        if pub_dt is None:
            continue
        items.append({"title": title, "link": link, "source": source_name, "pub_dt": pub_dt})
    return items


def dedupe_and_filter(items, max_age_hours, fallback_source, limit):
    now = datetime.now(timezone.utc)
    filtered = []
    for it in items:
        age_hours = (now - it["pub_dt"]).total_seconds() / 3600
        if age_hours > max_age_hours or age_hours < -1:
            continue
        title = it["title"]
        source = it["source"] or fallback_source
        clean_title = re.sub(r"\s*[-|]\s*" + re.escape(source) + r"$", "", title) if source else title
        filtered.append({
            "title": clean_title,
            "link": it["link"],
            "source": source,
            "pubDate": it["pub_dt"].isoformat(),
        })
    filtered.sort(key=lambda x: x["pubDate"], reverse=True)
    seen = set()
    deduped = []
    for it in filtered:
        key = it["title"][:12]
        if key in seen:
            continue
        seen.add(key)
        deduped.append(it)
    return deduped[:limit]


def fetch_google_news(query, limit=8, max_age_hours=30):
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": query, "hl": "zh-TW", "gl": "TW", "ceid": "TW:zh-Hant"}
    )
    data = http_get(url, retries=4, backoff_base=6)
    items = parse_rss_items(data)
    return dedupe_and_filter(items, max_age_hours, "", limit)


def fetch_direct_feed(feed_conf, limit=10, max_age_hours=30):
    data = http_get(feed_conf["url"], retries=3, backoff_base=4)
    items = parse_rss_items(data)
    return dedupe_and_filter(items, max_age_hours, feed_conf["fallback_source"], limit)


def fetch_robotics_candidates(limit=8, max_age_hours=30):
    try:
        items = fetch_google_news(ROBOTICS_QUERY, limit=limit, max_age_hours=max_age_hours)
    except Exception as e:
        print(f"  Google News robotics search failed: {e}")
        items = []

    if len(items) < 5:
        print(f"  Only {len(items)} from Google News, supplementing with keyword-filtered direct feeds...")
        extra = []
        for feed_conf in ROBOTICS_FALLBACK_FEEDS:
            try:
                raw = fetch_direct_feed(feed_conf, limit=40, max_age_hours=72)
            except Exception as e:
                print(f"  fallback feed {feed_conf['url']} failed: {e}")
                continue
            extra.extend(r for r in raw if any(k in r["title"] for k in ROBOTICS_KEYWORDS))
        seen_titles = {it["title"][:12] for it in items}
        for it in extra:
            key = it["title"][:12]
            if key not in seen_titles:
                seen_titles.add(key)
                items.append(it)
    return items[:limit]


def fetch_indicator(symbol):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    try:
        data = http_get(url, timeout=15, retries=2)
        meta = json.loads(data)["chart"]["result"][0]["meta"]
        price = meta.get("regularMarketPrice")
        prev = meta.get("previousClose") or meta.get("chartPreviousClose")
        if price is None or prev is None:
            return None
        pct = (price - prev) / prev * 100
        return {"price": price, "pct": pct}
    except Exception:
        return None


def build_indicators():
    tiles = []
    for t in TILES:
        result = fetch_indicator(t["symbol"])
        if result is None:
            tiles.append({
                "label": t["label"], "value": "資料暫缺", "valueClass": "",
                "delta": "", "deltaClass": "",
            })
            continue
        rise = result["pct"] >= 0
        cls = "rise" if rise else "fall"
        value_str = f"{result['price']:,.2f}"
        delta_str = f"{'+' if rise else ''}{result['pct']:.2f}%"
        tiles.append({
            "label": t["label"], "value": value_str, "valueClass": cls,
            "delta": delta_str, "deltaClass": cls,
        })
    return tiles


def candidate_models(api_key):
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
    data = json.loads(http_get(url, timeout=20, retries=3))
    names = []
    for m in data.get("models", []):
        methods = m.get("supportedGenerationMethods", [])
        name = m.get("name", "")
        if "generateContent" in methods and "flash" in name.lower():
            names.append(name.split("/")[-1])
    if not names:
        raise RuntimeError("No usable Gemini flash model found via ListModels")

    def version_key(n):
        m = re.search(r"gemini-(\d+(?:\.\d+)?)", n)
        ver = float(m.group(1)) if m else 0.0
        bad = any(x in n for x in ("exp", "preview", "8b", "thinking", "lite", "image", "tts"))
        return (bad, -ver, n)

    names.sort(key=version_key)
    return names


def call_gemini_once(prompt, api_key, model):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.7},
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=150) as resp:
            raw = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", "replace")[:800]
        raise RuntimeError(f"Gemini API error {e.code}: {err_body}")
    except Exception as e:
        raise RuntimeError(f"Gemini request failed: {e!r}")
    try:
        text = raw["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(text)
    except Exception as e:
        raise RuntimeError(f"Gemini response parse failed: {e!r}; raw={json.dumps(raw)[:500]}")


def call_gemini(prompt, api_key, forced_model=None):
    models = [forced_model] if forced_model else candidate_models(api_key)
    last_err = None
    for model in models:
        try:
            print(f"  trying model: {model}")
            return call_gemini_once(prompt, api_key, model), model
        except Exception as e:
            last_err = e
            print(f"  model {model} failed: {e}")
    raise RuntimeError(f"All Gemini models failed. Last error: {last_err}")


def build_prompt(slot, date_str, candidates, tiles, prev_mascot_message, allow_outfit):
    def fmt_candidates(cat):
        lines = []
        for i, c in enumerate(candidates[cat]):
            lines.append(f'{i}: 標題「{c["title"]}」 來源:{c["source"]}')
        return "\n".join(lines)

    slot_context = {
        "0730": "07:30 通勤上班前的晨間簡報，讀者剛起床，重點是隔夜美股/國際動態跟今天要注意什麼",
        "1300": "13:00 台股盤中午間更新，讀者在上班休息時間看，重點是盤中走勢與台股相關發展",
        "2000": "20:00 晚間更新，讀者剛下班/吃晚餐，重點是盤後總結跟晚間國際市場展望",
    }[slot]

    tiles_desc = "\n".join(f'- {t["label"]}: {t["value"]} ({t["delta"]})' for t in tiles)

    outfit_instruction = (
        '"outfit": 從這個清單挑0~2件給鴨子穿(可以是空陣列): '
        '["partyhat","santahat","gradcap","sunglasses","mask","scarf","bowtie","flag","umbrella"]，'
        '挑選要跟今天新聞氣氛/節慶有關聯，沒有特別理由就給空陣列,'
        if allow_outfit else
        '不要輸出 outfit 欄位(這個時段不能改動鴨子服裝)。'
    )

    pick_counts = {cat: min(5, len(candidates[cat])) for cat in ("markets", "robotics", "tech")}

    return f"""你是「光漢小鴨早晨速報」的內容編輯，服務對象是通勤族，語氣輕鬆但專業，繁體中文台灣用語。
現在是{slot_context}，日期是{date_str}。

以下是這個時段從新聞來源實際抓到的候選新聞(只有標題/來源，沒有全文)，請你從每個分類選出最重要的幾則(markets選{pick_counts['markets']}則、robotics選{pick_counts['robotics']}則、tech選{pick_counts['tech']}則，數量必須剛好符合)，並幫每則寫出：
- title: 可直接沿用或小幅潤飾原標題(不能改變原意)
- teaser: 8~14字的精簡副標
- full: 依據標題與常識，寫2~3句、約80~120字的白話說明(不要編造原文沒有的具體數字或引言，只做合理的脈絡解讀)
- think: 「延伸思考」，2~3句話，解釋這則新聞對讀者(一般投資人/上班族)的意義或啟發，白話、有洞見
- question: 「思考題」，1句反思性問題，引導讀者思考

分類與候選新聞：
【台股 markets】
{fmt_candidates('markets')}

【機器人 robotics】
{fmt_candidates('robotics')}

【科技 tech】
{fmt_candidates('tech')}

今日四項指標行情(已經是真實數字，不要更改，只需要你寫一句 note 說明這組指標在這個時段代表什麼意義)：
{tiles_desc}

鴨子吉祥物「光漢小鴨」上一次講的話是：「{prev_mascot_message or '(尚無紀錄)'}」，這次的 message 必須跟上次讀起來明顯不同、不能只是換句話說，長度20~28字，是給讀者的鼓勵語，可以呼應今天的新聞。action 從這個清單選一個: {MASCOT_ACTIONS}。{outfit_instruction}

請嚴格輸出以下 JSON 格式(不要有多餘文字)：
{{
  "tldr": "一句話總結今天最重要的事，40~60字",
  "indicators_note": "說明這4格指標代表什麼，30~50字",
  "picks": {{
    "markets": [{{"idx": 0, "title": "...", "teaser": "...", "full": "...", "think": "...", "question": "..."}}, ... 共{pick_counts['markets']}則],
    "robotics": [... 共{pick_counts['robotics']}則],
    "tech": [... 共{pick_counts['tech']}則]
  }},
  "mascot": {{"action": "...", "message": "...", "outfit": [...]}}
}}
idx 必須對應到我給你的候選新聞編號，且同一分類不能選重複的 idx。"""


def replace_slot(html, slot, new_data):
    pattern = re.compile(
        r'(<script type="application/json" id="slot-' + slot + r'">)(.*?)(</script>)',
        re.S,
    )
    new_json = json.dumps(new_data, ensure_ascii=False)
    replacement = r"\1" + new_json.replace("\\", "\\\\") + r"\3"
    new_html, n = pattern.subn(replacement, html, count=1)
    if n != 1:
        raise RuntimeError(f"slot-{slot} block not found or matched {n} times")
    return new_html


def replace_mascot(html, mascot_data):
    pattern = re.compile(
        r'(<script type="application/json" id="mascot-today">)(.*?)(</script>)',
        re.S,
    )
    new_json = json.dumps(mascot_data, ensure_ascii=False)
    replacement = r"\1" + new_json.replace("\\", "\\\\") + r"\3"
    new_html, n = pattern.subn(replacement, html, count=1)
    if n != 1:
        raise RuntimeError(f"mascot-today block not found or matched {n} times")
    return new_html


def extract_json_block(html, block_id, is_slot=True):
    key = f'slot-{block_id}' if is_slot else block_id
    m = re.search(r'<script type="application/json" id="' + re.escape(key) + r'">(.*?)</script>', html, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


def main():
    slot = os.environ.get("SLOT")
    if slot not in ("0730", "1300", "2000"):
        print(f"Invalid SLOT env var: {slot}", file=sys.stderr)
        sys.exit(1)

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("Missing GEMINI_API_KEY env var", file=sys.stderr)
        sys.exit(1)
    model = os.environ.get("GEMINI_MODEL") or None

    now = datetime.now(TAIPEI)
    date_str = f"{now.month}月{now.day}日．週{WEEKDAYS[now.weekday()]}"

    print(f"[1/5] Fetching news candidates for slot {slot}...")
    candidates = {}
    for cat, feed_conf in DIRECT_FEEDS.items():
        items = fetch_direct_feed(feed_conf, limit=10, max_age_hours=30)
        if len(items) < 5:
            raise RuntimeError(f"Not enough fresh news for category {cat}: only {len(items)} items")
        candidates[cat] = items
        print(f"  {cat}: {len(items)} candidates")

    print("  robotics: fetching (Google News + fallback feeds)...")
    robotics_items = fetch_robotics_candidates(limit=8, max_age_hours=30)
    if len(robotics_items) < 3:
        raise RuntimeError(f"Not enough fresh news for category robotics: only {len(robotics_items)} items")
    candidates["robotics"] = robotics_items
    print(f"  robotics: {len(robotics_items)} candidates")

    print("[2/5] Fetching market indicators...")
    tiles = build_indicators()

    html = open(INDEX_PATH, encoding="utf-8").read()
    prev_mascot = extract_json_block(html, "mascot-today", is_slot=False) or {}
    prev_message = prev_mascot.get("message", "")

    allow_outfit = slot == "0730"

    print("[3/5] Calling Gemini...")
    prompt = build_prompt(slot, date_str, candidates, tiles, prev_message, allow_outfit)
    result, used_model = call_gemini(prompt, api_key, model)
    print(f"  used model: {used_model}")

    print("[4/5] Assembling slot JSON...")
    modules = {}
    for cat in ("markets", "robotics", "tech"):
        picks = result["picks"][cat]
        expected = min(5, len(candidates[cat]))
        if len(picks) != expected:
            raise RuntimeError(f"Gemini returned {len(picks)} picks for {cat}, expected {expected}")
        used_idx = set()
        items = []
        for p in picks:
            idx = p["idx"]
            if idx in used_idx or idx < 0 or idx >= len(candidates[cat]):
                raise RuntimeError(f"Invalid or duplicate idx {idx} in {cat}")
            used_idx.add(idx)
            src = candidates[cat][idx]
            items.append({
                "title": p["title"],
                "teaser": p["teaser"],
                "full": p["full"],
                "think": p["think"],
                "question": p["question"],
                "sources": [{"url": src["link"], "name": src["source"]}],
            })
        modules[cat] = items

    slot_data = {
        "date": date_str,
        "tldr": result["tldr"],
        "indicators": {"note": result["indicators_note"], "tiles": tiles},
        "modules": modules,
    }

    mascot = result["mascot"]
    if allow_outfit:
        new_mascot = {
            "outfit": mascot.get("outfit", []),
            "action": mascot.get("action", "jump"),
            "message": mascot.get("message", ""),
        }
    else:
        new_mascot = {
            "outfit": prev_mascot.get("outfit", []),
            "action": mascot.get("action", "jump"),
            "message": mascot.get("message", ""),
        }

    print("[5/5] Writing index.html...")
    html = replace_slot(html, slot, slot_data)
    html = replace_mascot(html, new_mascot)
    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        f.write(html)

    print("Done.")


if __name__ == "__main__":
    main()
