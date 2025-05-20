import datetime
import json
import os
import sqlite3

import discord
import pytz
from discord.ext import commands
from dotenv import load_dotenv
from google import genai
from google.genai.types import GenerateContentConfig, GoogleSearch, Part, Tool
from google.genai.errors import ServerError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

load_dotenv()  # .envファイルから環境変数を読み込む
TOKEN = os.getenv("DISCORD_BOT_TOKEN")

target_channel_ids_str = os.getenv("TARGET_CHANNEL_IDS", "")
TARGET_CHANNEL_IDS = {
    int(cid.strip())
    for cid in target_channel_ids_str.split(",")
    if cid.strip().isdigit()
}

MAX_DISCORD_MESSAGE_LENGTH = 2000  # Discord's message character limit

intents = discord.Intents.default()
intents.messages = True  # メッセージ関連のイベントを処理するために必要
intents.message_content = True  # メッセージ内容を読み取るために必要

bot = commands.Bot(
    command_prefix="!", intents=intents
)  # コマンドのプレフィックスを'!'に設定


@bot.command(name="resetchat")
@commands.has_permissions(administrator=True)  # 管理者権限が必要な場合
async def resetchat(ctx):
    """
    現在のキャラクターの会話履歴をリセットします（管理者限定）。
    """
    global shared_chat_session, active_character_key

    if active_character_key is None:
        await ctx.send("エラー：現在アクティブなキャラクターが設定されていません。")
        return

    table_name = get_history_table_name(active_character_key)
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # 履歴テーブルの存在チェック
        cursor.execute(
            f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table_name}';"
        )
        if cursor.fetchone():
            # テーブルが存在すれば履歴を削除
            cursor.execute(f"DELETE FROM {table_name}")
            conn.commit()
            print(f"テーブル {table_name} の会話履歴を削除しました。")
            # await ctx.send(f"現在のキャラクター「{active_character_display_name}」の会話履歴をリセットしました。", mention_author=False) # active_character_display_name が使えるなら
            await ctx.send(
                f"現在のキャラクター「{active_character_key}」の会話履歴をリセットしました。",
                mention_author=False,
            )
        else:
            # テーブルが存在しない場合はリセットする履歴がない
            print(
                f"警告：テーブル {table_name} が見つかりませんでした。リセットする履歴はありません。"
            )
            await ctx.send(
                f"現在のキャラクター「{active_character_key}」の会話履歴は存在しませんでした。リセットは不要です。",
                mention_author=False,
            )

        # メモリ上のセッションを再初期化
        # initialize_chat_session 関数が DB から履歴を読み込む際、
        # 上記で削除したため履歴なしでセッションが開始されます。
        initialize_chat_session(active_character_key)
        print("チャットセッションを再初期化しました。")

    except sqlite3.Error as e:
        print(f"データベースエラーが発生しました: {e}")
        await ctx.send(
            f"履歴のリセット中にデータベースエラーが発生しました。",
            mention_author=False,
        )
    except Exception as e:
        print(f"予期せぬエラーが発生しました: {e}")
        await ctx.send(
            f"履歴のリセット中にエラーが発生しました。", mention_author=False
        )
    finally:
        if conn:
            conn.close()


@resetchat.error
async def resetchat_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("このコマンドを実行する権限がありません。", mention_author=False)
    else:
        # その他のエラーはコンソールに出力するなど
        print(f"コマンドエラー: {error}")
        await ctx.send("コマンド実行中にエラーが発生しました。", mention_author=False)


@bot.command(name="setchar")
@commands.has_permissions(administrator=True)  # 例: 管理者のみ変更可能
async def setchar_command(ctx, char_key: str):
    """
    ボットのキャラクターを変更します（管理者限定）。
    使用法: !setchar <キャラクターキー>
    """
    # 利用可能なキャラクターかチェック (PROMPT_DIR内のファイル名リストと比較など)
    available_chars = [
        f.split(".")[0] for f in os.listdir(PROMPT_DIR) if f.endswith(".json")
    ]
    if char_key in available_chars:
        try:
            initialize_chat_session(char_key)  # 新しいキャラでセッション再初期化
            # active_character_display_name が更新されていることを利用
            await ctx.send(
                f"キャラクターを「{active_character_display_name}」に変更しました。",
                mention_author=False,
            )
        except Exception as e:
            await ctx.send(
                f"キャラクター変更中にエラーが発生しました: {e}",
                mention_author=False,
            )
    else:
        await ctx.send(
            f"指定されたキャラクター「{char_key}」は見つかりません。",
            mention_author=False,
        )


