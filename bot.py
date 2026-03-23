import datetime
import json
import os
import sqlite3
import subprocess
from typing import List

import discord
import pytz
from discord.ext import commands, tasks
from dotenv import load_dotenv
from google import genai
from google.genai.types import (
    GenerateContentConfig,
    GoogleSearch,
    UrlContext,
    Part,
    Tool,
    ThinkingConfig,
)
from google.genai.errors import ServerError
from tenacity import (
    retry,
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
WEATHER_LOCATION = os.getenv("WEATHER_LOCATION", "東京")

FALLBACK_MIME_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".webm": "audio/webm",
}

intents = discord.Intents.default()
intents.messages = True  # メッセージ関連のイベントを処理するために必要
intents.message_content = True  # メッセージ内容を読み取るために必要

bot = commands.Bot(
    command_prefix="!", intents=intents
)  # コマンドのプレフィックスを'!'に設定


def list_available_character_keys():
    """PROMPT_DIR から利用可能なキャラクターキーを取得する。"""
    if not os.path.exists(PROMPT_DIR):
        return []
    return sorted(
        f.split(".")[0] for f in os.listdir(PROMPT_DIR) if f.endswith(".json")
    )


def is_command_message(message: discord.Message) -> bool:
    return isinstance(message.content, str) and message.content.startswith(
        bot.command_prefix
    )


def should_respond_to_message(message: discord.Message) -> bool:
    is_target_channel = message.channel.id in TARGET_CHANNEL_IDS
    is_mentioned = bot.user.mentioned_in(message)
    return is_target_channel or is_mentioned


def build_user_input(message: discord.Message, is_mentioned: bool) -> str:
    user_input = message.content
    if is_mentioned:
        user_input = user_input.replace(bot.user.mention, "").strip()
    return user_input


