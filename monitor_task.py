import sqlite3
import time
import requests  # 楽天APIで必要
from playwright.sync_api import sync_playwright
from line_utils import LineBotHelper
from linebot.v3.webhook import WebhookHandler
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    PushMessageRequest,
    TextMessage
)
# import google.generativeai as genai
import os
import re
from dotenv import load_dotenv
from datetime import datetime
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)
# --- 設定 ---
load_dotenv()
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
ASSOCIATE_ID = os.getenv("AMAZPN_AFF_ID")
RAKUTEN_APP_ID = os.getenv("RAKUTEN_APP_ID")
RAKUTEN_AFF_ID = os.getenv("RAKUTEN_AFF_ID")
# GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DB_FILE = os.getenv("DB_FILE")
# genai.configure(api_key=GEMINI_API_KEY)
# 1.5 Flash の標準版を指定
# model = genai.GenerativeModel('gemini-1.5-flash')
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)


def get_rakuten_lowest_price(keyword, amz_price=None):
    """
    楽天APIで最安値を検索（monitor用・フィルタ付き）
    amz_priceを渡すことで、異常な価格を弾きます。
    """
    try:
        url = "https://app.rakuten.co.jp/services/api/IchibaItem/Search/20220601"
        params = {
            "applicationId": RAKUTEN_APP_ID,
            "affiliateId": RAKUTEN_AFF_ID,
            "keyword": keyword,
            "sort": "+itemPrice",  # 価格の安い順
            "hits": 10             # 少し多めに取って中身を精査する
        }
        res = requests.get(url, params=params, timeout=5)
        data = res.json()
        
        items = data.get("Items", [])
        if not items:
            return None, None

        for item_wrapper in items:
            item = item_wrapper.get("Item")
            price = item.get("itemPrice")
            name = item.get("itemName")
            url = item.get("affiliateUrl") or item.get("itemUrl")

            # --- フィルタリング ---
            # 1. 中古品は除外
            if "中古" in name:
                continue
            
            # 2. 価格がAmazonの40%以下（送料別などの罠）や2倍以上（ボッタクリ）は除外
            if amz_price:
                if price < (amz_price * 0.4) or price > (amz_price * 2.0):
                    continue
            
            # 最初に合格したものが「真の最安値」
            return price, url

    except Exception as e:
        logger.error(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [楽天エラー] {e}")
    
    return None, None

# def generate_sales_copy(old_price, new_price, title):
#     """
#     値下げ幅に応じて、Geminiがテンションを調整した紹介文を作る。
#     """
#     product_name = title if title else "注目の商品"
#     # いくら安くなったか計算
#     diff_price = old_price - new_price
    
#     # プロンプトを工夫
#     prompt = f"""
#     Amazonで「{product_name}」が値下げされました！
    
#     【価格の変化】
#     ・元の価格：{old_price}円
#     ・新しい価格：{new_price}円
#     ・値下げ額：{diff_price}円
    
#     条件：
#     ・値下げ額が「数十円〜数百円」なら、落ち着いたトーンで「少し安くなりました」と伝えて。
#     ・値下げ額が「数千円〜数万円」なら、テンション高く「衝撃の値下げ！」と伝えて。
#     ・絵文字を適度に使って。
#     ・3行程度で、最後には必ず『在庫切れに注意！』と入れる。
#     """
    
#     try:
#         response = model.generate_content(prompt)
#         return response.text.strip()
#     except Exception as e:
#         print(f"DEBUG [Sales Copy Error]: {e}")
#         return f"【通知】{product_name}が{new_price:,}円になりました（-{diff_price:,}円）。\n在庫切れに注意！"

def generate_sales_copy(old_price, new_price, title):
    """
    値下げ幅に応じて、テンションを自動調整した紹介文を返す。
    """
    product_name = title if title else "注目の商品"
    diff_price = old_price - new_price
    
    # 1. 値下げ幅に応じたメッセージのテンプレート設定
    if diff_price >= 5000:
        # 5000円以上の大幅値下げ
        tone = "【衝撃の超絶値下げ！🚨】"
        desc = f"なんと{diff_price:,}円も安くなっています！このチャンスは二度とないかもしれません。今すぐチェックを！"
    elif diff_price >= 1000:
        # 1000円〜4999円の値下げ
        tone = "【かなりお買い得です！✨】"
        desc = f"前回より{diff_price:,}円プライスダウン！欲しかった方は今が絶好のタイミングですよ。"
    elif diff_price >= 100:
        # 100円〜999円のちょっとした値下げ
        tone = "【少し安くなりました！安値更新💡】"
        desc = f"{diff_price:,}円の値下げです。じわじわ安くなっていますね。おトクなうちにどうぞ！"
    else:
        # 数十円程度の微増・微減
        tone = "【価格に動きがありました！】"
        desc = f"わずかですが{diff_price:,}円お安くなっています。少しでも安く買いたい方は必見です。"

    # 2. 組み合わせて返す（3行程度）
    copy = f"{tone}\n「{product_name}」\n{desc}\n在庫切れに注意！"
    
    return copy

def get_amazon_info_for_monitor(page, asin):
    url = f"https://www.amazon.co.jp/dp/{asin}?th=1&psc=1"
    try:
        # ここを確実に domcontentloaded に！
        page.goto(url, wait_until="domcontentloaded", timeout=30000)

        # タイトル (重複回避)
        title = "不明な商品"
        title_el = page.locator("#productTitle").first
        if title_el.is_visible():
            title = title_el.inner_text().strip()

        # 価格 (appと同じ粘り強いロジック)
        price = None
        selectors = [
            "span.a-price > span.a-offscreen", # 通常
            "span#price_inside_buybox",       # 予約品など
            "span.a-price-whole"              # セール時など
        ]
        
        for s in selectors:
            el = page.locator(s).first
            if el.is_visible():
                price_text = el.inner_text()
                if price_text:
                    # 数字以外（￥やコンマ）を消して数値化
                    price = int(re.sub(r'[^\d]', '', price_text))
                    break
        
        return price, title

    except Exception as e:
        logger.error(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [Error] ASIN:{asin}: {e}")
        return None, None
def classify_word(word):
    w = word.upper()

    # 型番：英字と数字が混ざる
    if re.search(r'[A-Z]', w) and re.search(r'\d', w):
        return "MODEL"

    # 世代：数字1〜2桁
    if re.fullmatch(r'\d{1,2}', w):
        return "VERSION"

    # 世代表記：3S / II / 5G
    if re.fullmatch(r'\d+[A-Z]{1,2}', w) or re.fullmatch(r'[IVX]{2,4}', w):
        return "VERSION"

    # 英字バージョン名：PRO / MAX / SE
    if re.fullmatch(r'[A-Z]{2,6}', w):
        return "VERSION"

    # カタカナ・漢字（シリーズ）
    if re.fullmatch(r'[ァ-ヶー]{3,}|[一-龠]{2,}', word):
        return "SERIES"

    return "OTHER"

def extract_keyword_smart(title):
    clean = re.sub(r'[\(\[【].*?[\)\]】]', '', title)
    clean = re.sub(r'[!@#$%^&*_=+\[\]{};:"\\|<>/?~]', ' ', clean)
    words = clean.split()
    if not words:
        return ""

    picked = []
    picked.append(words[0])  # 先頭＝ブランド仮定

    for w in words[1:]:
        t = classify_word(w)

        # 型番・世代は最優先
        if t in ("MODEL", "VERSION"):
            picked.append(w)
        # シリーズは補助
        elif t == "SERIES" and len(picked) < 4:
            picked.append(w)

        if len(picked) >= 5:
            break

    return " ".join(picked)
def main():
    # 1. DBから監視中のユーザーとASINを取得
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    # 新しい列名（amz_price, rakuten_price）に合わせて取得
    c.execute("SELECT user_id, asin, amz_price, rakuten_price, title FROM users")
    rows = c.fetchall()
    
    if not rows:
        logger.info("監視対象がDBにありません。")
        conn.close()
        return

    line_helper = LineBotHelper(configuration)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="ja-JP"
        )
        page = context.new_page()

        for user_id, asin, old_amz_price, old_rak_price, title in rows:
            logger.info(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Checking ASIN: {asin}...")
            
            # Amazonの最新価格と最新タイトルを取得
            new_amz_price, latest_title = get_amazon_info_for_monitor(page, asin)
            
            if not new_amz_price:
                logger.info(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]   ⚠️ 価格取得に失敗したためスキップします。")
                continue

            # --- 値下げ検知の条件（10円より大きく下がった場合のみ通知） ---
            price_diff = old_amz_price - new_amz_price
            
            if price_diff > 10:
                logger.info(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ★値下げ検知！ {old_amz_price} -> {new_amz_price} (-{price_diff}円)")
                
                # 楽天でも今の最安値を調べる
                safe_keyword = extract_keyword_smart(latest_title)
                new_rak_price, new_rak_url = get_rakuten_lowest_price(safe_keyword, new_amz_price)
                
                # Geminiに「差額」を考慮した熱量でコピーを作ってもらう
                ai_text = generate_sales_copy(old_amz_price, new_amz_price, safe_keyword)

                # メッセージ組み立て（Amazonアフィリンク付き）
                aff_url = f"https://www.amazon.co.jp/dp/{asin}?tag={AMAZON_AFF_ID}"
                message = f"【値下げ速報！】\n\n{ai_text}\n\n📦 Amazon: {new_amz_price:,}円\n{aff_url}"
                
                if new_rak_price:
                    message += f"\n\n🔴 楽天最安(修正中): {new_rak_price:,}円\n{new_rak_url}"
                    if new_rak_price < new_amz_price:
                        message += "\n(今は楽天の方が安いようです！)"

                # LINE通知（Pushメッセージで送信）
                try:
                    line_helper.push_text(user_id, message)
                except Exception as e:
                    logger.error(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] LINE通知エラー: {e}")
                
                # DB更新：Amazonと楽天、両方の最新情報を保存
                c.execute("""
                    UPDATE users 
                    SET amz_price = ?, rakuten_price = ?, rakuten_url = ?, title = ? 
                    WHERE user_id = ? AND asin = ?
                """, (new_amz_price, new_rak_price, new_rak_url, safe_keyword, user_id, asin))
                conn.commit()
                
            elif new_amz_price != old_amz_price:
                # 10円以下のわずかな変動や、逆に値上がりした場合は「通知なし」でDBだけ更新
                logger.info(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]   ー 価格変動なし ({new_amz_price:,}円)")
            else:
                # 値上がり、または10円未満の微小な変動
                status = "値上がり" if new_amz_price > old_amz_price else "微変動"
                logger.info(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]   ⤴ {status}: {old_amz_price:,}円 -> {new_amz_price:,}円 (通知スキップ)")
                
                # DBの値だけ最新にしておく
                c.execute("UPDATE users SET amz_price = ? WHERE user_id = ? AND asin = ?", 
                        (new_amz_price, user_id, asin))
                conn.commit()

            page.wait_for_timeout(3000) # 次の商品のアクセスまで間隔を空ける（BAN対策）

        browser.close()
    
    conn.close()
    logger.info("巡回完了")

if __name__ == "__main__":
    main()