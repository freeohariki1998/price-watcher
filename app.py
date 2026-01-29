import os
import re
import sqlite3
import atexit
import requests
from flask import Flask, request, abort
from playwright.sync_api import sync_playwright
# import google.generativeai as genai
from dotenv import load_dotenv
from line_utils import LineBotHelper
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    PushMessageRequest,
    TextMessage,
    FlexMessage,
    FlexContainer
)
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
# --- キーの取得 ---
load_dotenv()
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
AMAZPN_AFF_ID = os.getenv("AMAZPN_AFF_ID")
RAKUTEN_APP_ID = os.getenv("RAKUTEN_APP_ID")
RAKUTEN_AFF_ID = os.getenv("RAKUTEN_AFF_ID")
# GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DB_FILE = os.getenv("DB_FILE")
# --- 初期設定 ---
app = Flask(__name__)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
line_helper = LineBotHelper(configuration)
# --- Geminiの設定 ---
# genai.configure(api_key=GEMINI_API_KEY)
# 1.5 Flash の標準版を指定
# model = genai.GenerativeModel('gemini-1.5-flash')



def create_product_bubble(title, amz_price, rak_price, asin, rak_url):
    # タイトルの短縮
    display_title = (title[:20] + '...') if len(title) > 20 else title
    amz_url = f"https://www.amazon.co.jp/dp/{asin}?tag={AMAZPN_AFF_ID}"
    
    # 価格表示の準備
    rak_text = f"¥{rak_price:,}" if rak_price else "なし"

    # フッターのボタンリストを作成
    footer_contents = [
        # Amazonボタン（常に表示）
        {"type": "button", "action": {"type": "uri", "label": "Amazon", "uri": amz_url}, "style": "primary", "color": "#f0c14b", "height": "sm"}
    ]

    # 楽天URLがある場合のみ楽天ボタンを追加
    if rak_url and rak_url.startswith("http"):
        footer_contents.append(
            {"type": "button", "action": {"type": "uri", "label": "楽天", "uri": rak_url}, "style": "primary", "color": "#bf0000", "height": "sm"}
        )

    # 削除ボタン
    footer_contents.append(
        {"type": "button", "action": {"type": "message", "label": "削除", "text": f"削除 {asin}"}, "style": "link", "color": "#ff0000", "height": "sm"}
    )

    bubble = {
        "type": "bubble",
        "size": "micro",
        "hero": {
            "type": "image",
            "url": f"https://images-na.ssl-images-amazon.com/images/P/{asin}.09.LZZZZZZZ.jpg",
            "size": "full", "aspectMode": "cover", "aspectRatio": "1:1"
        },
        "body": {
            "type": "box", "layout": "vertical", "contents": [
                {"type": "text", "text": display_title, "weight": "bold", "size": "sm", "wrap": True},
                {"type": "box", "layout": "baseline", "contents": [
                    {"type": "text", "text": "Amz:", "size": "xs", "color": "#888888", "flex": 1},
                    {"type": "text", "text": f"¥{amz_price:,}", "weight": "bold", "size": "md", "color": "#e47911", "flex": 4}
                ], "margin": "md"},
                {"type": "box", "layout": "baseline", "contents": [
                    {"type": "text", "text": "Rak:", "size": "xs", "color": "#888888", "flex": 1},
                    {"type": "text", "text": rak_text, "weight": "bold", "size": "md", "color": "#bf0000", "flex": 4}
                ]}
            ]
        },
        "footer": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "contents": footer_contents # 動的に作ったボタンリストを入れる
        }
    }
    return bubble


