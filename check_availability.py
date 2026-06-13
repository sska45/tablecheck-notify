import requests
import re
import os
import time
import json
from datetime import datetime, timedelta, timezone

DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]

JST = timezone(timedelta(hours=9))

# 監視する店舗リスト
# widget="v1": 旧ウィジェット（静龍苑など）
# widget="v2": 新ウィジェット（Addなど）
SHOPS = [
    {"name": "静龍苑",   "slug": "seiryuen",            "lang": "ja", "widget": "v1"},
    {"name": "鮨はし本", "slug": "hashimoto-sushi",     "lang": "ja", "widget": "v2"},
    {"name": "Entraide",        "slug": "entraide-kagurazaka", "lang": "ja", "widget": "v1"},
    {"name": "Restaurant Ryuzu", "slug": "restaurant-ryuzu",   "lang": "ja", "widget": "v1"},
]

NUM_GUESTS = 2    # 予約人数
DAYS_AHEAD = 60   # 何日先まで確認するか
COOLDOWN_MIN = 60 # 空き発見後の再チェックスキップ時間（分）
STATE_FILE = "state.json"


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def is_in_cooldown(state, shop_slug):
    last_found = state.get(shop_slug)
    if not last_found:
        return False
    elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last_found)).total_seconds()
    return elapsed < COOLDOWN_MIN * 60


# ── v1ウィジェット（旧Railsアプリ）────────────────────────────────────────

def check_shop_v1(shop):
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept-Language": "ja,en;q=0.9",
    })
    reserve_url = f"https://www.tablecheck.com/{shop['lang']}/shops/{shop['slug']}/reserve"
    res = session.get(reserve_url, timeout=15)
    res.raise_for_status()

    match = re.search(r'<meta[^>]+name="csrf-token"[^>]+content="([^"]+)"', res.text)
    if not match:
        match = re.search(r'name="authenticity_token"[^>]+value="([^"]+)"', res.text)
    if not match:
        raise ValueError(f"{shop['name']}: CSRFトークンが見つかりません")
    token = match.group(1)

    available_slots = []
    today = datetime.now(JST).date()

    for i in range(DAYS_AHEAD):
        date_str = (today + timedelta(days=i)).strftime("%Y-%m-%d")
        params = {
            "authenticity_token": token,
            "reservation[num_people_adult]": str(NUM_GUESTS),
            "reservation[start_date]": date_str,
        }
        r = session.get(
            f"https://www.tablecheck.com/{shop['lang']}/shops/{shop['slug']}/available/timetable",
            params=params, timeout=15,
        )
        if r.status_code != 200:
            time.sleep(1)
            continue

        slots = r.json().get("data", {}).get("slots", {}).get(date_str, {})
        for _ts, slot in slots.items():
            if slot.get("available"):
                sec = slot.get("seconds", 0)
                available_slots.append({
                    "shop": shop["name"],
                    "date": date_str,
                    "time": f"{sec // 3600:02d}:{(sec % 3600) // 60:02d}",
                    "meal": slot.get("meal", ""),
                    "url": reserve_url,
                })
        time.sleep(0.5)

    return available_slots


# ── v2ウィジェット（新React SPA）─────────────────────────────────────────

V2_API = "https://production-booking.tablecheck.com/v2/booking/availability_v5/dates"

def check_shop_v2(shop):
    today = datetime.now(JST).date()
    end_date = today + timedelta(days=DAYS_AHEAD)
    today_str = today.strftime("%Y-%m-%d")
    end_str = end_date.strftime("%Y-%m-%d")
    reserve_url = f"https://www.tablecheck.com/{shop['lang']}/{shop['slug']}/reserve"

    for attempt in range(3):
        r = requests.post(
            V2_API,
            json={
                "shop_id": shop["slug"],
                "start_at": today_str,
                "start_date": today_str,
                "end_date": end_str,
                "pax_adult": NUM_GUESTS,
            },
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": "https://www.tablecheck.com",
                "Referer": "https://www.tablecheck.com/",
            },
            timeout=15,
        )
        if r.status_code == 429:
            time.sleep(10 * (attempt + 1))
            continue
        break
    r.raise_for_status()

    body = r.json().get("availability_dates", {})
    if body.get("code") != "success":
        raise ValueError(f"{shop['name']}: APIエラー — {body.get('message')}")

    available_slots = []
    for date_str, slots in body.get("data", {}).items():
        for slot in slots:
            if slot.get("a"):
                # タイムスタンプ "2026-06-14T11:00:00Z" → JST変換
                t = datetime.fromisoformat(slot["t"].replace("Z", "+00:00")).astimezone(JST)
                available_slots.append({
                    "shop": shop["name"],
                    "date": date_str,
                    "time": t.strftime("%H:%M"),
                    "meal": "",
                    "url": reserve_url,
                })

    return available_slots


# ── 通知・メイン ────────────────────────────────────────────────────────────

def notify_discord(available_slots):
    sorted_slots = sorted(available_slots, key=lambda s: (s["date"], s["time"]))[:3]

    lines = ["@everyone 🍽️ **Tablecheck 空き枠通知**\n"]
    for s in sorted_slots:
        meal = f" ({s['meal']})" if s["meal"] else ""
        lines.append(f"**{s['shop']}**　{s['date']} {s['time']}{meal}")
        lines.append(f"　→ {s['url']}\n")

    message = "\n".join(lines)
    if len(message) > 1900:
        message = message[:1900] + "\n…（他にも空きあり）"

    res = requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=10)
    res.raise_for_status()


def main():
    state = load_state()
    all_available = []

    for shop in SHOPS:
        if is_in_cooldown(state, shop["slug"]):
            print(f"{shop['name']}: クールダウン中のためスキップ")
            continue
        try:
            if shop.get("widget") == "v2":
                slots = check_shop_v2(shop)
            else:
                slots = check_shop_v1(shop)
            print(f"{shop['name']}: {len(slots)} 件の空き枠")
            if slots:
                state[shop["slug"]] = datetime.now(timezone.utc).isoformat()
            all_available.extend(slots)
        except Exception as e:
            print(f"{shop['name']}: エラー — {e}")

    save_state(state)

    if all_available:
        notify_discord(all_available)
        print(f"合計 {len(all_available)} 件の空き枠を通知しました")
    else:
        print("空き枠なし")


if __name__ == "__main__":
    main()
