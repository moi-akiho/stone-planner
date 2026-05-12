"""
rhinestone.py — プレシオサ Rose MAXIMA フラットバック 最安購入プランナー

サイト別スクレイピング方式（実際のHTML構造に基づく）:
  ① OFFICE-K            www.onocoltd.jp       /product-list/3531 → 商品ページ
  ② つくろ！ドットコム   www.tsukuro.com       POST検索 + shopdetail + 価格API
  ③ デコダリア           www.san-ai-flowers.jp  Shopify JSON API
  ④ チャーミーマーケット 楽天市場              静的価格テーブル（スクレイピング不可）
"""

import csv
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, quote

import requests
from bs4 import BeautifulSoup

# ─────────────────────────────────────────
# 設定
# ─────────────────────────────────────────
WATCHLIST_FILE = Path(__file__).parent / "watchlist.json"

SHIPPING = {
    "onocoltd":      {"free_threshold": 1000, "cost": 550},
    "tsukuro":       {"free_threshold": 4000, "cost": 400},
    "deco_dahlia":   {"free_threshold": 1980, "cost": 120},   # ネコポス
    "charmy_market": {"free_threshold": 0,    "cost": 0},     # 常時送料無料
}

GROSS_PACK = {"SS12": 1440, "SS16": 1440, "SS20": 1440, "SS30": 288}

COUPON_TSUKURO = {"code": "tsukuro5off", "rate": 0.05, "days": [5, 15, 25]}

# ─── チャーミーマーケット 静的価格テーブル ───────────────────
# 楽天市場。スクレイピング不可のため手動入力価格を使用。
# カラー区分: クリスタル / クリスタルAB / カラー（その他全色）
CHARMY_PRICES = {
    "SS12": {"クリスタル": 4300, "クリスタルAB": 5800, "カラー": 5200},
    "SS16": {"クリスタル": 5700, "クリスタルAB": 7800, "カラー": 6700},
    "SS20": {"クリスタル": 7700, "クリスタルAB": 10600, "カラー": 9200},
    "SS30": {"クリスタル": 3400, "クリスタルAB": 4600, "カラー": 4200},
}

# 色名 → カテゴリ（クリスタル / クリスタルAB / カラー）
CHARMY_COLOR_CATEGORY: dict[str, str] = {
    "クリスタル":       "クリスタル",
    "クリスタルAB":     "クリスタルAB",
    "クリスタルオーロラ": "クリスタルAB",  # 別表記
    # ── カラー ──────────────────────────────
    "ブラックダイア":         "カラー",
    "ブラックダイヤモンド":   "カラー",   # 表記ゆれ対応
    "ジェット":               "カラー",
    "ホワイトオパール":       "カラー",
    "ジョンキル":             "カラー",
    "ライトトパーズ":         "カラー",
    "シトリン":               "カラー",
    "ライトコロラドトパーズ": "カラー",
    "トパーズ":               "カラー",
    "ライトピーチ":           "カラー",
    "ヴィンテージローズ":     "カラー",
    "ライトローズ":           "カラー",
    "ローズ":                 "カラー",
    "ライトアメジスト":       "カラー",
    "ペールライラック":       "カラー",
    "アクアボヘミカ":         "カラー",
    "アメジスト":             "カラー",
    "フューシャ":             "カラー",
    "ライトシャム":           "カラー",
    "タンザナイト":           "カラー",
    "アクアマリン":           "カラー",
    "ライトサファイア":       "カラー",
    "サファイア":             "カラー",
    "カプリブルー":           "カラー",
    "ブルージルコン":         "カラー",
    "エメラルド":             "カラー",
    "オリバイン":             "カラー",
    "ペリドット":             "カラー",
    "シャムロック":           "カラー",
    "サン":                   "カラー",
    "ヒヤシンス":             "カラー",
    "シャム":                 "カラー",
    "スモークトパーズ":       "カラー",
    "モンタナ":               "カラー",
    "レッドベルベット":       "カラー",
    "ディープシー":           "カラー",
}

# 5万円以上購入で15%OFFクーポン
CHARMY_COUPON_THRESHOLD = 50_000
CHARMY_COUPON_RATE = 0.15