async def extract_supported_attachment_parts(message: discord.Message) -> List[Part]:
    """サポート対象の添付ファイルを Part の配列に変換する。"""
    attachment_parts: List[Part] = []
    if not message.attachments:
        return attachment_parts

    print(
        f"添付ファイル付きメッセージを受信しました from {message.author.display_name} in channel {message.channel.name}"
    )

    for attachment in message.attachments:
        content_type = attachment.content_type or ""
        file_ext = os.path.splitext(attachment.filename.lower())[1]
        fallback_mime = FALLBACK_MIME_TYPES.get(file_ext)
        resolved_mime_type = content_type or fallback_mime

        is_supported_type = resolved_mime_type and (
            resolved_mime_type.startswith("image/")
            or resolved_mime_type.startswith("audio/")
        )

        if not is_supported_type:
            continue

        try:
            file_data_bytes = await attachment.read()
            print(
                f"添付ファイルをダウンロードしました: {attachment.filename} ({resolved_mime_type})"
            )
            attachment_parts.append(
                Part.from_bytes(data=file_data_bytes, mime_type=resolved_mime_type)
            )
        except Exception as e:
            print(f"添付ファイル処理中にエラーが発生しました: {e}")

    return attachment_parts


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
async def setchar_command(ctx, char_key: str):
    """
    ボットのキャラクターを変更します（管理者限定）。
    使用法: !setchar <キャラクターキー>
    """
    # 利用可能なキャラクターかチェック (PROMPT_DIR内のファイル名リストと比較など)
    available_chars = list_available_character_keys()
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
    if not os.path.exists(PROMPT_DIR):
        await ctx.send(
            f"キャラクター設定ディレクトリ `{PROMPT_DIR}` が見つかりません。",
            mention_author=False,
        )
        return

    available_keys = list_available_character_keys()
    if not available_keys:
        await ctx.send(
            "利用可能なキャラクター設定ファイルが見つかりません。",
            mention_author=False,
        )
        return

    for char_key in available_keys:
        try:
            _, _, display_name = load_character_definition(
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


@bot.command("talktome")
async def talktome_command(ctx):
    user = ctx.author.display_name
    talk_prompt = f"{user}との過去の会話を踏まえて、{user}との会話を再開するような発言をしてください。挨拶のみ発言することは避けてください。過去に自分が提案したことがある話題の繰り返しは避けるようにしてください。話題がない場合はキャラクター情報から会話のきっかけを考えてください。"
    async with ctx.channel.typing():
        response = _send_message_with_retry(shared_chat_session, [talk_prompt])
        bot_reply = response.text

    if bot_reply and bot_reply.strip():
        await ctx.reply(bot_reply, mention_author=False)
        add_message_to_db("user", "system", talk_prompt)
        add_message_to_db("model", "bot", bot_reply)


async def _announce_update_if_needed():
    """前回起動時と git commit hash が異なる場合、差分をキャラクター口調でアナウンスする。"""
    try:
        current_hash = (
            subprocess.check_output(
                ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL
            )
            .decode()
            .strip()
        )
    except Exception as e:
        print(f"アップデート検知: git コマンド失敗のためスキップします: {e}")
        return

    last_hash = get_setting_from_db("last_deployed_commit", None)
    set_setting_in_db("last_deployed_commit", current_hash)

    if last_hash is None:
        print(
            f"アップデート検知: 初回起動。コミットハッシュを記録しました: {current_hash[:7]}"
        )
        return

    if last_hash == current_hash:
        print("アップデート検知: コミットハッシュに変化なし。通知をスキップします。")
        return

    try:
        commit_log = (
            subprocess.check_output(
                ["git", "log", "--oneline", f"{last_hash}..{current_hash}"],
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )
    except Exception as e:
        print(f"アップデート検知: git log 取得失敗: {e}")
        commit_log = "(変更内容の取得に失敗しました)"

    if not commit_log:
        print("アップデート検知: 差分コミットなし。通知をスキップします。")
        return

    print(f"アップデート検知: {last_hash[:7]} → {current_hash[:7]}\n{commit_log}")

    if not shared_chat_session:
        print(
            "アップデート検知: チャットセッション未初期化のため通知をスキップします。"
        )
        return

    send_time_iso = datetime.datetime.now(pytz.timezone("Asia/Tokyo")).isoformat()
    update_prompt = (
        f"システム\n{send_time_iso}\n"
        f"ボットがアップデートされて再起動しました。以下の変更内容をキャラクターとしての口調で"
        f"Discordのみんなに自然にお知らせしてください。2000文字以内でまとめてください。\n\n"
        f"変更内容（git log）:\n{commit_log}"
    )

    try:
        response = _send_message_with_retry(shared_chat_session, [update_prompt])
        bot_reply = response.text
        if not bot_reply or not bot_reply.strip():
            return

        for channel_id in TARGET_CHANNEL_IDS:
            channel = bot.get_channel(channel_id)
            if channel:
                await channel.send(bot_reply)

    except Exception as e:
        print(f"アップデート通知中にエラーが発生しました: {e}")


@tasks.loop(
    time=datetime.time(
        hour=7,
        minute=0,
        second=0,
        tzinfo=datetime.timezone(datetime.timedelta(hours=9)),
    )
)
async def morning_weather_announcement():
    """毎朝7時(JST)に天気をキャラクターの口調でアナウンスする。"""
    global shared_chat_session

    if not shared_chat_session:
        print("朝の天気アナウンス: チャットセッションが未初期化のためスキップします。")
        return

    locations = [loc.strip() for loc in WEATHER_LOCATION.split(",") if loc.strip()]
    if len(locations) > 1:
        location_str = "・".join(locations)
        weather_prompt = (
            f"GoogleSearchを使って{location_str}それぞれの今日の天気予報を調べて、"
            "キャラクターとしての口調でDiscordの特定の誰かではなく、みんなに朝の天気をまとめてお知らせしてください。"
            "各地点の気温・降水確率・おすすめの服装など実用的な情報を含め、2000文字以内でまとめてください。"
        )
    else:
        weather_prompt = (
            f"GoogleSearchを使って{locations[0]}の今日の天気予報を調べて、"
            "キャラクターとしての口調でDiscordの特定の誰かではなく、みんなに朝の天気をお知らせしてください。"
            "気温・降水確率・おすすめの服装など実用的な情報を含め、2000文字以内でまとめてください。"
        )

    send_time_iso = datetime.datetime.now(pytz.timezone("Asia/Tokyo")).isoformat()
    formatted_prompt = f"システム\n{send_time_iso}\n{weather_prompt}"

    try:
        response = _send_message_with_retry(shared_chat_session, [formatted_prompt])
        bot_reply = response.text
        if not bot_reply or not bot_reply.strip():
            print("朝の天気アナウンス: 空の応答が返されました。")
            return

        add_message_to_db("user", "system", formatted_prompt)
        add_message_to_db("model", "bot", bot_reply)

        for channel_id in TARGET_CHANNEL_IDS:
            channel = bot.get_channel(channel_id)
            if channel:
                await channel.send(bot_reply)
            else:
                print(
                    f"朝の天気アナウンス: チャンネルID {channel_id} が見つかりませんでした。"
                )

    except Exception as e:
        print(f"朝の天気アナウンス中にエラーが発生しました: {e}")


@bot.command("weather")
@commands.has_permissions(administrator=True)
async def weather_command(ctx):
    """天気アナウンスを即時実行するテスト用コマンド（管理者専用）。"""
    async with ctx.channel.typing():
        await morning_weather_announcement()


@tasks.loop(
    time=datetime.time(
        hour=7,
        minute=2,
        second=0,
        tzinfo=datetime.timezone(datetime.timedelta(hours=9)),
    )
)
async def bocchi_news_announcement():
    """毎朝7時2分(JST)にぼっち・ざ・ろっく！の最新ニュースをアナウンスする。"""
    global shared_chat_session

    if not shared_chat_session:
        print("ぼっちニュース: チャットセッションが未初期化のためスキップします。")
        return

    news_prompt = (
        "GoogleSearchを使ってぼっち・ざ・ろっく！（Bocchi the Rock!）に関する"
        "過去24時間以内の最新ニュースを調べてください。"
        "アニメ・漫画・ライブ・グッズ・コラボなど関連する新着情報があれば、"
        "キャラクターとしての口調でDiscordの特定の誰かではなく、みんなにお知らせしてください。"
        "新しいニュースが特にない場合は何も出力しないでください（空白のみで応答してください）。"
        "2000文字以内でまとめてください。"
    )

    send_time_iso = datetime.datetime.now(pytz.timezone("Asia/Tokyo")).isoformat()
    formatted_prompt = f"システム\n{send_time_iso}\n{news_prompt}"

    try:
        response = _send_message_with_retry(shared_chat_session, [formatted_prompt])
        bot_reply = response.text
        if not bot_reply or not bot_reply.strip():
            print(
                "ぼっちニュース: 新着ニュースなし、または空応答のためスキップします。"
            )
            return

        add_message_to_db("user", "system", formatted_prompt)
        add_message_to_db("model", "bot", bot_reply)

        for channel_id in TARGET_CHANNEL_IDS:
            channel = bot.get_channel(channel_id)
            if channel:
                await channel.send(bot_reply)
            else:
                print(
                    f"ぼっちニュース: チャンネルID {channel_id} が見つかりませんでした。"
                )

    except Exception as e:
        print(f"ぼっちニュースアナウンス中にエラーが発生しました: {e}")


@bot.command("bocchinews")
@commands.has_permissions(administrator=True)
async def bocchi_news_command(ctx):
    """ぼっちニュースアナウンスを即時実行するテスト用コマンド（管理者専用）。"""
    async with ctx.channel.typing():
        await bocchi_news_announcement()


@tasks.loop(
    time=datetime.time(
        hour=17,
        minute=0,
        second=0,
        tzinfo=datetime.timezone(datetime.timedelta(hours=9)),
    )
)
async def evening_alcohol_review():
    """毎晩17時(JST)にきくりに一時切り替えして安酒レビューをアナウンスし、元のキャラに戻す。"""
    global shared_chat_session, active_character_key

    original_character_key = active_character_key

    try:
        # きくりに一時切り替え
        initialize_chat_session("kikuri")
        if not shared_chat_session:
            print("安酒レビュー: きくりセッションの初期化に失敗したためスキップします。")
            return

        review_prompt = (
            "GoogleSearchを使って今日飲むならこれ！というおすすめの安酒（コンビニ・スーパーで買えるもの）を"
            "1種類調べてください。値段・味の特徴・どんなシーンに合うかを含め、"
            "きくりとしての口調でDiscordの特定の誰かではなく、みんなに向けて今日の安酒レビューをしてください。"
            "2000文字以内でまとめてください。"
        )

        send_time_iso = datetime.datetime.now(pytz.timezone("Asia/Tokyo")).isoformat()
        formatted_prompt = f"システム\n{send_time_iso}\n{review_prompt}"

        response = _send_message_with_retry(shared_chat_session, [formatted_prompt])
        bot_reply = response.text
        if not bot_reply or not bot_reply.strip():
            print("安酒レビュー: 空応答のためスキップします。")
            return

        add_message_to_db("user", "system", formatted_prompt)
        add_message_to_db("model", "bot", bot_reply)

        for channel_id in TARGET_CHANNEL_IDS:
            channel = bot.get_channel(channel_id)
            if channel:
                await channel.send(bot_reply)
            else:
                print(
                    f"安酒レビュー: チャンネルID {channel_id} が見つかりませんでした。"
                )

    except Exception as e:
        print(f"安酒レビュー中にエラーが発生しました: {e}")

    finally:
        # 元のキャラに戻す
        if original_character_key:
            initialize_chat_session(original_character_key)
            print(f"安酒レビュー: キャラクターを「{original_character_key}」に戻しました。")
            for channel_id in TARGET_CHANNEL_IDS:
                channel = bot.get_channel(channel_id)
                if channel:
                    await channel.send(f"（{active_character_display_name} に戻りました）")


@bot.command("alcoholreview")
@commands.has_permissions(administrator=True)
async def alcohol_review_command(ctx):
    """安酒レビューを即時実行するテスト用コマンド（管理者専用）。"""
    async with ctx.channel.typing():
        await evening_alcohol_review()


@bot.event
async def on_ready():
    print(f"{bot.user.name} がDiscordに接続しました！")
    print("------")
    initialize_chat_session()
    if not morning_weather_announcement.is_running():
        morning_weather_announcement.start()
    if not bocchi_news_announcement.is_running():
        bocchi_news_announcement.start()
    if not evening_alcohol_review.is_running():
        evening_alcohol_review.start()
    await _announce_update_if_needed()


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
    if is_command_message(message):
        print(
            f"コマンドメッセージを検出しました: {message.content[:50]}..."
        )  # デバッグ用
        return  # コマンドとして処理されたので、通常のメッセージ処理は行わない

    if not shared_chat_session:
        await message.channel.send(
            "ボットのチャット機能が準備中です。少し待ってからもう一度お試しください。"
        )
        return

    is_mentioned = bot.user.mentioned_in(message)
    should_respond = should_respond_to_message(message)

    if not should_respond:
        return

    async with message.channel.typing():
        attachment_contents = []
        if message.attachments:
            attachment_contents = await extract_supported_attachment_parts(message)

        author_name = message.author.display_name
        user_input = build_user_input(message, is_mentioned)
        bot_reply = await handle_shared_discord_message(
            author_name, user_input, attachment_contents
        )

        if bot_reply and bot_reply.strip():  # Ensure there's non-whitespace content
            await message.reply(bot_reply, mention_author=False)
        else:
            print(
                f"Warning: Bot generated an empty or whitespace-only reply for user input: '{user_input}'"
            )


# --- グローバルなChatSession (メモリキャッシュとして) ---
# スクリプトが再起動されると失われるため、ファイル保存と組み合わせる
shared_chat_session = None
MODEL_NAME = "gemini-3-flash-preview"
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
client = genai.Client(api_key=GOOGLE_API_KEY)
google_search_tool = Tool(google_search=GoogleSearch())
google_url_context_tool = Tool(url_context=UrlContext())


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

    with get_db_connection() as conn:
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


def add_message_to_db(role, author_name, content):
    global active_character_key

    if active_character_key is None:
        raise ValueError(
            "アクティブなキャラクターキーが設定されていません。メッセージ保存できません。"
        )

    table_name = get_history_table_name(active_character_key)

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"""
        INSERT INTO {table_name} (role, author_name, content, timestamp)
        VALUES (?, ?, ?, ?)
        """,
            (role, author_name, content, datetime.datetime.now()),
        )


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
        return "", [], main_character_key

    processed_relations.add(main_character_key)

    main_char_data = _load_raw_character_data(main_character_key)
    if not main_char_data:
        return "", [], main_character_key

    display_name = main_char_data.get("character_name_display", main_character_key)
    system_instruction_user = main_char_data.get(
        "system_instruction_user", ""
    )  # メインキャラの基本指示
    # 以下は Gemini のプロンプト ベストプラクティスに沿った構造化された補助指示です。
    system_instruction_user += (
        "\n\n<context>キャラクター設定として上記のプロンプトを前提とする。</context>\n"
        "<task>目的: ユーザーと自然な会話を継続し、キャラクター性（口調・動機）を一貫して守る。</task>\n"
        "<input_format>ユーザー発言は次の形式で送られます\n発言者名\n送信時刻(ISO 8601, タイムゾーン付き・日本標準時/JSTで提供されます)\n発言内容\n画像は別のPartオブジェクトとして渡されることがある。</input_format>\n"
        "<note>送信時刻は JST の ISO 形式で与えられます。発言内容の時間的文脈が必要な場合はこの時刻を参照してください。モデルは自身で時刻を推測せず、この提供された時刻を優先して扱ってください。</note>\n"
        "<output_requirements>言語: 日本語。デフォルトは簡潔で直接的。必要ならユーザーが「詳しく」と要求する。出力は会話文、相手の名前を明示して応答、Discord制限: 最大2000文字。</output_requirements>\n"
        "<constraints>'私はAI' を明示しない。差別的・違法行為助長表現禁止。\n発言者名が異なる場合は別人として扱うこと。\n文体・語彙・文長を定期的に変化させ、過度に似た導入句や決まり文句を避ける。過去の自分の発言をそのまま繰り返したり逐次的に修正するような出力を行わないこと。\n回答に必要な事実がプロンプト内にない場合は推測で断定せず、GoogleSearch を使って確認すること。\nキャラクター設定に不足している情報が必要な場合も、創作せず GoogleSearch で確認し、確認できない要素は断定しないこと。</constraints>\n"
        "<tools>利用可能なツール: UrlContext(指定されたURLの内容を読み取る)、GoogleSearch(情報検索)。これらのツールは必要に応じて使用して正確な情報を取得してください。プロンプトや会話履歴・画像だけでは回答に必要な情報が不足している場合や、キャラクター情報の補完が必要な場合は、推測で補わず GoogleSearch を使って確認してください。ツールを使った結果はツールの出力を忠実に扱い、事実確認が取れない場合はその旨を明示してください。</tools>\n"
        "<multi_modal>画像: Partオブジェクトを受け取る。画像に基づく記述は簡潔に、視覚的情報を補助的に扱う。</multi_modal>\n"
        "<priority>優先順: task/constraints/output_requirements > tools > multi_modal。</priority>\n"
        "<anchor>上記指示に基づき、以下のユーザー入力に答えてください:</anchor>\n"
    )

    if not system_instruction_user:
        print(
            f"警告: メインキャラクター「{display_name}」のプロンプト基本情報が不完全です。"
        )

    final_initial_prompts = []
    return system_instruction_user, final_initial_prompts, display_name


def get_setting_from_db(key, default_value=None):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS bot_settings (key TEXT PRIMARY KEY, value TEXT)"
        )
        cursor.execute("SELECT value FROM bot_settings WHERE key = ?", (key,))
        row = cursor.fetchone()
    return row[0] if row else default_value


