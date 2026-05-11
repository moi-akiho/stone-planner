"""
rhinestone.py — プレシオサ Rose MAXIMA フラットバック 最安購入プランナー
対象サイト：
  ① OFFICE-K    onocoltd.jp
  ② つくろう     tsukuro.com     (EUC-JP / クーポン tsukuro5off)
  ③ クリスタルプロ crystal-pro.com
"""

import argparse
import csv
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup

# ─────────────────────────────────────────
# 設定
# ─────────────────────────────────────────
WATCHLIST_FILE = Path(__file__).parent / "watchlist.json"

SHIPPING = {
    "onocoltd":    {"free_threshold": 1000,  "cost": 550},
    "tsukuro":     {"free_threshold": 4000,  "cost": 400},
    "crystal_pro": {"free_threshold": 10000, "cost": 370},
}

# 送料無料ラインが未確認のOFFICE-Kは実測値で仮置き（550円）
# → 実際のサイトで確認後に上書きすること

GROSS_PACK = {"SS12": 1440, "SS16": 1440, "SS20": 1440, "SS30": 288}

COUPON_TSUKURO = {"code": "tsukuro5off", "rate": 0.05, "days": [5, 15, 25]}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

# ─────────────────────────────────────────
# ユーティリティ
# ─────────────────────────────────────────

def fetch(url: str, encoding: Optional[str] = None, timeout: int = 15) -> Optional[BeautifulSoup]:
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        if encoding:
            r.encoding = encoding
        r.raise_for_status()
        return BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        print(f"  [取得失敗] {url} — {e}", file=sys.stderr)
        return None


def price_int(text: str) -> Optional[int]:
    """「1,234円」→ 1234"""
    m = re.search(r"[\d,]+", text.replace("，", ","))
    if m:
        return int(m.group().replace(",", ""))
    return None


def coupon_active() -> bool:
    return datetime.today().day in COUPON_TSUKURO["days"]


def apply_coupon(price: int) -> int:
    return int(price * (1 - COUPON_TSUKURO["rate"]))


# ─────────────────────────────────────────
# スクレイパー
# ─────────────────────────────────────────

def scrape_onocoltd(color: str, size: str) -> dict:
    """
    OFFICE-K (onocoltd.jp)
    検索URL例: https://onocoltd.jp/search?q=プレシオサ+MAXIMA+SS20+ジェット
    """
    query = f"プレシオサ MAXIMA フラットバック {size} {color}"
    url = f"https://onocoltd.jp/search?q={quote(query)}"
    soup = fetch(url)
    result = {"site": "OFFICE-K", "site_id": "onocoltd", "color": color, "size": size,
              "price": None, "in_stock": False, "url": url}
    if not soup:
        return result

    # 商品カード（Shopifyテーマ想定）
    cards = soup.select(".product-item, .grid-product, .product-card, article.product")
    if not cards:
        # フォールバック: 価格テキストを直接探す
        cards = soup.select("[class*='product']")

    for card in cards:
        name_el = card.select_one("[class*='title'], h2, h3, .product-name")
        if not name_el:
            continue
        name = name_el.get_text()
        if size.upper() not in name.upper() and size.lower() not in name.lower():
            continue

        price_el = card.select_one("[class*='price']")
        if price_el:
            p = price_int(price_el.get_text())
            if p:
                result["price"] = p

        link_el = card.select_one("a[href]")
        if link_el:
            result["url"] = urljoin("https://onocoltd.jp", link_el["href"])

        btn = card.select_one("button[name='add'], .add-to-cart, [class*='cart']")
        result["in_stock"] = btn is not None and "disabled" not in btn.get("class", [])
        break

    return result