@setchar_command.error
async def setchar_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("キャラクターを変更する権限がありません。", mention_author=False)
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(
            "キャラクターキーを指定してください。\n使用法: `!setchar <キャラクターキー>`",
            mention_author=False,
        )
    else:
        print(f"setchar コマンドエラー: {error}")
        await ctx.send("コマンド実行中にエラーが発生しました。", mention_author=False)


# --- 利用可能なキャラクター一覧を表示するコマンド ---
@bot.command(name="listchars")
async def listchars_command(ctx):
    """
    利用可能なキャラクターの一覧を表示します。
    使用法: !listchars
    """
    available_chars_info = []
    # os.listdir(PROMPT_DIR) が存在するかどうかのチェックを追加するとより安全
    if not os.path.exists(PROMPT_DIR):
        await ctx.send(
            f"キャラクター設定ディレクトリ `{PROMPT_DIR}` が見つかりません。",
            mention_author=False,
        )
        return

    for f_name in os.listdir(PROMPT_DIR):
        if f_name.endswith(".json"):
            char_key = f_name.split(".")[0]
            # 簡単な説明などをJSONから読み込んで表示するのも良い
            # load_character_definition は初期プロンプトも読むので、表示名だけなら別のヘルパー関数が良いかも
            # または、ここではファイル名キーと表示名のみを表示する
            try:
                _, display_name = load_character_definition(
                    char_key
                )  # 表示名取得のため一時的に読み込み
                available_chars_info.append(
                    f"- `{char_key}` ({display_name}) {'(現在使用中)' if active_character_key == char_key else ''}"
                )
            except Exception as e:
                print(f"キャラクター情報読み込みエラー ({char_key}): {e}")
                available_chars_info.append(f"- `{char_key}` (情報の読み込みに失敗)")

    if available_chars_info:
        await ctx.send(
            "利用可能なキャラクター:\n" + "\n".join(available_chars_info),
            mention_author=False,
        )
    else:
        await ctx.send(
            "利用可能なキャラクター設定ファイルが見つかりません。",
            mention_author=False,
        )


@bot.event
async def on_ready():
    print(f"{bot.user.name} がDiscordに接続しました！")
    print("------")
    initialize_chat_session()