"""
    Amazonの複雑な商品名から、検索に便利な『短い商品名』だけを取り出す関数。
"""
def extract_product_name_by_gemini(title):
    try:
        # AIへの指示を「検索用」から「特定用」に変更
        prompt = f"""
        あなたは楽天市場検索専用のキーワード生成AIです。
        以下の商品タイトルから、「別世代の商品を誤ヒットさせない」検索キーワードを生成してください。

        【最重要ルール（絶対厳守）】
        ・ブランド名は必須
        ・**世代番号 または 型番のどちらか1つ以上を必ず含めること**
        ・上記が両方削除されるキーワードは生成禁止

        【生成ルール】
        1. 単語数は 2〜5語まで
        2. 世代番号（例：2 / 3 / 4 / Pro / Max / Ultra など）は削除禁止
        3. 型番（英数字2文字以上）は削除禁止
        4. 色・状態・数量（ブラック / 新品 など）は削除
        5. 記号はすべて削除
        6. 楽天検索で一般的に使われる表記を優先
        7. 出力は **1行のみ・キーワードのみ**
        8. 説明文は一切出力しない

        【商品タイトル】
        {title}
        """



        
        response = model.generate_content(prompt)
        # Geminiが「」や説明を付けてくることがあるので、改行や余計な文字を掃除
        clean_res = response.text.replace('「', '').replace('」', '').strip().split('\n')[0]
        logger.info(f" [楽天で調べるキーワード]: {clean_res}")
        return clean_res
    except Exception as e:
        logger.error(f"エラー発生: {e}")
        # 失敗した時は、せめて最初の15文字くらいを返す（短すぎるとズレるため）
        return title[:20]

"""
    楽天APIを使って、指定したキーワードで一番安い商品を検索する関数。
    amz_price（Amazonの価格）を渡すと、それと比較して変な商品は除外する。
"""
def get_rakuten_lowest_price(keyword, amz_price=None):
    try:
        # 楽天検索システムにアクセスするための設定
        safe_keyword = re.sub(r'[!@#$%^&*()_=+\[\]{};:"\\|,.<>/?~-]', ' ', keyword)
        words = [w for w in safe_keyword.split() if len(w) >= 2]
        safe_keyword = " ".join(words[:3])
        logger.info(f" [楽天API用キーワード]: {safe_keyword}")
        url = "https://app.rakuten.co.jp/services/api/IchibaItem/Search/20220601"
        # get_rakuten_lowest_price の params を修正
        params = {
            "applicationId": RAKUTEN_APP_ID,
            "affiliateId": RAKUTEN_AFF_ID,
            "keyword": safe_keyword,
            "NGKeyword": "ケース 中古 訳あり ジャンク 互換品",
            "sort": "+itemPrice",
            "hits": 15,
            "minPrice": int(amz_price * 0.8) if amz_price else None
        }
        # 楽天へアクセス
        res = requests.get(url, params=params, timeout=5)
        data = res.json()
        
        # API自体のエラーやヒットゼロを確認
        if "error" in data:
            logger.info(f" [楽天APIエラー]: {data.get('error_description')}")
            return None, None
        
        count = data.get("count", 0)
        logger.info(f" [楽天ヒット数]: {count}件")
        
        valid_prices = []
        items = data.get("Items", [])
        
        if not items:
            logger.info(f": 楽天で1件もヒットしませんでした。キーワードを変える必要があります。")
            return None, None

        for i, item_wrapper in enumerate(items):
            item = item_wrapper.get("Item")
            price = item.get('itemPrice')
            name = item.get('itemName')
            
            # 各商品がなぜ通ったか/落ちたかを出力
            logger.info(f"--- 楽天候補 [{i+1}] ---")
            logger.info(f"名前: {name[:30]}...")
            logger.info(f"価格: {price}円")
            
            # フィルタリングのログ
            if amz_price:
                low_limit = amz_price * 0.4 # Amazonの40%以下の価格は怪しい（偽物や送料別など）
                high_limit = amz_price * 2.0 # Amazonの2倍以上の価格は除外
                if price < low_limit:
                    logger.info(f"❌ 却下: 安すぎます (下限: {low_limit:.0f}円)")
                    continue
                if price > high_limit:
                    logger.info(f"❌ 却下: 高すぎます (上限: {high_limit:.0f}円)")
                    continue
            # 中古品の除外
            if "中古" in name:
                logger.info(f"❌ 却下: 中古品です")
                continue
                
            logger.info(f"✅ 採用候補！")
            valid_prices.append((price, item.get('itemAffiliateUrl') or item.get('itemUrl')))
        # 一番安いものを最終決定 
        if valid_prices:
            best_match = min(valid_prices, key=lambda x: x[0])
            logger.info(f": 最終選定された楽天価格: {best_match[0]}円")
            return best_match

    except Exception as e:
        logger.error(f" [楽天関数内エラー]: {e}")
    return None, None

