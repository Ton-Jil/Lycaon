#!/bin/bash
set -e # エラーが発生したらスクリプトを終了

# フォールバックとしてカレントディレクトリを使う (cloudbuild.yamlでcdしている前提ならこれで良い)
PROJECT_DIR_ON_VM=$(pwd)

echo "ブートストラップデプロイを開始します。対象ディレクトリ: $PROJECT_DIR_ON_VM"

# Gitのsafe.directory設定 (このスクリプトがリポジトリ内で実行される場合)
# cd コマンドで既にプロジェクトディレクトリに移動しているため、pwd でパスを取得
echo "Gitのsafe.directoryを設定します (対象: ${PROJECT_DIR_ON_VM})"
git config --global --add safe.directory "${PROJECT_DIR_ON_VM}"

# 1. 最新のコードを取得 (actual_deploy.sh もここで最新バージョンになる)
echo "最新のコードを取得しています (git pull origin main)..."
git pull origin main # メインブランチ名を適宜変更してください
echo "コードの更新が完了しました。"

# 2. メインのデプロイスクリプトのパスを定義 (bootstrap_deploy.sh と同じディレクトリにあると仮定)
ACTUAL_DEPLOY_SCRIPT="./builder/actual_deploy.sh"

if [ -f "$ACTUAL_DEPLOY_SCRIPT" ]; then
    echo "メインのデプロイスクリプト ($ACTUAL_DEPLOY_SCRIPT) を実行します..."
    # exec を使うと現在のシェルプロセスが新しいスクリプトに置き換わる
    exec "$ACTUAL_DEPLOY_SCRIPT"
else
    echo "エラー: メインのデプロイスクリプト ($ACTUAL_DEPLOY_SCRIPT) が見つかりません。"
    exit 1
fi