@bot.event
async def on_message(message):
    if message.author == bot.user:  # Bot自身のメッセージは無視
        return

    # コマンドとして処理を試みる
    # もしこのメッセージがコマンドとして認識され、処理が成功または失敗した場合、
    # ctx.command は None 以外になります。
    await bot.process_commands(message)

    # コマンドとして処理されたメッセージ（プレフィックスで始まるメッセージ）であれば、
    # ここで on_message のそれ以降の処理を終了します。
    # ctx.command が None でないこと、または単純にプレフィックスで始まるかで判定します。
    # 単純にプレフィックスで始まるかで判定する方が、未定義コマンドへのAI応答も防げるので推奨です。
    if message.content.startswith(bot.command_prefix):
        print(
            f"コマンドメッセージを検出しました: {message.content[:50]}..."
        )  # デバッグ用
        return  # コマンドとして処理されたので、通常のメッセージ処理は行わない

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

    image_contents = []
    if message.attachments:
        print(
            f"画像付きメッセージを受信しました from {message.author.display_name} in channel {message.channel.name}"
        )

        # 応答を生成するかどうかの基本的なフラグ（テキスト応答のロジックとは別に判定しても良い）
        # 例えば、画像付きメッセージの場合はメンションの有無にかかわらず常に画像を処理するなど
        process_image_message = True  # 画像付きメッセージは常に処理すると仮定

        if process_image_message:
            author_name = message.author.display_name

            # ボットが処理中であることを示す（タイピング表示）
            async with message.channel.typing():
                # 添付ファイルごとに処理（複数の画像がある場合）
                for attachment in message.attachments:
                    # 添付ファイルが画像であることを確認 (MIMEタイプをチェック)
                    if attachment.content_type and attachment.content_type.startswith(
                        "image/"
                    ):
                        try:
                            # 画像データをダウンロード（非同期）
                            image_data_bytes = await attachment.read()
                            print(
                                f"画像をダウンロードしました: {attachment.filename} ({attachment.content_type})"
                            )

                            # Gemini APIに渡す入力コンテンツを準備
                            # テキストと画像を組み合わせてリストとして渡します。
                            # google-generativeai ライブラリは、bytes と MIMEタイプから Part オブジェクトへの変換を内部で行うか、
                            # generate_content / send_message にそのまま渡せるように設計されています。

                            # 画像データを Part オブジェクト形式に変換して追加
                            # Part.from_bytes を使うのが明示的で推奨
                            image_part = Part.from_bytes(
                                data=image_data_bytes, mime_type=attachment.content_type
                            )
                            image_contents.append(image_part)

                        except Exception as e:
                            print(
                                f"画像処理またはGemini API呼び出し中にエラーが発生しました: {e}"
                            )
                            # APIエラーの詳細をログに出力することも重要
                            if hasattr(e, "response") and hasattr(
                                e.response, "prompt_feedback"
                            ):
                                print(f"API Feedback: {e.response.prompt_feedback}")
                            await message.reply(
                                f"画像の処理中にエラーが発生しました。",
                                mention_author=False,
                            )

    # ★★★ ここまで画像添付ファイルの処理 ★★★

    if should_respond:
        author_name = message.author.display_name
        user_input = message.content
        if is_mentioned:
            # メンションを取り除く
            user_input = user_input.replace(bot.user.mention, "").strip()
        async with message.channel.typing():
            bot_reply = await handle_shared_discord_message(
                author_name, user_input, image_contents
            )

        if bot_reply and bot_reply.strip():  # Ensure there's non-whitespace content
            if len(bot_reply) <= MAX_DISCORD_MESSAGE_LENGTH:
                await message.reply(bot_reply, mention_author=False)
            else:
                # Split the message into chunks
                parts = []
                for i in range(0, len(bot_reply), MAX_DISCORD_MESSAGE_LENGTH):
                    parts.append(bot_reply[i : i + MAX_DISCORD_MESSAGE_LENGTH])

                first_message_sent = False
                for part_content in parts:
                    if (
                        part_content.strip()
                    ):  # Don't send empty or whitespace-only messages
                        if not first_message_sent:
                            await message.reply(part_content, mention_author=False)
                            first_message_sent = True
                        else:
                            # Send subsequent parts as new messages in the channel
                            await message.channel.send(part_content)
        else:
            print(
                f"Warning: Bot generated an empty or whitespace-only reply for user input: '{user_input}'"
            )