# チャーミーマーケット 楽天店 URL（トップページ）
CHARMY_STORE_URL = "https://item.rakuten.co.jp/charmymarket/"  # ← 要確認

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

# デコダリア (san-ai-flowers.jp) の色名→ハンドル マッピング
# URL: /products/prs-{handle}
DECO_DAHLIA_COLOR_MAP = {
    "ジェット":             "jet",
    "クリスタル":           "crystal",
    "クリスタルオーロラ":   "crystal-ab",
    "ローズ":               "rose",
    "ライトローズ":         "light-rose",
    "アメジスト":           "amethyst",
    "ブラックダイヤモンド": "black-diamond",
    "ライトコロラドトパーズ": "light-colorado-topaz",
    "スモークトパーズ":     "smoke-topaz",
    "エメラルド":           "emerald",
    "ライトサファイア":     "light-sapphire",
    "ヘマタイト":           "hematite",
    "ジェットヘマタイト":   "jet-hematite",
    "ライトアメジスト":     "light-amethyst",
}

# crystal-pro.com の色名→URLコード マッピング（追加可）
CRYSTAL_PRO_COLOR_MAP = {
    "ジェット": "Jet",
    "クリスタル": "Crystal",
    "クリスタルオーロラ": "CrystalAB",
    "ブラックダイヤモンド": "BlackDiamond",
    "ライトコロラドトパーズ": "LightColoradoTopaz",
    "ライトローズ": "LightRose",
    "ローズ": "Rose",
    "アメジスト": "Amethyst",
    "スモークトパーズ": "SmokeTopaz",
    "エメラルド": "Emerald",
    "ライトサファイア": "LightSapphire",
    "ヘマタイト": "Hematite",
    "ジェットヘマタイト": "JetHematite",
    "ライトアメジスト": "LightAmethyst",
}

# ─────────────────────────────────────────
# ユーティリティ
# ─────────────────────────────────────────

def fetch_html(url: str, encoding: Optional[str] = None,
               method: str = "GET", data=None, timeout: int = 15) -> Optional[BeautifulSoup]:
    try:
        if method == "POST":
            r = requests.post(url, headers=HEADERS, data=data, timeout=timeout)
        else:
            r = requests.get(url, headers=HEADERS, timeout=timeout)
        if encoding:
            r.encoding = encoding
        else:
            r.encoding = r.apparent_encoding or "utf-8"
        r.raise_for_status()
        return BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        print(f"  [取得失敗] {url} — {e}", file=sys.stderr)
        return None


def price_int(text: str) -> Optional[int]:
    m = re.search(r"[\d,]+", text.replace("，", ","))
    if m:
        return int(m.group().replace(",", ""))
    return None


def coupon_active() -> bool:
    return datetime.today().day in COUPON_TSUKURO["days"]


def apply_coupon(price: int) -> int:
    return int(price * (1 - COUPON_TSUKURO["rate"]))


# ─────────────────────────────────────────
# ① OFFICE-K スクレイパー
# ─────────────────────────────────────────

def scrape_onocoltd(color: str, size: str) -> dict:
    """
    1. /product-list/3531 から色名でURLを探す
    2. 商品ページの variation_stock_list テーブルで size を確認
    """
    base_url = "https://www.onocoltd.jp"
    result = {
        "site": "OFFICE-K", "site_id": "onocoltd",
        "color": color, "size": size,
        "price": None, "in_stock": False,
        "url": f"{base_url}/product-list/3531",
        "note": "",
    }

    # カテゴリページから色→URLを取得
    soup = fetch_html(f"{base_url}/product-list/3531")
    if not soup:
        return result

    product_url = None
    for a in soup.select('a[href*="/product/"]'):
        name = a.get_text(strip=True)
        if color in name:
            href = a.get("href", "")
            product_url = href if href.startswith("http") else urljoin(base_url, href)
            break

    if not product_url:
        result["note"] = "取り扱いなし"
        return result

    result["url"] = product_url

    # 商品詳細ページ
    soup = fetch_html(product_url)
    if not soup:
        return result

    # JavaScriptから priceArray を取得（variation_id → 価格）
    price_array = {}
    for script in soup.find_all("script"):
        txt = script.string or ""
        if "priceArray" not in txt:
            continue
        for m in re.finditer(r"pConf\.priceArray\[1\]\[(\d+)\]\s*=\s*(\d+)", txt):
            price_array[m.group(1)] = int(m.group(2))

    # variation_stock_list テーブルでサイズを確認
    stock_table = soup.find(class_="variation_stock_list")
    if not stock_table:
        result["note"] = "サイズ情報取得不可"
        return result

    size_found = False
    for row in stock_table.find_all("tr"):
        th = row.find("th")
        if not th:
            continue
        row_size = th.get_text(strip=True)
        if size.upper() in row_size.upper():
            size_found = True
            radio = row.find("input", {"type": "radio"})
            result["in_stock"] = radio is not None
            if radio:
                # radioのvalue例: "195v1120" → variation_id="1120"
                var_m = re.search(r"v(\d+)$", radio.get("value", ""))
                if var_m and var_m.group(1) in price_array:
                    result["price"] = price_array[var_m.group(1)]
            else:
                result["note"] = "在庫なし（売り切れ）"
            break

    if not size_found:
        result["in_stock"] = False
        result["note"] = "このサイズは取り扱いなし"

    return result


