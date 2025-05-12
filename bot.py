import datetime
import os
import sqlite3

import discord
import google.generativeai as genai
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()  # .envファイルから環境変数を読み込む
TOKEN = os.getenv("DISCORD_BOT_TOKEN")

target_channel_ids_str = os.getenv("TARGET_CHANNEL_IDS", "")
TARGET_CHANNEL_IDS = {
    int(cid.strip())
    for cid in target_channel_ids_str.split(",")
    if cid.strip().isdigit()
}

intents = discord.Intents.default()
intents.messages = True  # メッセージ関連のイベントを処理するために必要
intents.message_content = True  # メッセージ内容を読み取るために必要

bot = commands.Bot(
    command_prefix="!", intents=intents
)  # コマンドのプレフィックスを'!'に設定


@bot.event
async def on_ready():
    print(f"{bot.user.name} がDiscordに接続しました！")
    print("------")
    initialize_chat_session()


@bot.command(name="ping")
async def ping(ctx):
    await ctx.send("pong!")


# メンションされた時だけ反応する (コマンドではないメンションへの応答)
@bot.event
async def on_message(message):
    if message.author == bot.user:  # Bot自身のメッセージは無視
        return

    if not shared_chat_session:
        await message.channel.send(
            "ボットのチャット機能が準備中です。少し待ってからもう一度お試しください。"
        )
        return

    # 特定チャンネルでの応答判定
    is_target_channel = message.channel.id in TARGET_CHANNEL_IDS

    # その他のチャンネルでのメンション判定
    is_mentioned = bot.user.mentioned_in(message)

    # 応答処理を実行するかどうかのフラグ
    should_respond = False

    if is_target_channel:
        should_respond = True
    elif is_mentioned:
        should_respond = True
    else:
        pass

    if should_respond:
        author_name = message.author.display_name
        user_input = message.content
        if is_mentioned:
            # メンションを取り除く
            user_input = user_input.replace(bot.user.mention, "").strip()
        async with message.channel.typing():
            bot_reply = await handle_shared_discord_message(author_name, user_input)
        # メンションを付与
        bot_reply = f"{message.author.mention} {bot_reply}"
        await message.channel.send(bot_reply)


initial_prompt_parts = [
    {
        "role": "user",
        "parts": [
            {
                "text": (
                    "あなたは非常に丁寧で、古風な言葉遣いをする執事です。"
                    "ユーザー様に対して常に敬意を払い、落ち着いたトーンで応答してください。"
                    "一人称は「私（わたくし）」、二人称は「ご主人様」を使用してください。"
                    "例：「ご主人様、何か御用でしょうか？」"
                    "ユーザーの発言にはユーザー名が付与されています（例：「ユーザーA: こんにちは」）。"
                    "応答の際には、誰のどの発言に対して応答しているのかを意識してください。"
                )
            }
        ],
    },
    {
        "role": "model",
        "parts": [
            {"text": "かしこまりました、ご主人様。私に何なりとお申し付けください。"}
        ],
    },
]


# --- グローバルなChatSession (メモリキャッシュとして) ---
# スクリプトが再起動されると失われるため、ファイル保存と組み合わせる
shared_chat_session = None
MODEL_NAME = "gemini-2.0-flash"
HISTORY_FILE = "shared_chat_history.json"  # 全ての会話をこの単一ファイルに保存
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
genai.configure(api_key=GOOGLE_API_KEY)
gemini_model = None  # モデルオブジェクトもグローバルに保持


DB_FILE = "chat_history.db"


# --- 1. datetime アダプタとコンバータの定義と登録 ---
def adapt_datetime_iso(dt_obj):
    """datetime.datetime オブジェクトをISO 8601形式の文字列に変換するアダプタ"""
    return dt_obj.isoformat()


def convert_iso_to_datetime(iso_str_bytes):
    """ISO 8601形式の文字列 (bytes型) をdatetime.datetime オブジェクトに変換するコンバータ"""
    # DBから読み取られる値はbytes型なので、適切なエンコーディングでstr型にデコードする
    return datetime.datetime.fromisoformat(iso_str_bytes.decode("utf-8"))


def get_db_connection():
    # detect_types パラメータを設定して、登録したコンバータが機能するようにする
    conn = sqlite3.connect(
        DB_FILE, detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES
    )
    conn.row_factory = sqlite3.Row  # カラム名でアクセスできるようにする
    return conn


# sqlite3モジュールにアダプタを登録: Pythonのdatetime.datetime型を上記関数で変換
sqlite3.register_adapter(datetime.datetime, adapt_datetime_iso)

# sqlite3モジュールにコンバータを登録: DBの "datetime" 型 (宣言) の値を上記関数で変換
# "datetime" はテーブル作成時の型宣言 (TIMESTAMP や DATETIME) に対応
sqlite3.register_converter(
    "datetime", convert_iso_to_datetime
)  # テーブルの型宣言に合わせる
sqlite3.register_converter(
    "timestamp", convert_iso_to_datetime
)  # TIMESTAMP型も同様に扱う場合


