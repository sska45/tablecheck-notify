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
    {"name": "食堂みかん",   "slug": "shokudo-mikan",       "lang": "ja", "widget": "v1"},
    {"name": "横浜mican",    "slug": "yokohama-mican",      "lang": "ja", "widget": "v2", "max_start_time": "19:30"},
]

NUM_GUESTS = 2    # 予約人数
DAYS_AHEAD = 60   # 何日先まで確認するか
SLOT_COOLDOWN_MIN = 60 * 24  # 同じ枠を再通知しないスキップ時間（分）
ERROR_COOLDOWN_MIN = 60 * 24  # エラー通知の再送スキップ時間（分）
STATE_FILE = "state.json"


def slot_key(slug, slot):
    """枠を一意に識別するキー（店slug＋日付＋時刻）"""
    return f"slot:{slug}|{slot['date']}|{slot['time']}"


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def is_in_cooldown(state, key, minutes):
    last = state.get(key)
    if not last:
        return False
    elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds()
    return elapsed < minutes * 60


def prune_state(state):
    """クールダウンを過ぎた枠の記録を削除して state.json の肥大化を防ぐ"""
    now = datetime.now(timezone.utc)
    for k in list(state.keys()):
        if not k.startswith("slot:"):
            continue
        try:
            elapsed = (now - datetime.fromisoformat(state[k])).total_seconds()
        except (ValueError, TypeError):
            del state[k]
            continue
        if elapsed >= SLOT_COOLDOWN_MIN * 60:
            del state[k]


# ── v1ウィジェット（旧Railsアプリ）────────────────────────────────────────

def check_shop_v1(shop):
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept-Language": "ja,en;q=0.9",
    })
    reserve_url = f"https://www.tablecheck.com/{shop['lang']}/shops/{shop['slug']}/reserve"
    for attempt in range(3):
        res = session.get(reserve_url, timeout=15)
        if res.status_code == 429:
            time.sleep(15 * (attempt + 1))
            continue
        break
    res.raise_for_status()

    match = re.search(r'<meta[^>]+name="csrf-token"[^>]+content="([^"]+)"', res.text)
    if not match:
        match = re.search(r'name="authenticity_token"[^>]+value="([^"]+)"', res.text)
    if not match:
        raise ValueError(f"{shop['name']}: CSRFトークンが見つかりません")
    token = match.group(1)

    available_slots = []
    today = datetime.now(JST).date()
    end_date = today + timedelta(days=DAYS_AHEAD)
    seen_dates = set()

    # 1リクエストで約5日分返るので5日刻みで叩く（60→12リクエスト）
    for i in range(0, DAYS_AHEAD, 5):
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
        if r.status_code == 429:
            time.sleep(15)
            continue
        if r.status_code != 200:
            time.sleep(1)
            continue

        all_date_slots = r.json().get("data", {}).get("slots", {})
        for d_str, day_slots in all_date_slots.items():
            if d_str in seen_dates:
                continue
            if d_str < today.strftime("%Y-%m-%d") or d_str >= end_date.strftime("%Y-%m-%d"):
                continue
            seen_dates.add(d_str)
            max_sec = shop.get("max_start_time")  # 例: "20:00" → 72000秒
            if max_sec:
                h, m = map(int, max_sec.split(":"))
                max_sec = h * 3600 + m * 60
            for _ts, slot in day_slots.items():
                if slot.get("available"):
                    sec = slot.get("seconds", 0)
                    if max_sec is not None and sec > max_sec:
                        continue
                    available_slots.append({
                    "shop": shop["name"],
                    "date": d_str,
                    "time": f"{sec // 3600:02d}:{(sec % 3600) // 60:02d}",
                    "meal": slot.get("meal", ""),
                    "url": reserve_url,
                })
        time.sleep(1)

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

    max_start_time = shop.get("max_start_time")  # 例: "19:30"
    available_slots = []
    for date_str, slots in body.get("data", {}).items():
        for slot in slots:
            if slot.get("a"):
                # タイムスタンプ "2026-06-14T11:00:00Z" → JST変換
                t = datetime.fromisoformat(slot["t"].replace("Z", "+00:00")).astimezone(JST)
                time_str = t.strftime("%H:%M")
                if max_start_time is not None and time_str > max_start_time:
                    continue
                available_slots.append({
                    "shop": shop["name"],
                    "date": date_str,
                    "time": time_str,
                    "meal": "",
                    "url": reserve_url,
                })

    return available_slots


# ── 通知・メイン ────────────────────────────────────────────────────────────

def notify_discord(available_slots):
    # 店舗ごとにグループ化して個別送信
    by_shop = {}
    for s in available_slots:
        by_shop.setdefault(s["shop"], []).append(s)

    displayed = []
    for shop_name, slots in by_shop.items():
        top3 = sorted(slots, key=lambda s: (s["date"], s["time"]))[:3]
        lines = [f"@everyone 🍽️ **Tablecheck 空き枠通知**\n"]
        for s in top3:
            meal = f" ({s['meal']})" if s["meal"] else ""
            lines.append(f"**{s['shop']}**　{s['date']} {s['time']}{meal}")
        lines.append(f"\n→ {top3[0]['url']}")

        message = "\n".join(lines)
        if len(message) > 1900:
            message = message[:1900] + "\n…（他にも空きあり）"

        res = requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=10)
        res.raise_for_status()
        displayed.extend(top3)

    return displayed


def notify_discord_error():
    message = "⚠️ アクセス制限で最新の空き情報が確認できていない状況が発生しています。"
    res = requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=10)
    res.raise_for_status()


def main():
    state = load_state()
    candidates = []      # 未通知（またはクールダウン切れ）の枠だけを集める
    has_rate_limit_error = False

    for shop in SHOPS:
        try:
            if shop.get("widget") == "v2":
                slots = check_shop_v2(shop)
            else:
                slots = check_shop_v1(shop)
            print(f"{shop['name']}: {len(slots)} 件の空き枠")
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 429:
                has_rate_limit_error = True
            print(f"{shop['name']}: エラー — {e}")
            continue
        except Exception as e:
            print(f"{shop['name']}: エラー — {e}")
            continue

        # 枠単位でクールダウン判定（24時間以内に通知済みの枠は除外）
        for slot in slots:
            if is_in_cooldown(state, slot_key(shop["slug"], slot), SLOT_COOLDOWN_MIN):
                continue
            slot["_slug"] = shop["slug"]
            candidates.append(slot)

    if has_rate_limit_error and not is_in_cooldown(state, "_rate_limit_error", ERROR_COOLDOWN_MIN):
        try:
            notify_discord_error()
            state["_rate_limit_error"] = datetime.now(timezone.utc).isoformat()
            print("レート制限エラーをDiscordに通知しました")
        except Exception as e:
            print(f"エラー通知の送信に失敗: {e}")

    if candidates:
        notified = notify_discord(candidates)
        now = datetime.now(timezone.utc).isoformat()
        for slot in notified:
            state[slot_key(slot["_slug"], slot)] = now
        print(f"合計 {len(notified)} 件の空き枠を通知しました（新規枠 {len(candidates)} 件検出）")
    else:
        print("新規の空き枠なし")

    prune_state(state)
    save_state(state)


if __name__ == "__main__":
    main()
