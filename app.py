"""
rhinestone Web アプリ
Flask + スマホ向けUI（複数色対応）
"""

import os
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Flask, jsonify, render_template, request

import rhinestone as rs

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True

SIZES = ["SS12", "SS16", "SS20", "SS30"]


@app.route("/")
def index():
    return render_template("index.html", sizes=SIZES)


@app.route("/search", methods=["POST"])
def search():
    data = request.get_json() or {}
    colors = [c.strip() for c in data.get("colors", []) if c.strip()]
    items = data.get("items", [])  # [{"size": "SS20", "packs": 1}, ...]

    if not colors or not items:
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

    # 全色×全サイズを並列で取得
    tasks = [(color, item) for color in colors for item in items]
    results = []
    with ThreadPoolExecutor(max_workers=min(len(tasks), 8)) as ex:
        futures = {ex.submit(fetch_one, color, item): (color, item) for color, item in tasks}
        for f in as_completed(futures):
            results.append(f.result())

    # 色順 → サイズ順に並べ直し
    size_order = {s: i for i, s in enumerate(SIZES)}
    color_order = {c: i for i, c in enumerate(colors)}
    results.sort(key=lambda x: (color_order.get(x["color"], 99), size_order.get(x["size"], 99)))

    # 色ごとにグループ化
    grouped = {}
    for r in results:
        grouped.setdefault(r["color"], []).append(r)

    return jsonify([{"color": c, "sizes": grouped[c]} for c in colors if c in grouped])


@app.route("/health")
def health():
    return "ok"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8085))
    app.run(host="0.0.0.0", port=port)