def create_table_if_not_exists():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
    CREATE TABLE IF NOT EXISTS conversation_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        role TEXT NOT NULL,
        author_name TEXT,
        content TEXT NOT NULL,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """
    )
    conn.commit()
    conn.close()


def add_message_to_db(role, author_name, content):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
    INSERT INTO conversation_history (role, author_name, content, timestamp)
    VALUES (?, ?, ?, ?)
    """,
        (role, author_name, content, datetime.datetime.now()),
    )
    conn.commit()
    conn.close()


def load_history_from_db(limit=100):  # 例: 直近100件のやり取りを読み込む
    conn = get_db_connection()
    cursor = conn.cursor()
    # timestampの降順で最新N件を取得し、それをさらに昇順に並べ替える
    # (SQLiteではサブクエリやウィンドウ関数が使えるが、シンプルに全件取得してPython側でハンドリングも可)
    # ここではシンプルに最新N件のメッセージを取得（userとmodelそれぞれを1件と数える）
    cursor.execute(
        """
    SELECT role, author_name, content FROM (
        SELECT role, author_name, content, timestamp
        FROM conversation_history
        ORDER BY timestamp DESC
        LIMIT ?
    ) ORDER BY timestamp ASC
    """,
        (limit,),
    )
    rows = cursor.fetchall()
    conn.close()

    history_for_model = []
    history_for_model.extend(initial_prompt_parts)
    if not rows:  # DBに履歴がない場合
        # 初期人格設定プロンプトをここで生成
        print("DBに履歴がなかったため、初期人格設定プロンプトを使用します。")
    else:
        for row in rows:
            # DBのcontentには既に "ユーザー名: メッセージ" の形式で入っている想定
            # または、author_nameとcontentを組み合わせてGeminiに渡す形式にする
            # ここでは、DBのcontentをそのままtextとして使用
            text_content = row["content"]
            # Geminiに渡す際、ユーザー発言には発言者名を付与する運用の場合、
            # DB保存時にcontentに含めるか、ここで再構成するか選択
            # 例: if row['role'] == 'user': text_content = f"{row['author_name']}: {row['content']}"
            history_for_model.append(
                {"role": row["role"], "parts": [{"text": text_content}]}
            )
        print(f"DBから {len(rows)} 件の履歴を読み込みました。")

    return history_for_model


def initialize_chat_session():
    """
    ボット起動時に呼び出され、チャットセッションを初期化または復元する。
    """
    global shared_chat_session, gemini_model
    create_table_if_not_exists()  # DBテーブル作成

    if not gemini_model:
        gemini_model = genai.GenerativeModel(MODEL_NAME)

    # DBから履歴を読み込み (例: 直近50ペア = 100メッセージ)
    history_from_db = load_history_from_db(limit=100)
    shared_chat_session = gemini_model.start_chat(history=history_from_db)
    print("チャットセッションがDB履歴で初期化されました。")


async def handle_shared_discord_message(author_name, user_message_content):
    """
    Discordのメッセージを受け取り、Gemini APIに応答を生成させる (共有・効率化版)
    """
    global shared_chat_session
    if not shared_chat_session:
        # ボット起動時に初期化されているはずだが、念のため
        print("エラー: チャットセッションが初期化されていません。")
        initialize_chat_session()  # 強制的に初期化を試みる（本番では on_ready で行うべき）
        if not shared_chat_session:
            return "申し訳ありません、ボットのチャット機能が正しく起動していません。管理者にご連絡ください。"

    message_for_api = f"{author_name}: {user_message_content}"
    print(
        f"{author_name}: {user_message_content}"
    )  # Discord側にエコーバックされるので必須ではない
    add_message_to_db(role="user", author_name=author_name, content=message_for_api)

    try:
        MAX_HISTORY_LENGTH = (
            200  # 例: 直近200件のやり取り（user+modelで1件と数えるなら100ペア）
        )
        if len(shared_chat_session.history) > MAX_HISTORY_LENGTH:
            print(f"履歴が{MAX_HISTORY_LENGTH}件を超えたため、古いものから削除します。")
            # 先頭から (MAX_HISTORY_LENGTH - 目的の履歴長) 分だけ削除
            # 人格設定プロンプトを残したい場合は、それを考慮して削除件数や開始位置を調整
            num_to_delete = len(shared_chat_session.history) - MAX_HISTORY_LENGTH
            # 最初の2件(人格設定のuser/modelペア)を残す場合:
            if len(shared_chat_session.history) > 2:  # 人格設定プロンプトがある前提
                del shared_chat_session.history[2 : 2 + num_to_delete]

        # APIに送信。メモリ上のshared_chat_session.historyも更新される
        response = await shared_chat_session.send_message_async(message_for_api)
        bot_response_text = response.text

        # ボットの応答をDBに保存
        # ボットの応答にも人格設定で名前が付与されている前提
        add_message_to_db(role="model", author_name="bot", content=bot_response_text)

        # save_shared_chat_history() は呼び出さない
        print(bot_response_text)
        return bot_response_text

    except Exception as e:
        # エラー処理 (省略)
        print(f"Error during message handling: {e}")
        return "エラーが発生しました。"


bot.run(TOKEN)