def set_setting_in_db(key, value):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS bot_settings (key TEXT PRIMARY KEY, value TEXT)"
        )
        cursor.execute(
            "INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)",
            (key, value),
        )


def load_history_from_db(limit=100):  # 例: 直近100件のやり取りを読み込む
    global active_character_key

    if active_character_key is None:
        raise ValueError(
            "アクティブなキャラクターキーが設定されていません。履歴読み込みできません。"
        )

    table_name = get_history_table_name(active_character_key)

    conn = None
    raw_rows_from_db = []  # DBから直接読み込んだ行データ

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        # timestampの降順で最新N件を取得し、それをさらに昇順に並べ替える
        # (SQLiteではサブクエリやウィンドウ関数が使えるが、シンプルに全件取得してPython側でハンドリングも可)
        # ここではシンプルに最新N件のメッセージを取得（userとmodelそれぞれを1件と数える）
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
        raw_rows_from_db = cursor.fetchall()
        print(
            f"テーブル {table_name} から {len(raw_rows_from_db)} 件の履歴をDBより読み込みました。"
        )  # テーブル名を出力
    except sqlite3.OperationalError as e:
        # テーブルが存在しない場合などに発生するエラー
        print(
            f"情報: テーブル {table_name} が見つからないかアクセスできません。新しい履歴として扱います。エラー詳細: {e}"
        )
        # raw_rows_from_db は空のまま
    except Exception as e:
        print(f"DB履歴の読み込み中に予期せぬエラーが発生しました ({table_name}): {e}")
        raw_rows_from_db = []  # 念のため空にする
    finally:
        if conn:
            conn.close()

    history_for_model = []

    if not raw_rows_from_db:
        print(f"DBテーブル {table_name} から読み込む有効な会話履歴はありませんでした。")
    else:
        # 履歴が必ず "user" メッセージから始まるように調整
        start_index = -1
        for i, row_data in enumerate(raw_rows_from_db):
            if row_data["role"] == "user":
                start_index = i
                break

        if start_index != -1:
            # "user" メッセージが見つかった場合、そこから履歴を開始
            effective_rows = raw_rows_from_db[start_index:]
            if start_index > 0:
                print(
                    f"読み込んだDB履歴の先頭 {start_index} 件 (modelロール) をスキップし、最初のuserロールのメッセージから履歴を開始します。"
                )

            for row_data in effective_rows:
                if row_data["role"] == "user":
                    text_content = row_data["content"]
                    history_for_model.append(
                        {"role": "user", "parts": [{"text": text_content}]}
                    )
                else:
                    # 過去のボット応答は逐語でモデルに渡すと自己模倣を助長するため省略またはプレースホルダを渡す
                    history_for_model.append(
                        {"role": "model", "parts": [{"text": "[前のボット応答は省略]"}]}
                    )
            print(
                f"DBから {len(effective_rows)} 件の整形済み会話履歴をモデル入力用に準備しました。"
            )
        else:
            # 読み込んだ履歴内に "user" メッセージが見つからなかった場合
            print(
                f"読み込んだDB履歴 {len(raw_rows_from_db)} 件の中にuserロールのメッセージが見つからなかったため、DBからの会話履歴は使用しません。"
            )

    return history_for_model


