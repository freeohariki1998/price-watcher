import sqlite3
import re
import os
import logging
from datetime import datetime
from playwright.sync_api import sync_playwright
from dotenv import load_dotenv
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    PushMessageRequest,
    FlexMessage,
    FlexContainer
)

# ログ設定
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

load_dotenv()
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
AMAZPN_AFF_ID = os.getenv("AMAZPN_AFF_ID")
DB_FILE = os.getenv("DB_FILE")

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)

# --- 値下げ通知用の Flex Message 生成 ---
# --- 値下げ通知用の Flex Message 生成 ---
def create_sale_notification_bubble(title, old_price, new_price, asin):
    display_title = (title[:18] + '...') if len(title) > 18 else title
    # 前回の修正を反映して AMAZON_AFF_ID に統一
    amz_url = f"https://www.amazon.co.jp/dp/{asin}?tag={AMAZPN_AFF_ID}"
    
    bubble = {
        "type": "bubble",
        "size": "micro",
        "hero": {
            "type": "image",
            "url": f"https://images-na.ssl-images-amazon.com/images/P/{asin}.09.LZZZZZZZ.jpg",
            "size": "full", 
            "aspectMode": "cover",
            "aspectRatio": "1:1"
        },
        "body": {
            "type": "box", "layout": "vertical", "contents": [
                {"type": "text", "text": display_title, "weight": "bold", "size": "sm", "wrap": True},
                {"type": "box", "layout": "baseline", "contents": [
                    {"type": "text", "text": "Amazon:", "size": "xs", "color": "#888888", "flex": 2},
                    # ここを amz_price から new_price に修正
                    {"type": "text", "text": f"¥{new_price:,}", "weight": "bold", "size": "md", "color": "#e47911", "flex": 4}
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

# --- Amazon価格取得ロジック ---
def get_amazon_info_for_monitor(page, asin):
    url = f"https://www.amazon.co.jp/dp/{asin}?th=1&psc=1"
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        
        # タイトル取得
        title = "不明な商品"
        title_el = page.locator("#productTitle").first
        if title_el.is_visible():
            title = title_el.inner_text().strip()

        # 価格取得
        price = None
        selectors = ["span.a-price > span.a-offscreen", "span#price_inside_buybox", "span.a-price-whole"]
        for s in selectors:
            el = page.locator(s).first
            if el.is_visible():
                price_text = el.inner_text()
                if price_text:
                    price = int(re.sub(r'[^\d]', '', price_text))
                    break
        return price, title
    except Exception as e:
        logger.error(f"ASIN:{asin} 取得失敗: {e}")
        return None, None

def main():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT user_id, asin, amz_price, title FROM users")
    rows = c.fetchall()
    
    if not rows:
        logger.info("監視対象がいません。")
        conn.close()
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(user_agent="Mozilla/5.0...", locale="ja-JP")
        page = context.new_page()

        for user_id, asin, old_price, saved_title in rows:
            logger.info(f"Checking {asin}...")
            new_price, latest_title = get_amazon_info_for_monitor(page, asin)
            
            if new_price is None:
                continue

            # 10円以上の値下げを検知
            if old_price - new_price > 10:
                logger.info(f"★値下げ検知！ {old_price} -> {new_price}")
                
                # カード型メッセージ作成
                bubble = create_sale_notification_bubble(latest_title, old_price, new_price, asin)
                
                # 通知送信
                with ApiClient(configuration) as api_client:
                    api = MessagingApi(api_client)
                    api.push_message(PushMessageRequest(
                        to=user_id,
                        messages=[FlexMessage(alt_text="値下げ通知！", contents=FlexContainer.from_dict(bubble))]
                    ))
                
                # DB更新
                c.execute("UPDATE users SET amz_price = ?, title = ? WHERE user_id = ? AND asin = ?", 
                        (new_price, latest_title, user_id, asin))
            
            elif new_price != old_price:
                # 値上がりや微変動はDBのみ更新（通知なし）
                c.execute("UPDATE users SET amz_price = ? WHERE user_id = ? AND asin = ?", 
                        (new_price, user_id, asin))
            
            conn.commit()
            page.wait_for_timeout(3000) # BAN対策の間隔

        browser.close()
    conn.close()
    logger.info("巡回完了")

if __name__ == "__main__":
    main()