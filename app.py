"""
rhinestone LINE Bot サーバー
Flask + LINE Messaging API v3

使い方（LINEから）:
  「ジェット SS20」         → 1パック検索
  「ジェット SS20 2」       → 2パック検索
  「監視リスト」            → 登録中の商品を表示
  「チェック」              → 再入荷チェックを今すぐ実行
  「使い方」               → ヘルプ表示
"""

import json
import os
import re
from pathlib import Path

from flask import Flask, abort, request
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    QuickReply,
    QuickReplyItem,
    MessageAction,
    ReplyMessageRequest,
    PushMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

import rhinestone as rs

app = Flask(__name__)

CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]

configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# ユーザーごとの会話状態（メモリ保持）
# { user_id: { "state": "idle" | "awaiting_restock", "pending": [item, ...] } }
user_sessions: dict[str, dict] = {}

SIZES = ["SS12", "SS16", "SS20", "SS30"]


# ─────────────────────────────────────────
# メッセージパース
# ─────────────────────────────────────────

def parse_query(text: str) -> tuple[str | None, str | None, int]:
    """
    「ジェット SS20」「ジェット SS20 2パック」などをパース
    → (color, size, packs)
    """
    text = text.strip()
    packs = 1
    # パック数を抽出
    m = re.search(r"(\d+)\s*パック?", text)
    if m:
        packs = int(m.group(1))
        text = text[:m.start()].strip()

    size = None
    for s in SIZES:
        if s.lower() in text.lower():
            size = s
            text = re.sub(s, "", text, flags=re.IGNORECASE).strip()
            break

    color = text.strip() if text.strip() else None
    return color, size, packs


# ─────────────────────────────────────────
# 返信テキスト生成
# ─────────────────────────────────────────

def format_results(results: list[dict], packs: int) -> str:
    coupon_note = ""
    if rs.coupon_active():
        coupon_note = "★ 今日はつくろうクーポン(5%OFF)が使える日！\n\n"

    lines = [coupon_note]
    for r in results:
        stock = "✅ 在庫あり" if r["in_stock"] else "❌ 在庫なし"
        price = f"¥{r['price']:,}" if r["price"] else "価格不明"
        coupon = "（クーポン適用済）" if r.get("coupon_applied") else ""
        lines.append(f"【{r['site']}】\n{stock}\n{price}{coupon}\n{r['url']}")

    return "\n\n".join(lines)


def format_plan(plan: dict) -> str:
    lines = ["─── 最安購入プラン ───"]
    for sid, p in plan["site_plan"].items():
        name = {"onocoltd": "OFFICE-K", "tsukuro": "つくろう", "crystal_pro": "クリスタルプロ"}[sid]
        if p["shipping"] == 0:
            shipping_str = "送料無料"
        else:
            shipping_str = f"送料 ¥{p['shipping']:,}（あと ¥{p['free_shortage']:,} で無料）"
        lines.append(f"\n【{name}】\n小計 ¥{p['subtotal']:,} / {shipping_str}\n合計 ¥{p['total']:,}")

    lines.append(f"\n━ 総合計 ¥{plan['grand_total']:,}")
    return "\n".join(lines)


def help_text() -> str:
    return (
        "【使い方】\n\n"
        "🔍 価格チェック\n"
        "「カラー サイズ」と送ってください\n"
        "例）ジェット SS20\n"
        "例）クリスタル SS16 2パック\n\n"
        "📋 監視リスト確認\n"
        "「監視リスト」と送ってください\n\n"
        "🔔 再入荷チェック（今すぐ）\n"
        "「チェック」と送ってください\n\n"
        "対応サイズ：SS12 / SS16 / SS20 / SS30"
    )


# ─────────────────────────────────────────
# 返信ヘルパー
# ─────────────────────────────────────────

def reply(reply_token: str, text: str, quick_replies: list | None = None):
    msg = TextMessage(text=text)
    if quick_replies:
        msg.quick_reply = QuickReply(items=[
            QuickReplyItem(action=MessageAction(label=label, text=text_))
            for label, text_ in quick_replies
        ])
    with ApiClient(configuration) as client:
        api = MessagingApi(client)
        api.reply_message(ReplyMessageRequest(
            reply_token=reply_token,
            messages=[msg],
        ))


def push(user_id: str, text: str, quick_replies: list | None = None):
    msg = TextMessage(text=text)
    if quick_replies:
        msg.quick_reply = QuickReply(items=[
            QuickReplyItem(action=MessageAction(label=label, text=text_))
            for label, text_ in quick_replies
        ])
    with ApiClient(configuration) as client:
        api = MessagingApi(client)
        api.push_message(PushMessageRequest(
            to=user_id,
            messages=[msg],
        ))


# ─────────────────────────────────────────
# メッセージ処理
# ─────────────────────────────────────────