def scrape_tsukuro(color: str, size: str) -> dict:
    """
    つくろう (tsukuro.com) — EUC-JP
    """
    query = f"プレシオサ ローザ MAXIMA {size} {color}"
    url = f"https://tsukuro.com/search.php?q={quote(query, encoding='euc-jp', errors='replace')}"
    soup = fetch(url, encoding="euc-jp")
    result = {"site": "つくろう", "site_id": "tsukuro", "color": color, "size": size,
              "price": None, "in_stock": False, "url": url, "coupon_applied": False}
    if not soup:
        return result

    items = soup.select(".product_item, .item, li.goods_list_item, .goods-item")
    for item in items:
        name_el = item.select_one(".goods_name, .product-name, h3, h4, .name")
        if not name_el:
            continue
        name = name_el.get_text()
        if size.upper() not in name.upper():
            continue

        price_el = item.select_one(".price, .goods_price, [class*='price']")
        if price_el:
            p = price_int(price_el.get_text())
            if p:
                result["price"] = p
                if coupon_active():
                    result["price"] = apply_coupon(p)
                    result["coupon_applied"] = True

        link_el = item.select_one("a[href]")
        if link_el:
            result["url"] = urljoin("https://tsukuro.com", link_el["href"])

        # カートボタンの状態で在庫判定（「在庫なし表示でも入れられる」ケースあり）
        cart_btn = item.select_one("input[type='submit'], button[type='submit'], .cart-btn")
        if cart_btn:
            disabled = cart_btn.get("disabled") or "disabled" in str(cart_btn.get("class", ""))
            result["in_stock"] = not disabled
        break

    return result


def scrape_crystal_pro(color: str, size: str) -> dict:
    """
    クリスタルプロ (crystal-pro.com)
    """
    query = f"プレシオサ MAXIMA フラットバック {size} {color}"
    url = f"https://crystal-pro.com/search?q={quote(query)}"
    soup = fetch(url)
    result = {"site": "クリスタルプロ", "site_id": "crystal_pro", "color": color, "size": size,
              "price": None, "in_stock": False, "url": url}
    if not soup:
        return result

    items = soup.select(".product-item, .product-card, article, .grid__item")
    for item in items:
        name_el = item.select_one("h2, h3, .product-title, [class*='title']")
        if not name_el:
            continue
        name = name_el.get_text()
        if size.upper() not in name.upper():
            continue

        price_el = item.select_one("[class*='price']")
        if price_el:
            p = price_int(price_el.get_text())
            if p:
                result["price"] = p

        link_el = item.select_one("a[href]")
        if link_el:
            result["url"] = urljoin("https://crystal-pro.com", link_el["href"])

        btn = item.select_one("button, input[type='submit']")
        result["in_stock"] = btn is not None and btn.get("disabled") is None
        break

    return result


# ─────────────────────────────────────────
# 並列スクレイピング
# ─────────────────────────────────────────

def fetch_all(color: str, size: str) -> list[dict]:
    scrapers = [scrape_onocoltd, scrape_tsukuro, scrape_crystal_pro]
    results = []
    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = {ex.submit(fn, color, size): fn.__name__ for fn in scrapers}
        for f in as_completed(futures):
            try:
                results.append(f.result())
            except Exception as e:
                print(f"  [エラー] {futures[f]}: {e}", file=sys.stderr)
    return results


# ─────────────────────────────────────────
# 購入プラン計算
# ─────────────────────────────────────────

def calc_plan(items: list[dict], packs: int) -> dict:
    """
    items: fetch_all() の結果リスト（複数サイズ・カラー混在可）
    packs: 各アイテムの購入パック数
    → 送料込み最安プランを計算
    """
    # サイトごとに購入金額を合算
    site_totals = {"onocoltd": 0, "tsukuro": 0, "crystal_pro": 0}
    chosen = []

    for item in items:
        if not item["price"] or not item["in_stock"]:
            continue
        sid = item["site_id"]
        total_item = item["price"] * packs
        site_totals[sid] += total_item
        chosen.append({**item, "packs": packs, "subtotal": total_item})

    # 送料計算
    plan = {}
    grand_total = 0
    for sid, sub in site_totals.items():
        if sub == 0:
            continue
        s = SHIPPING[sid]
        shipping = 0 if sub >= s["free_threshold"] else s["cost"]
        shortage = max(0, s["free_threshold"] - sub) if shipping > 0 else 0
        plan[sid] = {
            "subtotal": sub,
            "shipping": shipping,
            "total": sub + shipping,
            "free_shortage": shortage,
        }
        grand_total += sub + shipping

    return {"items": chosen, "site_plan": plan, "grand_total": grand_total}