# ─────────────────────────────────────────
# ② つくろう スクレイパー
# ─────────────────────────────────────────

def scrape_tsukuro(color: str, size: str) -> dict:
    """
    1. POST検索で「ラインストーン {color}」→ ラインストーンMAXIMAの brandcode を特定
    2. /shopdetail/{brandcode}/ の stockList テーブルからグロスパックの在庫確認
    3. /shop/shopdetail_option.html で価格を取得
    """
    base_url = "https://www.tsukuro.com"
    result = {
        "site": "つくろ！ドットコム", "site_id": "tsukuro",
        "color": color, "size": size,
        "price": None, "in_stock": False,
        "url": base_url,
        "coupon_applied": False,
        "note": "",
    }

    # 検索
    query = f"ラインストーン {color}"
    soup = fetch_html(
        f"{base_url}/shop/shopbrand.html",
        encoding="euc-jp",
        method="POST",
        data={"search": query.encode("euc-jp", errors="replace")},
    )
    if not soup:
        return result

    # ラインストーンMAXIMA/{color} のリンクを探す
    brandcode = None
    product_url = None
    target_name = f"ラインストーンMAXIMA/{color}"
    for a in soup.select('a[href*="shopdetail"]'):
        name = a.get_text(strip=True)
        # 末尾が /{color} で終わるものだけマッチ（ジェットブラウンフレアなどを除外）
        ends_with_color = name.endswith(f"/{color}") or name.endswith(f"/{color}/グロスパック")
        if "ラインストーンMAXIMA" in name and ends_with_color and "ホットフィックス" not in name:
            href = a.get("href", "")
            # brandcodeを取得
            m = re.search(r"brandcode=(\d+)", href)
            if m:
                brandcode = m.group(1)
                canonical = f"{base_url}/shopdetail/{brandcode}/"
                product_url = canonical
                break

    if not brandcode:
        result["note"] = "取り扱いなし"
        return result

    result["url"] = product_url

    # 商品ページ取得
    soup = fetch_html(product_url, encoding="euc-jp")
    if not soup:
        return result

    # stockList テーブルからサイズ行を探す
    stock_table = soup.find(class_="stockList")
    if not stock_table:
        result["note"] = "在庫テーブル取得不可"
        return result

    option1_id = None
    option2_id = None  # 3 = グロスパック

    for row in stock_table.find_all("tr"):
        th = row.find("th", class_="leftLine")
        if not th:
            continue
        row_size = th.get_text(strip=True).upper()
        if size.upper() != row_size:
            continue

        # カラムを取得（小袋=td[0], グロスパック=td[1]）
        tds = row.find_all("td")
        if len(tds) < 2:
            continue
        gross_td = tds[1]  # グロスパック列

        # 在庫ステータスを確認
        instock_el = gross_td.find(class_=re.compile(r"M_select-option-(instock|smallstock)"))
        soldout_el = gross_td.find(class_=re.compile(r"M_select-option-soldout"))

        if soldout_el and not instock_el:
            result["in_stock"] = False
            result["note"] = "在庫なし（売り切れ）"
        elif instock_el:
            result["in_stock"] = True
            # radioのvalueからoption idを取得（例: "4_3"）
            radio = gross_td.find("input", {"type": "radio"})
            if radio:
                val = radio.get("value", "")
                parts = val.split("_")
                if len(parts) == 2:
                    option1_id = parts[0]
                    option2_id = parts[1]
        break
    else:
        result["note"] = "このサイズは取り扱いなし"
        return result

    # 価格をAJAX APIで取得
    if result["in_stock"] and option1_id and option2_id:
        uid = str(int(brandcode))  # 先頭0を除去
        try:
            pr = requests.post(
                f"{base_url}/shop/shopdetail_option.html",
                headers={**HEADERS,
                         "Content-Type": "application/x-www-form-urlencoded",
                         "Referer": product_url},
                data=f"uid={uid}&option1_id={option1_id}&option2_id={option2_id}",
                timeout=10,
            )
            price_data = pr.json()
            price_raw = int(re.sub(r"[^\d]", "", str(price_data.get("price", "0"))))
            if price_raw > 0:
                result["price_base"] = price_raw
                result["price_discounted"] = apply_coupon(price_raw)
                result["coupon_applied"] = coupon_active()
                result["price"] = result["price_discounted"] if result["coupon_applied"] else price_raw
        except Exception as e:
            pass  # 価格取得失敗でも在庫情報は返す

    return result


