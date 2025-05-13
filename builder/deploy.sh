#!/bin/bash
set -e # エラーが発生したらスクリプトを終了

# 仮想環境のパス (例)
VENV_PATH="venv/bin/activate" # WorkingDirectoryからの相対パス、またはフルパス

echo "デプロイを開始します..."

# 1. 最新のコードを取得
git pull origin main # または対象のブランチ
echo "コードを更新しました。"

# 2. 仮想環境を有効化 (もしあれば)
if [ -f "$VENV_PATH" ]; then
    source "$VENV_PATH"
    echo "仮想環境を有効化しました。"
else
    echo "警告: 仮想環境が見つかりません ($VENV_PATH)"
fi

# 3. 依存関係の更新
if [ -f "requirements.txt" ]; then
    pip install -r requirements.txt
    echo "依存関係を更新しました。"
fi

# 4. (もしあれば) データベースマイグレーションなどの追加タスク
# echo "追加タスクを実行します..."

# 5. systemdサービスの再起動
sudo systemctl restart my_discord_bot.service # systemdサービス名に置き換える
echo "Discordボットサービスを再起動しました。"

# (もし仮想環境を使っている場合、非アクティブ化は不要。スクリプト終了でシェルも閉じる)
# if [ -f "$VENV_PATH" ]; then
# deactivate
# fi

echo "デプロイが完了しました。"