active_character_key = None
active_character_display_name = (
    "デフォルト"  # 現在のキャラクター表示名を保持するグローバル変数
)


def _create_chat_session(system_instruction: str = None, history: list = None):
    """Helper function to create a new chat session."""
    global shared_chat_session
    if history is None:
        history = []

    chat_config = GenerateContentConfig(
        response_modalities=["TEXT"],
        system_instruction=system_instruction,
        thinking_config=ThinkingConfig(thinking_level="low"),
        tools=[google_search_tool, google_url_context_tool],
    )

    shared_chat_session = client.chats.create(
        model=MODEL_NAME, history=history, config=chat_config
    )


def initialize_chat_session(character_key_to_load=None):
    """
    ボット起動時に呼び出され、チャットセッションを初期化または復元する。
    """
    global shared_chat_session, active_character_key, active_character_display_name

    if character_key_to_load is None:
        character_key_to_load = get_setting_from_db("current_character_key", "lycaon")

    system_instruction_text, initial_conversation_history, display_name = (
        load_character_definition(character_key_to_load)
    )
    active_character_key = character_key_to_load
    active_character_display_name = display_name  # グローバルな表示名を更新

    if not system_instruction_text:
        print(
            f"警告: キャラクター「{character_key_to_load}」のプロンプトでセッションを開始できません。"
        )
        shared_chat_session = None
        return

    create_table_if_not_exists()  # DBテーブル作成

    # DBから履歴を読み込み
    history_from_db = load_history_from_db(limit=30)

    # 最終的な履歴を作成: (キャラクタープロンプト + DBからの会話履歴)
    final_history_for_session = initial_conversation_history + history_from_db
    _create_chat_session(
        system_instruction=system_instruction_text, history=final_history_for_session
    )
    set_setting_in_db(
        "current_character_key", character_key_to_load
    )  # 現在のキャラをDBに保存
    print(
        f"チャットセッションがキャラクター「{active_character_display_name}」とDB履歴で初期化されました。"
    )


