# JLLM ボットシステム

Discord Bot「jllm-voice」を、DeepSeek Harness と Cordis の設計思想を取り入れたプラグイン構成へ作り替えるプロジェクトです。読み上げ・翻訳・会話・画像生成・ウェブ管理などの主要機能を、設定から着脱・交換できるようにしています。

## 現在の状況

- プラグインカーネル、Discord、読み上げ、辞書、翻訳、会話、セッション、ウェブ管理、Miq、Umigame、Anima などの主要機能を移行しました。
- 旧実装との互換性を確認するテストを追加し、**1129件のテストと、内蔵プラグインの11件のテスト**が通ることを確認しました。
- 静的検査と、配布物の新規環境への導入検査も完了しています。
- 本番データの移行・逆移行・ロールバック、ならびに実際の Discord・ボイスチャンネル・x230 での確認は、これから実施します。

コードと自動テストの互換性は確認できていますが、本番環境での完全な互換確認はまだ完了していません。

## 設計の要点

- 起動・依存解決・後片付けを小さなカーネルに集約します。
- 各機能は具体的なプラグインではなく、版番号の付いた能力に依存します。
- プラグイン宣言と設定を起動前に検査し、依存不足と設定不正を区別します。
- 論理的なプラグインと配布単位を分け、必要な機能だけを導入できるようにします。
- 実行データと秘密情報をソースコードから分離します。

全体構成、メッセージ処理、データ移行、段階的な導入計画は [全体再設計書](PLUGIN_ARCHITECTURE.md) を参照してください。

## 主な機能

- Discord への接続と、コマンド・メッセージ処理
- 複数の音声合成先への対応、障害時の切り替え、音声効果、辞書、再生キュー
- 読み上げ設定、話者の固定、ブックマーク、メディア登録、HTML 登録
- 翻訳、会話、要約、セッションの分岐、今北産業
- パスキー認証を備えたウェブ管理画面とウェブチャット
- Miq の画像生成、Umigame、Anima、D1、実験場
- WavRank の集計と画像出力

## 必要なもの

- Python 3.14 以上
- [uv](https://docs.astral.sh/uv/)
- 使用する機能に応じた Discord トークンや音声合成先
- 音声や画像を扱う機能に必要な外部プログラム

## 導入と検査

リポジトリのルートディレクトリで次を実行します。

```bash
uv sync --all-packages
uv run python -m unittest discover -s tests -q
uv run python -m unittest discover -s plugins/builtins/tests -q
uvx ruff check .
```

配布物の作成と、別環境への導入確認は次で行います。

```bash
uv build --wheel --all-packages
```

## 起動方法

まず、秘密情報を含まない設定例を複製し、自分の環境に合わせて編集します。

```bash
cp .env.example .env
mkdir -p profiles.local
cp profiles/reader.yaml profiles.local/reader.yaml
```

`.env` と `profiles.local/` は Git の管理外です。実際のトークンや個人設定を書き込んで問題ありません。設定例には秘密情報を書かないでください。

次のコマンドでプラグインと設定を確認できます。

```bash
uv run jllm plugins list
uv run jllm config validate --profile profiles.local/reader.yaml
uv run jllm doctor --profile profiles.local/reader.yaml
```

起動は次のコマンドです。

```bash
uv run jllm run --profile profiles.local/reader.yaml
```

`profiles/` には、読み上げ・翻訳・ウェブ管理・独立機能などの構成例を置いています。実際の運用では、必要な構成を `profiles.local/` に複製して使います。

## WavRank の動作例

`profiles/wavrank.yaml` には、Discord 接続、ローカル保存、利用制限、WavRank を含めています。

```bash
mkdir -p profiles.local
cp profiles/wavrank.yaml profiles.local/wavrank.yaml
# profiles.local/wavrank.yaml のチャンネルとデータの場所を編集
uv run jllm run --profile profiles.local/wavrank.yaml
```

`data/exports/<channel>/messages.jsonl`、音声辞書、フォントなどの実行データは別途用意します。実データをリポジトリへコピーしないでください。

## 秘密情報と実行データ

認証情報は `.env` など、Git から除外した場所に保管します。初期設定の雛形として [.env.example](.env.example) を用意していますが、実際のトークンはここにも書かないでください。

次のものはリポジトリに含めません。

- Discord トークン、API キー、SSH の秘密鍵、パスキー、クッキー
- 音声、参照音声、画像生成物、モデル、フォント
- 会話ログ、SQLite、エクスポート、利用者設定、そのほかの実行データ

意図しないファイルが追跡対象になっていないかは `git status --ignored` で確認できます。追跡対象の機械検査には次を使用します。

```bash
uv run python tools/check-repository.py
```

## 主な構成

```text
src/jllm/                 カーネルとコマンドライン
contracts/               機能間で共有する契約
plugins/builtins/        内蔵プラグイン
profiles/                構成例
docs/                    開発・移行文書
tests/                   自動テスト
tools/                   検査とデータ移行の道具
```

## 文書

- [全体再設計書](PLUGIN_ARCHITECTURE.md)
- [プラグイン開発案内](docs/plugin-development.md)
- [互換性の状況](docs/migration-status.md)
- [開発参加案内](CONTRIBUTING.md)
- [安全方針](SECURITY.md)

## 開発への参加

変更は小さく分け、テストと静的検査を通してから記録してください。日本語の文書と、秘密情報を含まない設定例を維持します。詳しくは [開発参加案内](CONTRIBUTING.md) を参照してください。

## 使用許諾

[MIT ライセンス](LICENSE)