# アマゾンのデータを取得
def get_product_info(page, asin):
    # バリエーションを強制固定するURL
    url = f"https://www.amazon.co.jp/dp/{asin}?th=1&psc=1"
    title = "不明な商品"
    
    try:
        logger.info(f" [{asin}]: Amazonアクセス中... {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        price_selector = "span#price_inside_buybox, span.a-price-whole, div#corePrice_feature_div span.a-offscreen"
        try:
            page.wait_for_selector(price_selector, timeout=5000) 
        except:
            logger.info(f" [{asin}]: セレクターが見つかりませんでした。")

        # タイトル
        title_el = page.locator("#productTitle").first
        if title_el.is_visible():
            title = title_el.inner_text().strip()

        # 価格（複数セレクターで粘る）
        price_text = None
        selectors = [
            "span#price_inside_buybox",
            "span.a-p-price-whole",
            "div#corePrice_feature_div span.a-offscreen"
        ]
        
        for s in selectors:
            el = page.locator(s).first
            if el.is_visible():
                price_text = el.inner_text()
                if price_text:
                    logger.info(f" [{asin}]: 価格発見({s}): {price_text}")
                    break
        
        if price_text:
            price = int(re.sub(r'[^\d]', '', price_text))
            return title, price
        return title, None

    except Exception as e:
        logger.error(f" [{asin}]: エラー: {e}")
        return title, None

# 楽天のワードを変更中
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

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature) 
    except InvalidSignatureError:
        logger.error(f"エラー発生: 署名検証に失敗しました。チャネルシークレットを確認してください。")
        abort(400)
    except Exception as e:
        logger.error(f"エラー発生: {e}")
        abort(500)
    return 'OK'