# --- グローバルなChatSession (メモリキャッシュとして) ---
# スクリプトが再起動されると失われるため、ファイル保存と組み合わせる
shared_chat_session = None
initial_prompts_count = 0  # Tracks the number of initial prompts for history pruning
MODEL_NAME = "gemini-2.0-flash"
HISTORY_FILE = "shared_chat_history.json"  # 全ての会話をこの単一ファイルに保存
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
client = genai.Client(api_key=GOOGLE_API_KEY)
google_search_tool = Tool(google_search=GoogleSearch())
chat_config = GenerateContentConfig(
    tools=[google_search_tool],
    response_modalities=["TEXT"],
    frequency_penalty=1.0,
    temperature=0.3,
)


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
    system_instruction_user += "ユーザーの発言には改行区切りで発言時間、ユーザー名、発言内容が付与されています。\n発言の例\n時間\nユーザーA\nこんにちは\n\n応答の際には、誰のどの発言に対して応答しているのかを意識して、応答内容に含めるときはこの付与されたユーザー名を取り除いてから応答してください。また、会話の時間も意識してください。また、ユーザーの入力した発言時間、ユーザー名の内容を回答の最初に入れることは絶対に避けてください。ユーザーの発言内容を理解した上で、必ずあなた自身の言葉で応答してください。ユーザーの話し方に安易に影響されないようにしてください。同じ文字やフレーズの極端な繰り返しを避け、簡潔で多様な表現を心がけてください。不自然に長い同じ文字の羅列は避けてください。次に詳細なキャラクター設定を示しますので、そのキャラになりきってメタ的な発言を避けるようにしてください。"
    system_instruction_user += main_char_data.get("character_metadata", "")
    # example_dialogues は system_instruction ではなく、会話履歴の例として final_initial_prompts に追加します。
    example_dialogues_list = main_char_data.get(
        "example_dialogues", []
    )  # JSON側のキー名に合わせる
    initial_model_response = main_char_data.get("initial_model_response", "")
    conversation_examples_list = main_char_data.get("conversation_examples", [])

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

    # Add example dialogues from the new field, paired with generic user turns
    for dialogue_string in example_dialogues_list:
        # Add a generic user turn before each example model dialogue to maintain history structure
        final_initial_prompts.append(
            {"role": "user", "parts": [{"text": "..."}]}
        )  # Using "..." as a placeholder
        final_initial_prompts.append(
            {"role": "model", "parts": [{"text": dialogue_string}]}
        )

    for example_message in conversation_examples_list:
        # 各要素がChatSessionのhistoryとして有効な構造か、簡単な検証を行うとより安全
        if (
            isinstance(example_message, dict)
            and example_message.get("role") in ["user", "model"]
            and isinstance(example_message.get("parts"), list)
        ):
            final_initial_prompts.append(example_message)
        else:
            print(
                f"警告: キャラクター「{display_name}」の conversation_examples 内の要素の構造が不正です: {example_message}"
            )
            # 不正な要素はスキップ
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


def _create_chat_session(history: list):
    """Helper function to create a new chat session."""
    global shared_chat_session
    shared_chat_session = client.chats.create(
        model=MODEL_NAME, history=history, config=chat_config
    )


def initialize_chat_session(character_key_to_load=None):
    """
    ボット起動時に呼び出され、チャットセッションを初期化または復元する。
    """
    global shared_chat_session, gemini_model, active_character_key, active_character_display_name, initial_prompts_count

    if character_key_to_load is None:
        character_key_to_load = get_setting_from_db("current_character_key", "lycaon")

    initial_character_prompts, display_name = load_character_definition(
        character_key_to_load
    )
    initial_prompts_count = len(initial_character_prompts)  # 初期プロンプトの数を保存
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

    # DBから履歴を読み込み (例: 直近50ペア = 100メッセージ)
    history_from_db = load_history_from_db(limit=100)

    # 4. 最終的な履歴を作成: (キャラクタープロンプト + DBからの会話履歴)
    final_history_for_session = initial_character_prompts + history_from_db
    _create_chat_session(history=final_history_for_session)
    set_setting_in_db(
        "current_character_key", character_key_to_load
    )  # 現在のキャラをDBに保存
    print(
        f"チャットセッションがキャラクター「{active_character_display_name}」とDB履歴で初期化されました。"
    )


# 東京のタイムゾーンを設定
tokyo_tz = pytz.timezone("Asia/Tokyo")


def get_current_time_japan():
    """日本標準時 (JST) の現在の日時を取得し、指定フォーマットの文字列で返す"""
    now_tokyo = datetime.datetime.now(tokyo_tz)
    # AIに分かりやすいフォーマットで返します。必要に応じて調整してください。
    return now_tokyo.strftime(
        "%Y年%m月%d日 (%A) %H時%M分%S秒 JST"
    )  # 例: 2025年05月17日 (金曜日) 22時12分30秒 JST