# ─────────────────────────────────────────
# ③ デコダリア スクレイパー
# ─────────────────────────────────────────

def scrape_deco_dahlia(color: str, size: str) -> dict:
    """
    Shopify JSON API: /products/prs-{handle}.json でバリアント一覧取得
    SKUパターン: prs-{handle}-{size}q?-q1pc
    在庫: HTML + Schema.org JSON-LD
    """
    base_url = "https://www.san-ai-flowers.jp"
    result = {
        "site": "デコダリア", "site_id": "deco_dahlia",
        "color": color, "size": size,
        "price": None, "in_stock": False,
        "url": base_url,
        "note": "",
    }

    handle = DECO_DAHLIA_COLOR_MAP.get(color)
    if not handle:
        result["note"] = f"色コード未登録（{color}）"
        return result

    product_url = f"{base_url}/products/prs-{handle}"
    result["url"] = product_url

    # Shopify JSON API で全バリアントを取得
    try:
        r = requests.get(f"{product_url}.json", headers=HEADERS, timeout=15)
        if r.status_code == 404:
            result["note"] = "取り扱いなし"
            return result
        r.raise_for_status()
        data = r.json()
    except Exception:
        result["note"] = "取得失敗"
        return result

    size_lower = size.lower()  # "ss20"
    target_variant = None
    for v in data.get("product", {}).get("variants", []):
        sku = v.get("sku", "").lower()
        if f"-{size_lower}" in sku and "q1pc" in sku:
            target_variant = v
            break

    if not target_variant:
        result["note"] = "このサイズは取り扱いなし"
        return result

    try:
        result["price"] = int(float(target_variant.get("price", 0)))
    except (ValueError, TypeError):
        pass

    # 在庫確認: バリアントページの Schema.org JSON-LD
    variant_id = target_variant["id"]
    soup = fetch_html(f"{product_url}?variant={variant_id}")
    if soup:
        for script in soup.find_all("script", {"type": "application/ld+json"}):
            try:
                ld = json.loads(script.string or "")
                offers = ld.get("offers", [])
                if isinstance(offers, dict):
                    offers = [offers]
                for offer in offers:
                    avail = offer.get("availability", "")
                    if "InStock" in avail or "PreOrder" in avail:
                        result["in_stock"] = True
                    elif "OutOfStock" in avail:
                        result["in_stock"] = False
                    # SKUが一致するものがあれば優先
                    if offer.get("sku", "").lower() == target_variant.get("sku", "").lower():
                        result["in_stock"] = "InStock" in avail
                        break
                break
            except Exception:
                continue

    if not result["in_stock"]:
        result["note"] = "在庫なし"

    return result


# ─────────────────────────────────────────
# （旧クリスタルプロ スクレイパー — 参照用に残す）
# ─────────────────────────────────────────