# ─────────────────────────────────────────
# 表示
# ─────────────────────────────────────────

def print_results(results: list[dict], packs: int):
    print("\n" + "=" * 60)
    print("  ラインストーン価格チェック結果")
    print("=" * 60)

    for r in results:
        stock_mark = "✓ 在庫あり" if r["in_stock"] else "✗ 在庫なし"
        price_str = f"¥{r['price']:,}" if r["price"] else "価格不明"
        coupon = " [クーポン5%OFF適用]" if r.get("coupon_applied") else ""
        print(f"\n[{r['site']}] {r['color']} {r['size']}")
        print(f"  価格   : {price_str}{coupon}")
        print(f"  在庫   : {stock_mark}")
        print(f"  URL    : {r['url']}")

    print()


def print_plan(plan: dict):
    print("=" * 60)
    print("  最安購入プラン")
    print("=" * 60)
    for sid, p in plan["site_plan"].items():
        site_name = {"onocoltd": "OFFICE-K", "tsukuro": "つくろう", "crystal_pro": "クリスタルプロ"}[sid]
        print(f"\n【{site_name}】")
        print(f"  小計   : ¥{p['subtotal']:,}")
        if p["shipping"] == 0:
            print(f"  送料   : 無料")
        else:
            print(f"  送料   : ¥{p['shipping']:,}  ← あと ¥{p['free_shortage']:,} で送料無料")
        print(f"  合計   : ¥{p['total']:,}")

    print(f"\n  ━━ 総合計 : ¥{plan['grand_total']:,} ━━")
    print()


def print_no_stock(results: list[dict]):
    no_stock = [r for r in results if not r["in_stock"]]
    if no_stock:
        print("在庫なし商品:")
        for r in no_stock:
            print(f"  - [{r['site']}] {r['color']} {r['size']}")


# ─────────────────────────────────────────
# 監視リスト（再入荷通知）
# ─────────────────────────────────────────

def load_watchlist() -> list[dict]:
    if WATCHLIST_FILE.exists():
        return json.loads(WATCHLIST_FILE.read_text(encoding="utf-8"))
    return []


def save_watchlist(wl: list[dict]):
    WATCHLIST_FILE.write_text(json.dumps(wl, ensure_ascii=False, indent=2), encoding="utf-8")


def add_to_watchlist(item: dict):
    wl = load_watchlist()
    key = (item["site_id"], item["color"], item["size"])
    if not any((w["site_id"], w["color"], w["size"]) == key for w in wl):
        wl.append({
            "site_id": item["site_id"],
            "site": item["site"],
            "color": item["color"],
            "size": item["size"],
            "url": item["url"],
            "added": datetime.now().isoformat(timespec="seconds"),
        })
        save_watchlist(wl)
        print(f"  → 監視リストに登録しました: [{item['site']}] {item['color']} {item['size']}")
    else:
        print(f"  → 既に監視リストに登録済みです")


def notify_line(message: str, token: str):
    """LINE Notify でメッセージ送信"""
    try:
        requests.post(
            "https://notify-api.line.me/api/notify",
            headers={"Authorization": f"Bearer {token}"},
            data={"message": message},
            timeout=10,
        )
    except Exception as e:
        print(f"  [LINE通知失敗] {e}", file=sys.stderr)