# Gemini API呼び出しにリトライを適用するヘルパー関数
@retry(
    stop=stop_after_attempt(5),  # 最大5回試行 (初回 + 4回リトライ)
    wait=wait_exponential(
        multiplier=1, min=4, max=30
    ),  # 最小4秒、その後8秒、16秒と指数関数的に増加し、最大30秒まで待機
)
def _send_message_with_retry(chat_session, contents):
    """
    Gemini ChatSessionのsend_messageをリトライ付きで実行するヘルパー関数。
    """
    # print("Gemini APIにメッセージを送信中...")
    try:
        response = chat_session.send_message(contents)
        # print("Gemini APIからの応答を受信しました。")
        if response.text is None:
            raise Exception("Response text is None.")
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
    author_name, user_message_content, attachment_contents=None
):
    """
    Discordのメッセージを受け取り、Gemini APIに応答を生成させる (共有・効率化版)
    """
    global shared_chat_session, active_character_key

    if not shared_chat_session:
        # ボット起動時に初期化されているはずだが、念のため
        print("エラー: チャットセッションが初期化されていません。")
        initialize_chat_session()  # 強制的に初期化を試みる（本番では on_ready で行うべき）
        if not shared_chat_session:
            return "申し訳ありません、ボットのチャット機能が正しく起動していません。管理者にご連絡ください。"

    # 送信時刻 (ローカルタイム、タイムゾーン付き ISO 8601)
    # 送信時刻を日本標準時(JST)で取得してISO 8601形式で送る
    send_time_iso = datetime.datetime.now(pytz.timezone("Asia/Tokyo")).isoformat()
    original_message_for_api = f"{author_name}\n{send_time_iso}\n{user_message_content}"
    print(original_message_for_api)

    try:
        MAX_HISTORY_LENGTH = 60  # 履歴内の最大メッセージ数 (初期プロンプト + 会話)

        # Chatオブジェクトから現在の履歴を取得 (curated=True でモデルに送信される履歴を取得)
        current_history_list = shared_chat_session.get_history(curated=True)

        if len(current_history_list) > MAX_HISTORY_LENGTH:
            print(
                f"現在の履歴長 ({len(current_history_list)}) が最大長 ({MAX_HISTORY_LENGTH}) を超えたため、履歴を整理します。"
            )

            initialize_chat_session(active_character_key)

    except Exception as e:
        print(f"履歴の整理中にエラーが発生しました: {e}")
        # 致命的ではないかもしれないので、処理を続行する。エラーメッセージを返すことも検討。

    # --- Gemini APIへの送信と応答長チェック ---
    first_api_call_contents = [original_message_for_api]
    if attachment_contents:
        for attachment_part in attachment_contents:
            first_api_call_contents.append(attachment_part)

    MAX_ATTEMPTS_FOR_LENGTH = 3  # 初回試行 + 2回の短縮試行
    bot_response_text = ""

    for attempt in range(MAX_ATTEMPTS_FOR_LENGTH):
        current_api_call_input_parts: list

        if attempt == 0:
            current_api_call_input_parts = first_api_call_contents
        else:
            # 応答が長すぎたため再試行
            shortening_prompt_text = "あなたの直前の応答はDiscordの文字数制限(2000文字)を超過しました。内容を維持しつつ、2000文字以内で簡潔に言い直してください。"
            print(
                f"応答短縮を要求します (試行 {attempt + 1}/{MAX_ATTEMPTS_FOR_LENGTH}): {shortening_prompt_text}"
            )
            current_api_call_input_parts = [shortening_prompt_text]

        try:
            # APIに送信。shared_chat_session.historyはこの呼び出しによって更新される
            # (入力内容が'user'として、応答内容が'model'として追加される)
            response = _send_message_with_retry(
                shared_chat_session, current_api_call_input_parts
            )
            bot_response_text = response.text

            if len(bot_response_text) <= MAX_DISCORD_MESSAGE_LENGTH:
                # 応答が適切な長さであれば、DBに保存して返す
                add_message_to_db(
                    role="user",
                    author_name=author_name,
                    content=original_message_for_api,
                )
                add_message_to_db(
                    role="model", author_name="bot", content=bot_response_text
                )
                print(
                    f"Geminiからの応答（試行 {attempt + 1}）: {bot_response_text[:200]}..."
                )  # ログには一部表示
                return bot_response_text
            else:
                # 応答が長すぎる場合
                print(
                    f"Geminiの応答が長すぎます ({len(bot_response_text)}文字)。試行 {attempt + 1}/{MAX_ATTEMPTS_FOR_LENGTH}。"
                )
                # 長すぎた応答はDBには保存しない。ループが継続すれば短縮が試みられる。
                if attempt == MAX_ATTEMPTS_FOR_LENGTH - 1:
                    # これが最後の試行でも長すぎた場合
                    break  # ループを抜けて最終処理へ

        except ServerError as e:  # _send_message_with_retry がリトライを諦めた場合
            print(
                f"Gemini APIでサーバーエラーが発生しました（試行 {attempt + 1}）：{e}"
            )
            if attempt == MAX_ATTEMPTS_FOR_LENGTH - 1:  # 最後の試行でのエラー
                return "Gemini APIでエラーが繰り返し発生しました。しばらくしてからもう一度お試しください。"
            # ループは継続し、次の試行で再度API呼び出しが行われる（べきだが、ここではエラーとして終了させる方が安全か）
            # ServerErrorがここまで来たということは、_send_message_with_retry内のリトライが尽きたということ。
            return "Gemini APIとの通信中にエラーが発生しました。"  # ここで終了させる
        except Exception as e:  # その他の予期せぬエラー
            print(
                f"メッセージ処理中に予期せぬエラーが発生しました（試行 {attempt + 1}）：{e}"
            )
            return "メッセージの処理中に予期せぬエラーが発生しました。"

    # ループが完了しても適切な長さの応答が得られなかった場合
    if len(bot_response_text) > MAX_DISCORD_MESSAGE_LENGTH:
        print(
            f"Geminiの応答は、{MAX_ATTEMPTS_FOR_LENGTH}回の試行後も長すぎます。最終応答長: {len(bot_response_text)}"
        )
        # この長すぎた最終応答はDBには保存しない。セッション履歴には残っている。
        return "エラー、回答できませんでした。"

    # 通常ここには到達しないはずだが、万が一のためのフォールバック
    print("予期せぬ状態で応答生成が終了しました。")
    return "予期せぬエラーにより応答を生成できませんでした。"


bot.run(TOKEN)
