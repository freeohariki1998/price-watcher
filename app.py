import os
import re
import sqlite3
import requests
from flask import Flask, request, abort
from playwright.sync_api import sync_playwright
from dotenv import load_dotenv
from line_utils import LineBotHelper
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    PushMessageRequest,
    TextMessage,
    FlexMessage,
    FlexContainer
)
import logging

# ログ設定
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

load_dotenv()
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
AMAZPN_AFF_ID = os.getenv("AMAZPN_AFF_ID")
DB_FILE = os.getenv("DB_FILE")

app = Flask(__name__)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
line_helper = LineBotHelper(configuration)

# --- Flex Message 生成関数 ---
def create_product_bubble(title, amz_price, asin):
    display_title = (title[:35] + '...') if len(title) > 35 else title
    amz_url = f"https://www.amazon.co.jp/dp/{asin}?tag={AMAZPN_AFF_ID}"
    
    bubble = {
        "type": "bubble",
        "size": "mega",
        "hero": {
            "type": "image",
            "url": f"https://images-na.ssl-images-amazon.com/images/P/{asin}.09.LZZZZZZZ.jpg",
            "size": "full", "aspectMode": "aspectFit", "aspectRatio": "20:13"
        },
        "body": {
            "type": "box", "layout": "vertical", "contents": [
                {"type": "text", "text": display_title, "weight": "bold", "size": "md", "wrap": True},
                {"type": "box", "layout": "baseline", "contents": [
                    {"type": "text", "text": "Amazon価格:", "size": "sm", "color": "#888888", "flex": 2},
                    {"type": "text", "text": f"¥{amz_price:,}", "weight": "bold", "size": "xl", "color": "#e47911", "flex": 3}
                ], "margin": "md"}
            ]
        },
        "footer": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "contents": [
                {"type": "button", "action": {"type": "uri", "label": "Amazonで見る", "uri": amz_url}, "style": "primary", "color": "#f0c14b"},
                {"type": "button", "action": {"type": "message", "label": "監視を解除する", "text": f"削除 {asin}"}, "style": "link", "color": "#ff0000"}
            ]
        }
    }
    return bubble

# --- Amazon情報取得 ---
def get_product_info(page, asin):
    url = f"https://www.amazon.co.jp/dp/{asin}?th=1&psc=1"
    title = "不明な商品"
    try:
        logger.info(f" [{asin}]: Amazonアクセス中...")
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        
        title_el = page.locator("#productTitle").first
        if title_el.is_visible():
            title = title_el.inner_text().strip()

        price_selectors = ["span#price_inside_buybox", "span.a-price-whole", "div#corePrice_feature_div span.a-offscreen"]
        for s in price_selectors:
            el = page.locator(s).first
            if el.is_visible():
                price_text = el.inner_text()
                if price_text:
                    price = int(re.sub(r'[^\d]', '', price_text))
                    return title, price
        return title, None
    except Exception as e:
        logger.error(f" [{asin}]: エラー: {e}")
        return title, None

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature) 
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    text = event.message.text
    user_id = event.source.user_id

    if text == "使い方":
        guide = ("【使い方】\nAmazon商品のURLを送るだけで、値下がり時に通知します！\n\n"
                 "・URL送信：監視登録\n・マイリスト表示：登録中の商品確認\n・価格更新：手動チェック")
        line_helper.reply_text(event.reply_token, guide)
        return

    if text.startswith("削除 "):
        asin_to_delete = text.split(" ")[1]
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("DELETE FROM users WHERE user_id = ? AND asin = ?", (user_id, asin_to_delete))
        conn.commit()
        success = c.rowcount > 0
        conn.close()
        line_helper.reply_text(event.reply_token, "削除したよ！" if success else "見つからなかったよ。")
        return

    if text == "マイリスト表示":
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT title, amz_price, asin FROM users WHERE user_id = ? ORDER BY id DESC LIMIT 10", (user_id,))
        rows = c.fetchall()
        conn.close()

        if not rows:
            line_helper.reply_text(event.reply_token, "現在監視中の商品はありません。")
            return

        bubbles = [create_product_bubble(t, p, a) for t, p, a in rows]
        with ApiClient(configuration) as api_client:
            api = MessagingApi(api_client)
            api.push_message(PushMessageRequest(to=user_id, messages=[
                FlexMessage(alt_text="マイリスト", contents=FlexContainer.from_dict({"type": "carousel", "contents": bubbles}))
            ]))
        return

    # URL登録処理
    if "amazon.co.jp" in text or "amzn.asia" in text:
        line_helper.reply_text(event.reply_token, "Amazonの価格を調べて登録するね！⏳")
        target_url = text.strip()
        
        # 短縮URL解決
        if "amzn.asia" in target_url:
            try:
                res = requests.get(target_url, allow_redirects=True, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
                target_url = res.url
            except: pass

        asin_match = re.search(r'/(?:dp|gp/product)/([A-Z0-9]{10})', target_url)
        if not asin_match:
            line_helper.push_text(user_id, "Amazonの商品URLが見つからなかったよ。")
            return
        
        asin = asin_match.group(1)
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            page = browser.new_page()
            title, price = get_product_info(page, asin)
            browser.close()

        if price:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("INSERT OR REPLACE INTO users (user_id, asin, title, amz_price) VALUES (?, ?, ?, ?)", (user_id, asin, title, price))
            conn.commit()
            conn.close()

            # 登録完了もカードで送る
            bubble = create_product_bubble(title, price, asin)
            with ApiClient(configuration) as api_client:
                api = MessagingApi(api_client)
                api.push_message(PushMessageRequest(to=user_id, messages=[
                    TextMessage(text="監視リストに追加したよ！値下がりしたら教えるね✨"),
                    FlexMessage(alt_text="登録完了", contents=FlexContainer.from_dict(bubble))
                ]))
        else:
            line_helper.push_text(user_id, "価格が読み取れなかったよ。もう一度試してみてね。")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)