def watch_mode(line_token: Optional[str] = None):
    """監視リストをチェックして在庫復活を通知"""
    wl = load_watchlist()
    if not wl:
        print("監視リストは空です。")
        return

    print(f"監視リスト: {len(wl)} 件をチェック中...")
    restocked = []
    for w in wl:
        scrapers = {
            "onocoltd": scrape_onocoltd,
            "tsukuro": scrape_tsukuro,
            "crystal_pro": scrape_crystal_pro,
        }
        fn = scrapers.get(w["site_id"])
        if not fn:
            continue
        r = fn(w["color"], w["size"])
        if r["in_stock"]:
            restocked.append(r)
            print(f"  ✓ 入荷検知！ [{r['site']}] {r['color']} {r['size']} ¥{r['price']:,}")

    if restocked and line_token:
        msg = "\n\n".join([
            f"【入荷通知】\n商品: {r['color']} {r['size']}\nサイト: {r['site']}\n"
            f"価格: ¥{r['price']:,}\nURL: {r['url']}"
            for r in restocked
        ])
        notify_line(f"\n{msg}", line_token)
        print("  → LINE通知を送りました")
    elif not restocked:
        print("  入荷なし（全商品まだ在庫切れ）")


# ─────────────────────────────────────────
# CSV出力
# ─────────────────────────────────────────

def export_csv(results: list[dict], filepath: str = "rhinestone_result.csv"):
    with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["site", "color", "size", "price", "in_stock", "url"])
        writer.writeheader()
        for r in results:
            writer.writerow({
                "site": r["site"],
                "color": r["color"],
                "size": r["size"],
                "price": r.get("price", ""),
                "in_stock": "在庫あり" if r["in_stock"] else "在庫なし",
                "url": r["url"],
            })
    print(f"  → CSV出力: {filepath}")


# ─────────────────────────────────────────
# メイン
# ─────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="プレシオサ Rose MAXIMA 最安購入プランナー"
    )
    parser.add_argument("--color", "-c", help="カラー名（例: ジェット）")
    parser.add_argument(
        "--size", "-s",
        choices=["SS12", "SS16", "SS20", "SS30"],
        help="サイズ",
    )
    parser.add_argument("--packs", "-p", type=int, default=1, help="購入パック数（デフォルト1）")
    parser.add_argument("--watch", action="store_true", help="監視リストをチェックして通知")
    parser.add_argument("--csv", action="store_true", help="結果をCSV出力")
    parser.add_argument("--line-token", help="LINE Notify トークン（再入荷通知用）")
    parser.add_argument("--watchlist", action="store_true", help="現在の監視リストを表示")
    args = parser.parse_args()

    # 監視リスト表示
    if args.watchlist:
        wl = load_watchlist()
        if not wl:
            print("監視リストは空です。")
        else:
            print(f"監視リスト ({len(wl)} 件):")
            for w in wl:
                print(f"  [{w['site']}] {w['color']} {w['size']}  登録日: {w['added']}")
        return

    # 監視モード
    if args.watch:
        watch_mode(line_token=args.line_token)
        return

    # 通常モード（価格チェック）
    if not args.color or not args.size:
        parser.error("--color と --size を指定してください（例: --color ジェット --size SS20）")

    print(f"\n検索中: {args.color} {args.size}  {args.packs}パック")
    if coupon_active():
        print(f"  ★ 今日はつくろうクーポン({COUPON_TSUKURO['code']})が使える日です！（5%OFF）")
    else:
        next_days = [d for d in COUPON_TSUKURO["days"] if d > datetime.today().day]
        next_day = next_days[0] if next_days else COUPON_TSUKURO["days"][0]
        print(f"  （つくろうクーポン適用日: 毎月5・15・25日 / 次回: {next_day}日）")

    results = fetch_all(args.color, args.size)

    print_results(results, args.packs)

    in_stock = [r for r in results if r["in_stock"] and r["price"]]
    if in_stock:
        plan = calc_plan(in_stock, args.packs)
        print_plan(plan)
    else:
        print("在庫のある商品が見つかりませんでした。\n")

    # 在庫なし → 監視リスト登録を提案
    no_stock = [r for r in results if not r["in_stock"]]
    for r in no_stock:
        ans = input(f"[{r['site']}] {r['color']} {r['size']} — 再入荷通知を希望しますか？ (y/n): ").strip().lower()
        if ans == "y":
            add_to_watchlist(r)

    # CSV出力
    if args.csv:
        export_csv(results)

    # グロスパック情報
    units = GROSS_PACK.get(args.size, "?")
    print(f"\n参考: {args.size} グロスパック = {units}個/パック")


if __name__ == "__main__":
    main()