def handle_message(user_id: str, text: str, reply_token: str):
    session = user_sessions.get(user_id, {"state": "idle", "pending": []})
    text = text.strip()

    # ─── 再入荷通知の返答待ち ───
    if session["state"] == "awaiting_restock":
        pending = session.get("pending", [])
        if text in ("はい", "yes", "YES", "はい！"):
            for item in pending:
                rs.add_to_watchlist(item)
            session = {"state": "idle", "pending": []}
            user_sessions[user_id] = session
            names = "\n".join([f"・[{i['site']}] {i['color']} {i['size']}" for i in pending])
            reply(reply_token, f"登録しました！入荷したらすぐお知らせします📦\n\n{names}")
        elif text in ("いいえ", "no", "NO", "いいえ"):
            session = {"state": "idle", "pending": []}
            user_sessions[user_id] = session
            reply(reply_token, "了解です。また検索するときはいつでも声かけてください！")
        else:
            reply(reply_token, "「はい」か「いいえ」で教えてください",
                  quick_replies=[("はい", "はい"), ("いいえ", "いいえ")])
        return

    # ─── 監視リスト表示 ───
    if text in ("監視リスト", "監視", "ウォッチリスト"):
        wl = rs.load_watchlist()
        if not wl:
            reply(reply_token, "監視リストは空です。\n在庫なしの商品を検索すると登録できます。")
        else:
            lines = [f"現在の監視リスト（{len(wl)}件）\n"]
            for w in wl:
                lines.append(f"・[{w['site']}] {w['color']} {w['size']}")
            reply(reply_token, "\n".join(lines))
        return

    # ─── 今すぐ再入荷チェック ───
    if text in ("チェック", "check", "再入荷チェック", "確認"):
        wl = rs.load_watchlist()
        if not wl:
            reply(reply_token, "監視リストが空です。先に商品を検索して登録してください。")
            return
        reply(reply_token, f"チェック中です…（{len(wl)}件）")
        restocked = []
        for w in wl:
            scrapers = {
                "onocoltd": rs.scrape_onocoltd,
                "tsukuro": rs.scrape_tsukuro,
                "crystal_pro": rs.scrape_crystal_pro,
            }
            fn = scrapers.get(w["site_id"])
            if fn:
                r = fn(w["color"], w["size"])
                if r["in_stock"]:
                    restocked.append(r)
        if restocked:
            msg = "入荷してました！\n\n"
            for r in restocked:
                msg += f"✅ [{r['site']}] {r['color']} {r['size']}\n¥{r['price']:,}\n{r['url']}\n\n"
            push(user_id, msg.strip())
        else:
            push(user_id, "まだ在庫は復活していません。引き続き監視中です🔍")
        return

    # ─── ヘルプ ───
    if text in ("使い方", "ヘルプ", "help", "？", "?"):
        reply(reply_token, help_text())
        return

    # ─── 価格検索 ───
    color, size, packs = parse_query(text)

    if not color or not size:
        reply(reply_token,
              "「カラー サイズ」の形式で送ってください\n例）ジェット SS20\n例）クリスタル SS16 2パック\n\n「使い方」と送るとヘルプが見られます")
        return

    # 「検索中」を即返信（reply tokenはここで使い切る）
    reply(reply_token, f"検索中です…⏳\n{color} {size} × {packs}パック\n少々お待ちください")

    # 検索実行（時間がかかるのでpushで結果を送る）
    results = rs.fetch_all(color, size)
    result_text = format_results(results, packs)

    in_stock = [r for r in results if r["in_stock"] and r["price"]]
    if in_stock:
        plan = rs.calc_plan(in_stock, packs)
        full_text = result_text + "\n\n" + format_plan(plan)
    else:
        full_text = result_text + "\n\n在庫のある商品が見つかりませんでした。"

    no_stock = [r for r in results if not r["in_stock"] and "取り扱いなし" not in r.get("note", "")]

    if no_stock:
        full_text += f"\n\n❌ 在庫なし商品が{len(no_stock)}件あります。\n再入荷通知を希望しますか？"
        user_sessions[user_id] = {"state": "awaiting_restock", "pending": no_stock}
        push(user_id, full_text,
             quick_replies=[("希望する", "はい"), ("希望しない", "いいえ")])
    else:
        user_sessions[user_id] = {"state": "idle", "pending": []}
        push(user_id, full_text)


# ─────────────────────────────────────────
# Webhook エンドポイント
# ─────────────────────────────────────────

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"


@handler.add(MessageEvent, message=TextMessageContent)
def on_message(event: MessageEvent):
    handle_message(
        user_id=event.source.user_id,
        text=event.message.text,
        reply_token=event.reply_token,
    )


@app.route("/health")
def health():
    return "ok"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