def scrape_crystal_pro(color: str, size: str) -> dict:
    """
    /SHOP/PFB-{ColorCode}-GRS.html を直接取得
    select[name="VAR1-1"] のオプションでサイズを確認
    """
    base_url = "http://www.crystal-pro.com"
    result = {
        "site": "クリスタルプロ", "site_id": "crystal_pro",
        "color": color, "size": size,
        "price": None, "in_stock": False,
        "url": base_url,
        "note": "",
    }

    # 色コードを取得
    color_code = CRYSTAL_PRO_COLOR_MAP.get(color)
    if not color_code:
        result["note"] = f"色コード未登録（{color}）"
        return result

    product_url = f"{base_url}/SHOP/PFB-{color_code}-GRS.html"
    result["url"] = product_url

    soup = fetch_html(product_url)
    if not soup:
        return result

    # 404 or 商品なしの確認
    if "404" in (soup.title.get_text() if soup.title else "") or \
       "見つかりません" in soup.get_text():
        result["note"] = "取り扱いなし"
        return result

    # VAR1-1 select からサイズ一覧を取得（空の先頭オプションを除く）
    select = soup.find("select", {"name": "VAR1-1"})
    if not select:
        result["note"] = "サイズ情報取得不可"
        return result

    options = [o for o in select.find_all("option") if o.get("value", "")]
    size_index = None
    for i, opt in enumerate(options, start=1):
        if size.lower() in opt.get("value", "").lower():
            size_index = i
            break

    if size_index is None:
        result["in_stock"] = False
        result["note"] = "このサイズは取り扱いなし"
        return result

    # スクリプト内の arrVari からサイズ別価格・在庫を取得
    # arrVari['pos_N__'] = ["has_stock", "¥XX,XXX", ...]
    for script in soup.find_all("script"):
        txt = script.string or ""
        if "arrVari" not in txt:
            continue
        key = f"pos_{size_index}__"
        pattern = re.compile(
            r"arrVari\['" + re.escape(key) + r"'\]\s*=\s*\[(.*?)\];",
            re.DOTALL,
        )
        m = pattern.search(txt)
        if not m:
            continue
        # クォートされた文字列を個別に抽出（"12,936" のカンマに惑わされないよう）
        items = re.findall(r'"([^"]*)"', m.group(1))
        has_stock = items[0] == "1" if items else False
        price_raw = items[1] if len(items) > 1 else ""
        # "&#165;12,936" → "¥12,936" → 12936
        import html as _html
        price_clean = re.sub(r"[^\d]", "", _html.unescape(price_raw))
        result["in_stock"] = has_stock
        if price_clean:
            result["price"] = int(price_clean)
        if not has_stock:
            result["note"] = "在庫なし"
        break

    return result


# ─────────────────────────────────────────
# ④ チャーミーマーケット（静的価格）
# ─────────────────────────────────────────

def scrape_charmy_market(color: str, size: str) -> dict:
    """
    楽天チャーミーマーケット — スクレイピング不可のため手動設定価格を使用。
    5万円以上で15%OFFクーポンあり。送料常時無料。
    """
    result = {
        "site": "チャーミーマーケット", "site_id": "charmy_market",
        "color": color, "size": size,
        "price": None, "in_stock": False,
        "url": CHARMY_STORE_URL,
        "note": "",
    }

    category = CHARMY_COLOR_CATEGORY.get(color)
    if not category:
        result["note"] = f"取り扱いなし（{color}）"
        return result

    size_prices = CHARMY_PRICES.get(size)
    if not size_prices:
        result["note"] = "このサイズは取り扱いなし"
        return result

    price = size_prices.get(category)
    if price is None:
        result["note"] = "価格不明"
        return result

    coupon_price = int(price * (1 - CHARMY_COUPON_RATE))

    result["price"]        = price
    result["price_base"]   = price
    result["price_coupon"] = coupon_price
    result["coupon_note"]  = f"¥{CHARMY_COUPON_THRESHOLD:,}以上で15%OFF"
    result["in_stock"]     = True   # 静的データ：在庫はサイトで要確認
    return result


# ─────────────────────────────────────────
# 並列スクレイピング
# ─────────────────────────────────────────

