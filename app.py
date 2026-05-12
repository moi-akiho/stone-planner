"""
rhinestone Web アプリ
Flask + スマホ向けUI（色ごとに独立したサイズ・数量設定）
"""

import json
import os
import smtplib
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.mime.text import MIMEText
from pathlib import Path

from flask import Flask, jsonify, render_template, request

import rhinestone as rs

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True

SIZES = ["SS12", "SS16", "SS20", "SS30"]


@app.route("/")
def index():
    wl = rs.load_watchlist()
    return render_template("index.html", sizes=SIZES, watchlist_count=len(wl))


# ─── 価格検索 ───────────────────────────────

@app.route("/search", methods=["POST"])
def search():
    data = request.get_json() or {}
    requests_list = data.get("requests", [])

    if not requests_list:
        return jsonify({"error": "色とサイズを入力してください"}), 400

    def fetch_one(color, item):
        size = item["size"]
        packs = max(1, int(item.get("packs", 1)))
        all_results = rs.fetch_all(color, size)

        available = sorted(
            [r for r in all_results if r["in_stock"] and r["price"]],
            key=lambda x: x["price"],
        )
        unavailable = [r for r in all_results if not r["in_stock"]]

        for r in available:
            sid = r["site_id"]
            s = rs.SHIPPING[sid]
            subtotal = r["price"] * packs
            shipping = 0 if subtotal >= s["free_threshold"] else s["cost"]
            shortage = max(0, s["free_threshold"] - subtotal) if shipping > 0 else 0
            r["packs"] = packs
            r["subtotal"] = subtotal
            r["shipping"] = shipping
            r["free_shortage"] = shortage
            r["total"] = subtotal + shipping

        return {
            "color": color,
            "size": size,
            "packs": packs,
            "available": available,
            "unavailable": unavailable,
        }

    tasks = [
        (req["color"], item)
        for req in requests_list
        for item in req.get("items", [])
    ]
    if not tasks:
        return jsonify({"error": "サイズを選んでください"}), 400

    size_order = {s: i for i, s in enumerate(SIZES)}
    color_order = {req["color"]: i for i, req in enumerate(requests_list)}

    results = []
    with ThreadPoolExecutor(max_workers=min(len(tasks), 8)) as ex:
        futures = {ex.submit(fetch_one, c, item): (c, item) for c, item in tasks}
        for f in as_completed(futures):
            results.append(f.result())

    results.sort(key=lambda x: (color_order.get(x["color"], 99), size_order.get(x["size"], 99)))

    grouped = {}
    for r in results:
        grouped.setdefault(r["color"], []).append(r)

    colors_in_order = [req["color"] for req in requests_list]
    return jsonify([{"color": c, "sizes": grouped[c]} for c in colors_in_order if c in grouped])


# ─── 監視リスト ──────────────────────────────

@app.route("/watchlist", methods=["GET"])
def get_watchlist():
    return jsonify(rs.load_watchlist())


@app.route("/watchlist/add", methods=["POST"])
def add_watchlist():
    item = request.get_json() or {}
    required = {"site_id", "site", "color", "size", "url"}
    if not required.issubset(item.keys()):
        return jsonify({"error": "情報が不足しています"}), 400
    rs.add_to_watchlist(item)
    return jsonify({"ok": True, "count": len(rs.load_watchlist())})


@app.route("/watchlist/remove", methods=["POST"])
def remove_watchlist():
    data = request.get_json() or {}
    site_id, color, size = data.get("site_id"), data.get("color"), data.get("size")
    wl = rs.load_watchlist()
    wl = [w for w in wl if not (w["site_id"] == site_id and w["color"] == color and w["size"] == size)]
    rs.save_watchlist(wl)
    return jsonify({"ok": True, "count": len(wl)})


@app.route("/watchlist/check", methods=["POST"])
def check_watchlist():
    wl = rs.load_watchlist()
    if not wl:
        return jsonify([])

    def check_one(w):
        fn = {"onocoltd": rs.scrape_onocoltd, "tsukuro": rs.scrape_tsukuro}.get(w["site_id"])
        if not fn:
            return None
        r = fn(w["color"], w["size"])
        return {**w, "in_stock": r["in_stock"], "price": r.get("price")}

    results = []
    with ThreadPoolExecutor(max_workers=min(len(wl), 6)) as ex:
        futures = [ex.submit(check_one, w) for w in wl]
        for f in as_completed(futures):
            r = f.result()
            if r:
                results.append(r)

    results.sort(key=lambda x: (x["color"], x["size"]))
    return jsonify(results)


@app.route("/watchlist/notify", methods=["GET", "POST"])
def notify():
    """cron-job.org から定期的に叩かれるエンドポイント。在庫復活があればメール送信。"""
    wl = rs.load_watchlist()
    if not wl:
        return jsonify({"ok": True, "restocked": []})

    def check_one(w):
        fn = {"onocoltd": rs.scrape_onocoltd, "tsukuro": rs.scrape_tsukuro}.get(w["site_id"])
        if not fn:
            return None
        r = fn(w["color"], w["size"])
        return {**w, "in_stock": r["in_stock"], "price": r.get("price")}

    results = []
    with ThreadPoolExecutor(max_workers=min(len(wl), 6)) as ex:
        futures = [ex.submit(check_one, w) for w in wl]
        for f in as_completed(futures):
            r = f.result()
            if r:
                results.append(r)

    restocked = [r for r in results if r["in_stock"]]

    if restocked:
        _send_restock_mail(restocked)

    return jsonify({"ok": True, "restocked": [f"{r['color']} {r['size']} ({r['site']})" for r in restocked]})


def _send_restock_mail(items: list):
    mail_to = os.environ.get("NOTIFY_MAIL_TO", "")
    mail_password = os.environ.get("NOTIFY_MAIL_PASSWORD", "")
    if not mail_to or not mail_password:
        return

    lines = ["【ラインストーン 再入荷通知】\n"]
    for r in items:
        price_str = f"¥{r['price']:,}" if r.get("price") else "価格不明"
        lines.append(f"✅ {r['color']} {r['size']}")
        lines.append(f"   サイト: {r['site']}")
        lines.append(f"   価格: {price_str}")
        lines.append(f"   URL: {r['url']}\n")

    body = "\n".join(lines)
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = f"💎 再入荷！{items[0]['color']} {items[0]['size']} など{len(items)}件"
    msg["From"] = mail_to
    msg["To"] = mail_to

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(mail_to, mail_password)
            smtp.send_message(msg)
    except Exception as e:
        print(f"[メール送信失敗] {e}")


@app.route("/health")
def health():
    return "ok"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8085))
    app.run(host="0.0.0.0", port=port)
