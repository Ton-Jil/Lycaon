import datetime
import json
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

    if message.content.startswith("!setchar "):
        if message.author.guild_permissions.administrator:  # 例: 管理者のみ変更可能
            char_key = message.content.split(" ", 1)[1].strip()
            # 利用可能なキャラクターかチェック (PROMPT_DIR内のファイル名リストと比較など)
            available_chars = [
                f.split(".")[0] for f in os.listdir(PROMPT_DIR) if f.endswith(".json")
            ]
            if char_key in available_chars:
                try:
                    initialize_chat_session(
                        char_key
                    )  # 新しいキャラでセッション再初期化
                    await message.reply(
                        f"キャラクターを「{active_character_display_name}」に変更しました。",
                        mention_author=False,
                    )
                except Exception as e:
                    await message.reply(
                        f"キャラクター変更中にエラーが発生しました: {e}",
                        mention_author=False,
                    )
            else:
                await message.reply(
                    f"指定されたキャラクター「{char_key}」は見つかりません。",
                    mention_author=False,
                )
        else:
            await message.reply(
                "キャラクターを変更する権限がありません。", mention_author=False
            )
        return  # コマンド処理後は通常の会話応答をしない

    if message.content == "!listchars":
        available_chars_info = []
        for f_name in os.listdir(PROMPT_DIR):
            if f_name.endswith(".json"):
                char_key = f_name.split(".")[0]
                # 簡単な説明などをJSONから読み込んで表示するのも良い
                _, display_name = load_character_definition(
                    char_key
                )  # 表示名取得のため
                available_chars_info.append(f"- `{char_key}` ({display_name})")
        if available_chars_info:
            await message.reply(
                "利用可能なキャラクター:\n" + "\n".join(available_chars_info),
                mention_author=False,
            )
        else:
            await message.reply(
                "利用可能なキャラクター設定ファイルが見つかりません。",
                mention_author=False,
            )
        return

    if should_respond:
        author_name = message.author.display_name
        user_input = message.content
        if is_mentioned:
            # メンションを取り除く
            user_input = user_input.replace(bot.user.mention, "").strip()
        async with message.channel.typing():
            bot_reply = await handle_shared_discord_message(author_name, user_input)
        # 返信で応答
        await message.reply(bot_reply, mention_author=False)


# --- グローバルなChatSession (メモリキャッシュとして) ---
# スクリプトが再起動されると失われるため、ファイル保存と組み合わせる
shared_chat_session = None
MODEL_NAME = "gemini-2.0-flash"
HISTORY_FILE = "shared_chat_history.json"  # 全ての会話をこの単一ファイルに保存
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
genai.configure(api_key=GOOGLE_API_KEY)
gemini_model = None  # モデルオブジェクトもグローバルに保持


DB_FILE = "chat_history.db"


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


def get_history_table_name(character_key):
    # キャラクターキーから安全なテーブル名を生成
    # ここでは簡易的にキーのプレフィックスとするが、より厳密な検証が必要な場合がある
    if (
        not isinstance(character_key, str) or not character_key.isalnum()
    ):  # 例: 英数字のみを許可
        print(f"警告: 不正なキャラクターキーが指定されました: {character_key}")
        # 不正なキーの場合はデフォルトやエラーを示すテーブル名を返す
        return "history_default_invalid"
    return f"history_{character_key}"