# Gemini API呼び出しにリトライを適用するヘルパー関数
@retry(
    stop=stop_after_attempt(5),  # 最大5回試行 (初回 + 4回リトライ)
    wait=wait_exponential(
        multiplier=1, min=4, max=30
    ),  # 最小4秒、その後8秒、16秒と指数関数的に増加し、最大30秒まで待機
    retry=retry_if_exception_type(ServerError),
)
def _send_message_with_retry(chat_session, contents):
    """
    Gemini ChatSessionのsend_messageをリトライ付きで実行するヘルパー関数。
    """
    # print("Gemini APIにメッセージを送信中...")
    try:
        response = chat_session.send_message(contents)
        # print("Gemini APIからの応答を受信しました。")
        return response
    except ServerError as e:
        print(
            f"Gemini APIでServiceUnavailableエラーが発生しました。リトライします: {e}"
        )
        raise  # tenacityがこの例外を捕捉してリトライを処理します
    except Exception as e:
        print(f"Gemini API呼び出し中に予期せぬエラーが発生しました: {e}")
        raise  # その他のエラーはリトライせずそのまま送出

    # より簡潔な形式でも良い: "%Y/%m/%d %H:%M"


async def handle_shared_discord_message(
    author_name, user_message_content, image_contents=None
):
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

    current_time_str = get_current_time_japan()
    message_for_api = f"{current_time_str}\n{author_name}\n{user_message_content}"
    print(
        f"{author_name}: {user_message_content}"
    )  # Discord側にエコーバックされるので必須ではない
    add_message_to_db(role="user", author_name=author_name, content=message_for_api)

    try:
        MAX_HISTORY_LENGTH = 200  # 履歴内の最大メッセージ数 (初期プロンプト + 会話)

        # Chatオブジェクトから現在の履歴を取得 (curated=True でモデルに送信される履歴を取得)
        current_history_list = shared_chat_session.get_history(curated=True)

        if len(current_history_list) > MAX_HISTORY_LENGTH:
            print(
                f"現在の履歴長 ({len(current_history_list)}) が最大長 ({MAX_HISTORY_LENGTH}) を超えたため、履歴を整理します。"
            )

            pruned_history: list

            if initial_prompts_count >= MAX_HISTORY_LENGTH:
                # MAX_HISTORY_LENGTH が初期プロンプト数よりも小さいか等しい場合、
                # 初期プロンプトの先頭 MAX_HISTORY_LENGTH 件のみを保持
                pruned_history = current_history_list[:MAX_HISTORY_LENGTH]
                print(
                    f"警告: MAX_HISTORY_LENGTH ({MAX_HISTORY_LENGTH}) が初期プロンプト数 ({initial_prompts_count}) 以下です。履歴は初期プロンプトの先頭 {len(pruned_history)} 件に切り詰められます。"
                )
            else:
                # 初期プロンプトは全て保持
                initial_prompts_part = current_history_list[:initial_prompts_count]

                # 会話部分の履歴を取得
                conversational_part = current_history_list[initial_prompts_count:]

                # 保持する会話メッセージの数を計算
                num_conversational_to_keep = MAX_HISTORY_LENGTH - initial_prompts_count

                if len(conversational_part) > num_conversational_to_keep:
                    # 会話部分が長すぎる場合、末尾から指定件数だけ残す (古いものを削除)
                    pruned_conversational_part = conversational_part[
                        -num_conversational_to_keep:
                    ]
                    print(
                        f"古い会話履歴から {len(conversational_part) - len(pruned_conversational_part)} 件を削除しました。初期プロンプト {initial_prompts_count} 件は保持されます。"
                    )
                else:
                    # 会話部分が指定件数以下ならそのまま使用
                    pruned_conversational_part = conversational_part

                pruned_history = initial_prompts_part + pruned_conversational_part

            # ChatSessionを新しい履歴で再生成
            _create_chat_session(history=pruned_history)
            print(
                f"チャットセッションを新しい履歴 (計{len(pruned_history)}件) で再構築しました。"
            )

        send_contents = [message_for_api]
        if image_contents:
            for image_part in image_contents:
                send_contents.append(image_part)
        # APIに送信。メモリ上のshared_chat_session.historyも更新される
        response = _send_message_with_retry(shared_chat_session, send_contents)
        bot_response_text = response.text

        # ボットの応答をDBに保存
        # ボットの応答にも人格設定で名前が付与されている前提
        add_message_to_db(role="model", author_name="bot", content=bot_response_text)

        # save_shared_chat_history() は呼び出さない
        print(bot_response_text)
        return bot_response_text

    except Exception as e:
        print(f"Error during message handling: {e}")
        return "エラーが発生しました。"


bot.run(TOKEN)
