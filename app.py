"""
rhinestone Web アプリ
Flask + スマホ向けUI
"""

import os
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, jsonify, render_template, request

import rhinestone as rs

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True

COLORS = list(rs.CRYSTAL_PRO_COLOR_MAP.keys())
SIZES = ["SS12", "SS16", "SS20", "SS30"]


@app.route("/")
def index():
    return render_template("index.html", colors=COLORS, sizes=SIZES)


@app.route("/search", methods=["POST"])
def search():
    data = request.get_json()
    color = (data or {}).get("color", "").strip()
    items = (data or {}).get("items", [])  # [{"size": "SS20", "packs": 1}, ...]

    if not color or not items:
        return jsonify({"error": "色とサイズを選んでください"}), 400

    def fetch_size(item):
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
            "size": size,
            "packs": packs,
            "available": available,
            "unavailable": unavailable,
        }

    with ThreadPoolExecutor(max_workers=len(items)) as ex:
        futures = [ex.submit(fetch_size, item) for item in items]
        results = [f.result() for f in futures]

    size_order = {s: i for i, s in enumerate(SIZES)}
    results.sort(key=lambda x: size_order.get(x["size"], 99))

    return jsonify(results)


@app.route("/health")
def health():
    return "ok"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8085))
    app.run(host="0.0.0.0", port=port)
