import requests
import re
import os
import time
from datetime import datetime, timedelta, timezone

DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]

# 監視する店舗リスト（名前とTableCheckのスラグ）
SHOPS = [
    {"name": "静龍苑", "slug": "seiryuen", "lang": "ja"},
]

NUM_GUESTS = 2   # 予約人数
DAYS_AHEAD = 60  # 何日先まで確認するか


def get_session_and_token(shop):
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ja,en;q=0.9",
    })
    url = f"https://www.tablecheck.com/{shop['lang']}/shops/{shop['slug']}/reserve"
    res = session.get(url, timeout=15)
    res.raise_for_status()

    match = re.search(r'<meta[^>]+name="csrf-token"[^>]+content="([^"]+)"', res.text)
    if not match:
        match = re.search(r'name="authenticity_token"[^>]+value="([^"]+)"', res.text)
    if not match:
        raise ValueError(f"{shop['name']}: CSRFトークンが見つかりません")

    return session, match.group(1), url


def check_shop(shop):
    session, token, reserve_url = get_session_and_token(shop)
    available_slots = []
    today = datetime.now(timezone(timedelta(hours=9))).date()  # JST

    for i in range(DAYS_AHEAD):
        date = today + timedelta(days=i)
        date_str = date.strftime("%Y-%m-%d")

        params = {
            "authenticity_token": token,
            "reservation[num_people_adult]": str(NUM_GUESTS),
            "reservation[start_date]": date_str,
        }
        timetable_url = (
            f"https://www.tablecheck.com/{shop['lang']}/shops/{shop['slug']}/available/timetable"
        )
        r = session.get(timetable_url, params=params, timeout=15)
        if r.status_code != 200:
            time.sleep(1)
            continue

        slots = r.json().get("data", {}).get("slots", {}).get(date_str, {})
        for _ts, slot in slots.items():
            if slot.get("available"):
                sec = slot.get("seconds", 0)
                hour = sec // 3600
                minute = (sec % 3600) // 60
                available_slots.append({
                    "shop": shop["name"],
                    "date": date_str,
                    "time": f"{hour:02d}:{minute:02d}",
                    "meal": slot.get("meal", ""),
                    "url": reserve_url,
                })

        time.sleep(0.5)  # サーバー負荷を下げるため少し待つ

    return available_slots


def notify_discord(available_slots):
    # 店舗ごとにまとめる
    by_shop = {}
    for s in available_slots:
        by_shop.setdefault(s["shop"], []).append(s)

    lines = ["🍽️ **Tablecheck 空き枠通知**\n"]
    for shop_name, slots in by_shop.items():
        lines.append(f"**{shop_name}**")
        for s in slots:
            lines.append(f"　{s['date']} {s['time']} ({s['meal']})")
        lines.append(f"　予約はこちら → {slots[0]['url']}\n")

    message = "\n".join(lines)
    # Discordの2000文字制限を考慮
    if len(message) > 1900:
        message = message[:1900] + "\n…（他にも空きあり）"

    res = requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=10)
    res.raise_for_status()


def main():
    all_available = []
    for shop in SHOPS:
        try:
            slots = check_shop(shop)
            all_available.extend(slots)
            print(f"{shop['name']}: {len(slots)} 件の空き枠")
        except Exception as e:
            print(f"{shop['name']}: エラー — {e}")

    if all_available:
        notify_discord(all_available)
        print(f"合計 {len(all_available)} 件の空き枠を通知しました")
    else:
        print("空き枠なし")


if __name__ == "__main__":
    main()