# LINEから送られてきたメッセージを受け取り対応
@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    # 届いたメッセージの中身（テキスト）を取り出す
    text = event.message.text
    # 送ってきた人のID（誰が送ったか）を特定する
    user_id = event.source.user_id
    # line_helper = LineBotHelper(configuration)
    # --- 使い方 ---
    if text == "使い方":
        guide_text = (
            "【使い方ガイド】\n\n"
            "1. Amazonの商品ページで「共有」ボタンを押す\n"
            "2. このBotを選んでURLを送信\n"
            "3.Amazonの価格を即座にチェックします。\n\n"
            "🔔 [自動監視スタート]\n"
            "URLを送るだけで監視リストに登録！価格が下がった時に自動で通知します。\n\n"
            "🔴 [楽天比較機能について]\n"
            "現在、楽天の最安値も一緒に探す機能を『テスト公開中』です。一部商品で精度を調整中のため、参考値としてご利用ください🛠️\n\n"
            "❌ [監視を止めたい時]\n"
            "『解除』と送ると、現在監視中のリストを確認・消去できます。"
        )
        line_helper.reply_text(event.reply_token, guide_text)
        return
    # --- 削除機能の追加 ---
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
                
                # 2. 楽天最新価格を取得
                search_keyword = extract_keyword_smart(title)
                rak_price, rak_url = get_rakuten_lowest_price(search_keyword, new_amz_price or old_amz_price)

                # 3. 【重要】Amazonと楽天の両方の情報をDBに保存
                c.execute("""
                    UPDATE users 
                    SET amz_price = ?, rakuten_price = ?, rakuten_url = ? 
                    WHERE user_id = ? AND asin = ?
                """, (new_amz_price or old_amz_price, rak_price, rak_url, user_id, asin))
                
                page.wait_for_timeout(1000) # ラズパイへの負荷軽減

            browser_instance.close()
        
        conn.commit()

        # 4. 更新後の最新データを再取得して、リストとして表示する
        c.execute("SELECT title, amz_price, rakuten_price, asin, rakuten_url FROM users WHERE user_id = ? ORDER BY id DESC LIMIT 10", (user_id,))
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

    
    # --- URL解析・登録ロジック ---
    # --- 5. URL解析・登録ロジック ---
    # まず、ユーザーに「これから時間がかかる作業を始めるよ」と即レスする
    line_helper.reply_text(event.reply_token, "Amazonと楽天の価格を調べて登録するね！少し時間がかかるから、終わったらまたメッセージを送るよ⏳")
    target_url = text.strip()
    asin = None
    amz_price = None
    title = "商品"

    # 最初のASIN抽出試行
    asin_match = re.search(r'/(?:dp|gp/product)/([A-Z0-9]{10})', target_url)
    if asin_match:
        asin = asin_match.group(1)

    # 2. Playwright 起動
    try:
        with sync_playwright() as p:
            # ラズパイでも動くように低負荷設定で起動
            browser_instance = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"])
            context = browser_instance.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                locale="ja-JP"
            )
            context.clear_cookies()
            page = context.new_page()
            page.route("**/*.{png,jpg,jpeg,gif,webp,svg,woff,ttf}", lambda route: route.abort())

            # 短縮URLの解決
            if "amzn.asia" in target_url:
                try:
                    logger.info(f": 短縮URLを解決中... {target_url}")
                    # HEADではなくGETを使い、headersで「ブラウザのふり」をするのがコツ
                    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
                    response = requests.get(target_url, allow_redirects=True, timeout=10, headers=headers)
                    
                    # 最終的なURLを取得
                    target_url = response.url 
                    logger.info(f": 解決後のURL -> {target_url}")
                except Exception as e:
                    logger.error(f"短縮URL解決エラー: {e}")

            # 2. 解決後のURLからASINを抜き出す
            asin_match = re.search(r'/(?:dp|gp/product)/([A-Z0-9]{10})', target_url)
            if asin_match:
                asin = asin_match.group(1)
                logger.info(f": 特定されたASIN -> {asin}")

            # 価格取得
            if asin:
                title, amz_price = get_product_info(page, asin)
            
            browser_instance.close()
    except Exception as e:
        logger.error(f"CRITICAL Playwright Error: {e}")
                amz_price = None

    # 3. 結果の処理（DB保存 ＆ 楽天検索）
    if amz_price:
        # 楽天での検索・比較
        search_keyword = extract_keyword_smart(title)
        # search_keyword = extract_product_name_by_gemini(title)
        rakuten_price, rakuten_url = get_rakuten_lowest_price(search_keyword, amz_price)
        
        # DB保存（新しい設計のテーブルへ）
        try:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("""
                INSERT OR REPLACE INTO users 
                (user_id, asin, title, amz_price, rakuten_price, rakuten_url) 
                VALUES (?, ?, ?, ?, ?, ?)
            """, (user_id, asin, title, amz_price, rakuten_price, rakuten_url))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f" [DB Error]: {e}")

        amz_aff_url = f"https://www.amazon.co.jp/dp/{asin}?tag={AMAZPN_AFF_ID}"
        
        # --- メッセージ組み立て ---
        msg = f"✅ 登録完了：{search_keyword}\n\n"
        msg += f"📦 Amazon: {amz_price:,}円\n{amz_aff_url}\n\n"
        
        if rakuten_price:
            msg += f"🔴 楽天最安(修正中): {rakuten_price:,}円\n{rakuten_url}\n\n"
        else:
            msg += "※楽天では見つかりませんでした。\n\n"
            
        msg += "監視リストに追加しました！値下がりしたら通知するね✨"

        # --- LINE送信処理（テキストのみ） ---
        with ApiClient(configuration) as api_client:
            api = MessagingApi(api_client)
            api.push_message(
                PushMessageRequest(
                    to=user_id,
                    messages=[
                        TextMessage(text=msg)
                    ]
                )
            )
    else:
        # 失敗した場合も Push で通知
        line_helper.push_text(user_id, "ごめん、Amazonの価格がうまく読み取れなかった。URLが正しいか確認してもう一度送ってみてね。")

    return
if __name__ == "__main__":
    # debug=True にすると、エラー内容がコンソールに詳しく表示されます
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)