def fetch_all(color: str, size: str) -> list[dict]:
    scrapers = [scrape_onocoltd, scrape_tsukuro, scrape_deco_dahlia, scrape_charmy_market]
    results = []
    with ThreadPoolExecutor(max_workers=4) as ex:
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
    site_totals = {"onocoltd": 0, "tsukuro": 0, "crystal_pro": 0}
    chosen = []

    for item in items:
        if not item["price"] or not item["in_stock"]:
            continue
        sid = item["site_id"]
        total_item = item["price"] * packs
        site_totals[sid] += total_item
        chosen.append({**item, "packs": packs, "subtotal": total_item})

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
        note = f"  備考   : {r['note']}" if r.get("note") else ""
        print(f"\n[{r['site']}] {r['color']} {r['size']}")
        print(f"  価格   : {price_str}{coupon}")
        print(f"  在庫   : {stock_mark}")
        if note:
            print(note)
        print(f"  URL    : {r['url']}")


def print_plan(plan: dict):
    print("\n" + "=" * 60)
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


# ─────────────────────────────────────────
# 監視リスト
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
        print(f"  → 登録しました: [{item['site']}] {item['color']} {item['size']}")
    else:
        print(f"  → 既に登録済みです")


def notify_line(message: str, token: str):
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
    wl = load_watchlist()
    if not wl:
        print("監視リストは空です。")
        return

    print(f"監視リスト: {len(wl)} 件をチェック中...")
    restocked = []
    scrapers = {
        "onocoltd": scrape_onocoltd,
        "tsukuro": scrape_tsukuro,
        "crystal_pro": scrape_crystal_pro,
    }
    for w in wl:
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
        writer = csv.DictWriter(f, fieldnames=["site", "color", "size", "price", "in_stock", "note", "url"])
        writer.writeheader()
        for r in results:
            writer.writerow({
                "site": r["site"],
                "color": r["color"],
                "size": r["size"],
                "price": r.get("price", ""),
                "in_stock": "在庫あり" if r["in_stock"] else "在庫なし",
                "note": r.get("note", ""),
                "url": r["url"],
            })
    print(f"  → CSV出力: {filepath}")


# ─────────────────────────────────────────
# メイン
# ─────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="プレシオサ Rose MAXIMA 最安購入プランナー")
    parser.add_argument("--color", "-c", help="カラー名（例: ジェット）")
    parser.add_argument("--size", "-s", choices=["SS12", "SS16", "SS20", "SS30"])
    parser.add_argument("--packs", "-p", type=int, default=1)
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--csv", action="store_true")
    parser.add_argument("--line-token")
    parser.add_argument("--watchlist", action="store_true")
    args = parser.parse_args()

    if args.watchlist:
        wl = load_watchlist()
        if not wl:
            print("監視リストは空です。")
        else:
            print(f"監視リスト ({len(wl)} 件):")
            for w in wl:
                print(f"  [{w['site']}] {w['color']} {w['size']}  登録日: {w['added']}")
        return

    if args.watch:
        watch_mode(line_token=args.line_token)
        return

    if not args.color or not args.size:
        parser.error("--color と --size を指定してください")

    print(f"\n検索中: {args.color} {args.size}  {args.packs}パック")
    if coupon_active():
        print(f"  ★ 今日はつくろうクーポン({COUPON_TSUKURO['code']})が使える日！（5%OFF）")

    results = fetch_all(args.color, args.size)
    print_results(results, args.packs)

    in_stock = [r for r in results if r["in_stock"] and r["price"]]
    if in_stock:
        plan = calc_plan(in_stock, args.packs)
        print_plan(plan)

    no_stock = [r for r in results if not r["in_stock"]]
    for r in no_stock:
        if r.get("note") in ("取り扱いなし", "このサイズは取り扱いなし", f"色コード未登録（{args.color}）"):
            continue
        ans = input(f"[{r['site']}] {r['color']} {r['size']} — 再入荷通知を希望しますか？ (y/n): ").strip().lower()
        if ans == "y":
            add_to_watchlist(r)

    if args.csv:
        export_csv(results)

    units = GROSS_PACK.get(args.size, "?")
    print(f"\n参考: {args.size} グロスパック = {units}個/パック")


if __name__ == "__main__":
    main()