def create_table_if_not_exists():
    global active_character_key

    if active_character_key is None:
        raise ValueError(
            "アクティブなキャラクターキーが設定されていません。テーブル名決定できません。"
        )

    table_name = get_history_table_name(active_character_key)

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        f"""
    CREATE TABLE IF NOT EXISTS {table_name} (
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
    global active_character_key

    if active_character_key is None:
        raise ValueError(
            "アクティブなキャラクターキーが設定されていません。メッセージ保存できません。"
        )

    table_name = get_history_table_name(active_character_key)

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        f"""
    INSERT INTO {table_name} (role, author_name, content, timestamp)
    VALUES (?, ?, ?, ?)
    """,
        (role, author_name, content, datetime.datetime.now()),
    )
    conn.commit()
    conn.close()


PROMPT_DIR = "character_prompts"


def _load_raw_character_data(character_filename_key):
    """指定されたキーのキャラクターデータをJSONファイルからそのまま読み込むヘルパー関数"""
    prompt_file_path = os.path.join(PROMPT_DIR, f"{character_filename_key}.json")
    if not os.path.exists(prompt_file_path):
        print(f"警告: キャラクターデータファイルが見つかりません: {prompt_file_path}")
        return None
    try:
        with open(prompt_file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data
    except Exception as e:
        print(
            f"エラー: キャラクターデータファイルの読み込み/解析に失敗 ({prompt_file_path}): {e}"
        )
        return None


def load_character_definition(main_character_key, processed_relations=None):
    """
    指定されたキー (ファイル名から拡張子を除いたもの) に基づいて
    キャラクタープロンプトファイルを読み込み、初期履歴と表示名を返す。
    """
    if processed_relations is None:
        processed_relations = set()

    if main_character_key in processed_relations:
        return [], main_character_key

    processed_relations.add(main_character_key)

    main_char_data = _load_raw_character_data(main_character_key)
    if not main_char_data:
        return [], main_character_key

    display_name = main_char_data.get("character_name_display", main_character_key)
    system_instruction_user = main_char_data.get(
        "system_instruction_user", ""
    )  # メインキャラの基本指示
    system_instruction_user += "ユーザーの発言にはユーザー名が付与されています（例：「ユーザーA: こんにちは」）。応答の際には、誰のどの発言に対して応答しているのかを意識してください。次に詳細なキャラクター設定を示しますので、そのキャラになりきってメタ的な発言を避けるようにしてください。"
    system_instruction_user += main_char_data.get("character_metadata", "")
    initial_model_response = main_char_data.get("initial_model_response", "")

    if not system_instruction_user or not initial_model_response:
        print(
            f"警告: メインキャラクター「{display_name}」のプロンプト基本情報が不完全です。"
        )

    # 周辺人物の基本情報の文字列を構築
    supplementary_related_info_parts = []
    if "related_characters" in main_char_data and isinstance(
        main_char_data["related_characters"], list
    ):
        related_character_keys = main_char_data["related_characters"]
        if related_character_keys:  # リストが空でない場合
            supplementary_related_info_parts.append(
                "\n\n--- 参考: あなたと関わりのある人物の詳細情報 ---"
            )
            for related_key in related_character_keys:
                if (
                    isinstance(related_key, str)
                    and related_key not in processed_relations
                ):
                    related_data = _load_raw_character_data(related_key)
                    if related_data:
                        related_display_name = related_data.get(
                            "character_name_display", related_key
                        )
                        related_description = related_data.get(
                            "character_metadata", "特に公表されている説明はありません。"
                        )  # 短い説明

                        info_line = (
                            f"\n- {related_display_name} ({related_description})"
                        )
                        supplementary_related_info_parts.append(info_line)

    # メインキャラクターのシステムプロンプトに、抽出した周辺人物の基本情報を「参考情報」として追記
    if (
        len(supplementary_related_info_parts) > 1
    ):  # ヘッダー行があるので1より大きいかで判定
        system_instruction_user += "".join(supplementary_related_info_parts)
        # メインプロンプト内で関係性を記述してもらうことを促す一文は、
        # メインの system_instruction_user 自体に含めてもらう方が自然かもしれません。
        # 例: 「あなたは以下の人物たちのことも知っています。彼らとの関係性はあなたの設定に基づきます。」
        # system_instruction_user += "\n上記はあなたが知っている人物のリストです。彼らとの具体的な関係性やあなたの考えは、あなたの基本設定に基づいて判断してください。"

    final_initial_prompts = [
        {"role": "user", "parts": [{"text": system_instruction_user}]},
        {"role": "model", "parts": [{"text": initial_model_response}]},
    ]
    # print(f"キャラクター「{display_name}」（関連人物の参考情報含む）のプロンプトを構築しました。")
    # print(f"最終システムプロンプト:\n{system_instruction_user}")  # デバッグ用
    return final_initial_prompts, display_name


def get_setting_from_db(key, default_value=None):
    conn = get_db_connection()  # 既存のDB接続関数
    cursor = conn.cursor()
    cursor.execute(
        "CREATE TABLE IF NOT EXISTS bot_settings (key TEXT PRIMARY KEY, value TEXT)"
    )
    conn.commit()
    cursor.execute("SELECT value FROM bot_settings WHERE key = ?", (key,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else default_value


def set_setting_in_db(key, value):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "CREATE TABLE IF NOT EXISTS bot_settings (key TEXT PRIMARY KEY, value TEXT)"
    )
    # 存在すれば更新、しなければ挿入
    cursor.execute(
        "INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)", (key, value)
    )
    conn.commit()
    conn.close()


def load_history_from_db(limit=100):  # 例: 直近100件のやり取りを読み込む
    global active_character_key

    if active_character_key is None:
        raise ValueError(
            "アクティブなキャラクターキーが設定されていません。履歴読み込みできません。"
        )

    table_name = get_history_table_name(active_character_key)

    conn = get_db_connection()
    cursor = conn.cursor()
    # timestampの降順で最新N件を取得し、それをさらに昇順に並べ替える
    # (SQLiteではサブクエリやウィンドウ関数が使えるが、シンプルに全件取得してPython側でハンドリングも可)
    # ここではシンプルに最新N件のメッセージを取得（userとmodelそれぞれを1件と数える）
    try:
        cursor.execute(
            f"""
        SELECT role, author_name, content FROM (
            SELECT role, author_name, content, timestamp
            FROM {table_name}
            ORDER BY timestamp DESC
            LIMIT ?
        ) ORDER BY timestamp ASC
        """,
            (limit,),
        )
        rows = cursor.fetchall()
        print(
            f"テーブル {table_name} から {len(rows)} 件の履歴を読み込みました。"
        )  # テーブル名を出力
    except sqlite3.OperationalError as e:
        # テーブルが存在しない場合などに発生するエラー
        print(
            f"警告: テーブル {table_name} が見つかりません。新しい履歴を開始します。エラー: {e}"
        )
        rows = []  # テーブルがない場合は履歴なしとして扱う
    conn.close()

    history_for_model = []
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


active_character_key = None
active_character_display_name = (
    "デフォルト"  # 現在のキャラクター表示名を保持するグローバル変数
)


def initialize_chat_session(character_key_to_load=None):
    """
    ボット起動時に呼び出され、チャットセッションを初期化または復元する。
    """
    global shared_chat_session, gemini_model, active_character_key, active_character_display_name

    if character_key_to_load is None:
        character_key_to_load = get_setting_from_db("current_character_key", "lycaon")

    initial_character_prompts, display_name = load_character_definition(
        character_key_to_load
    )
    active_character_key = character_key_to_load
    active_character_display_name = display_name  # グローバルな表示名を更新

    if not initial_character_prompts:
        print(
            f"警告: キャラクター「{character_key_to_load}」のプロンプトでセッションを開始できません。"
        )
        # 適切なフォールバック処理 (例: エラーを返す、非常にシンプルなデフォルトプロンプトを使うなど)
        # shared_chat_session = None # またはエラー状態を示す
        # return
        # ここでは、最も基本的なプロンプトなしセッションで開始する例（実際にはエラー処理した方が良い）
        initial_character_prompts = []

    create_table_if_not_exists()  # DBテーブル作成

    if not gemini_model:
        gemini_model = genai.GenerativeModel(MODEL_NAME)

    # DBから履歴を読み込み (例: 直近50ペア = 100メッセージ)
    history_from_db = load_history_from_db(limit=100)

    # 4. 最終的な履歴を作成: (キャラクタープロンプト + DBからの会話履歴)
    final_history_for_session = initial_character_prompts + history_from_db
    shared_chat_session = gemini_model.start_chat(history=final_history_for_session)
    set_setting_in_db(
        "current_character_key", character_key_to_load
    )  # 現在のキャラをDBに保存
    print(
        f"チャットセッションがキャラクター「{active_character_display_name}」とDB履歴で初期化されました。"
    )


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
