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
    # タイトルの短縮（microサイズに合わせてさらに短めに）
    display_title = (title[:18] + '...') if len(title) > 18 else title
    amz_url = f"https://www.amazon.co.jp/dp/{asin}?tag={AMAZPN_AFF_ID}"
    
    bubble = {
        "type": "bubble",
        "size": "micro",  # 以前と同じコンパクトサイズに戻しました
        "hero": {
            "type": "image",
            "url": f"https://images-na.ssl-images-amazon.com/images/P/{asin}.09.LZZZZZZZ.jpg",
            "size": "full", 
            "aspectMode": "cover", # 正しい値を指定
            "aspectRatio": "1:1"   # 正方形に戻しました
        },
        "body": {
            "type": "box", "layout": "vertical", "contents": [
                {"type": "text", "text": display_title, "weight": "bold", "size": "sm", "wrap": True},
                {"type": "box", "layout": "baseline", "contents": [
                    {"type": "text", "text": "Amazon:", "size": "xs", "color": "#888888", "flex": 2},
                    {"type": "text", "text": f"¥{amz_price:,}", "weight": "bold", "size": "md", "color": "#e47911", "flex": 4}
                ], "margin": "md"}
            ]
        },
        "footer": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "contents": [
                {"type": "button", "action": {"type": "uri", "label": "Amazon", "uri": amz_url}, "style": "primary", "color": "#f0c14b", "height": "sm"},
                {"type": "button", "action": {"type": "message", "label": "解除", "text": f"削除 {asin}"}, "style": "link", "color": "#ff0000", "height": "sm"}
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
        guide_text = (
            "【使い方ガイド】\n\n"
            "1. Amazonの商品ページで「共有」ボタンを押す\n"
            "2. このBotを選んでURLを送信\n"
            "3.Amazonの価格を即座にチェックします。\n\n"
            "🔔 [自動監視スタート]\n"
            "URLを送るだけで監視リストに登録！価格が下がった時に自動で通知します。\n\n"
            "❌ [監視を止めたい時]\n"
            "『解除』と送ると、現在監視中のリストを確認・消去できます。"
        )
        line_helper.reply_text(event.reply_token, guide_text)
        return

    if text.startswith("削除 "):
        asin_to_delete = text.split(" ")[1]
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("DELETE FROM users WHERE user_id = ? AND asin = ?", (user_id, asin_to_delete))
        conn.commit()
        deleted_count = c.rowcount
        conn.close()

        if deleted_count > 0:
            line_helper.reply_text(event.reply_token, f"商品（ASIN: {asin_to_delete}）を監視リストから削除したよ。")
        else:
            line_helper.reply_text(event.reply_token, "削除に失敗したか、すでにリストにないみたい。")
        return

    # --- リスト表示機能 ---
    if text == "マイリスト表示":
        logger.info(f": ユーザー {user_id} がマイリストを表示します")

        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        
        c.execute("""
            SELECT title, amz_price, rakuten_price, asin, rakuten_url 
            FROM users 
            WHERE user_id = ? 
            ORDER BY id DESC LIMIT 10
        """, (user_id,))
        
        rows = c.fetchall()
        conn.close()

        if not rows:
            line_helper.reply_text(user_id, "現在監視中の商品はありません。")
            return

        bubbles = []
        for title, amz_p, rak_p, asin, rak_url in rows:
            bubbles.append(create_product_bubble(title, amz_p, rak_p, asin, rak_url))
        carousel_contents = {
            "type": "carousel",
            "contents": bubbles
        }

        with ApiClient(configuration) as api_client:
            api = MessagingApi(api_client)
            api.push_message(
                PushMessageRequest(
                    to=user_id,
                    messages=[
                        FlexMessage(
                            alt_text="マイリスト表示",
                            contents=FlexContainer.from_dict(carousel_contents)
                        )
                    ]
                )
            )
            return

    # --- 4. 価格更新機能 ---
    if text == "価格更新":
        logger.info(f": ユーザー {user_id} 価格更新を押しました")
        line_helper.reply_text(event.reply_token, "全商品の最新価格をチェックするね！少し時間がかかるから終わったらリストを送るよ⏳")
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        # 【修正】新しい列名から情報を取得
        c.execute("SELECT title, asin, amz_price FROM users WHERE user_id = ?", (user_id,))
        rows = c.fetchall()
        with sync_playwright() as p:
            browser_instance = p.chromium.launch(headless=True, args=["--no-sandbox"])
            context = browser_instance.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page = context.new_page()
            for title, asin, old_amz_price in rows:
                # 1. Amazon最新価格を取得
                _, new_amz_price = get_product_info(page, asin)
                # 3. 【重要】Amazonと楽天の両方の情報をDBに保存
                c.execute("""
                    UPDATE users 
                    SET amz_price = ?
                    WHERE user_id = ? AND asin = ?
                """, (new_amz_price or old_amz_price, user_id, asin))
                page.wait_for_timeout(1000) 
            browser_instance.close()
        conn.commit()

        # 4. 更新後の最新データを再取得して、リストとして表示する
        c.execute("SELECT title, amz_price, asin FROM users WHERE user_id = ? ORDER BY id DESC LIMIT 10", (user_id,))
        updated_rows = c.fetchall()
        conn.close()

        # 5. 更新後のカードリスト（カルーセル）を作成
        bubbles = []
        for t, ap, rp, a, ru in updated_rows:
            bubbles.append(create_product_bubble(t, ap, rp, a, ru))

        carousel = {"type": "carousel", "contents": bubbles}

        # 6. 最後は Pushメッセージで「テキスト」と「最新リスト」をセットで送る
        with ApiClient(configuration) as api_client:
            api = MessagingApi(api_client)
            api.push_message(
                PushMessageRequest(
                    to=user_id,
                    messages=[
                        TextMessage(text="お待たせ！全部の価格を更新したよ。今の最安値はこれだね✨"),
                        FlexMessage(
                            alt_text="最新価格リスト",
                            contents=FlexContainer.from_dict(carousel)
                        )
                    ]
                )
            )
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