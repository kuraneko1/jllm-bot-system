# JLLM Bot System

JLLM Bot System は、Discord Bot「jllm-voice」を全面再設計するプロジェクトです。DeepSeek Harness / Cordis の設計思想を取り入れ、主要機能を profile から着脱・交換できるプラグインとして実装します。

> [!NOTE]
> Phase 1 の plugin kernel、Phase 2 の Discord・WavRank vertical slice、Phase 3 の TTS contract・VOICEVOX／OpenAI 互換／speech.cpp／Irodori provider・direct adapter を実装しています。ほかの旧 jllm-voice 機能は移行中です。実 Discord・実音声での互換確認はまだ完了していません。

## 設計の要点

- Python 3.14 と asyncio を採用し、起動・依存解決・後片付けを小さな kernel に集約します。
- 機能 plugin は具象 plugin ではなく、version 付き capability に依存します。
- manifest と設定を起動前に検査し、依存不足と設定不正を区別します。
- 論理 plugin と配布単位を分け、必要な機能だけを導入できるようにします。
- 実行データと秘密情報をコードから分離します。

全体構成、message 処理、データ移行、段階的な導入計画は [PLUGIN_ARCHITECTURE.md](PLUGIN_ARCHITECTURE.md) を参照してください。

## 開発環境

- Python 3.14 以上
- [uv](https://docs.astral.sh/uv/)

```bash
uv sync --all-packages
uv run python -m unittest discover -s tests -v
uv run python -m unittest discover -s plugins/builtins/tests -v
uv run jllm plugins list
uv run jllm config validate --profile profiles/minimal.yaml
uv run jllm doctor --profile profiles/minimal.yaml
```

`plugins list`、`doctor`、`config validate` は plugin・profile・依存関係の検査用です。`run` は profile を起動します。

## WavRank vertical slice

`profiles/wavrank.yaml` には Discord adapter、local storage、制限設定、WavRank を含めています。実際に使うときは、認証情報を含められるローカル profile を Git 外に作り、チャンネルとデータの場所を指定します。

```bash
mkdir -p profiles.local
cp profiles/wavrank.yaml profiles.local/wavrank.yaml
# profiles.local/wavrank.yaml の channel_id・data_dir を編集
uv run jllm run --profile profiles.local/wavrank.yaml
```

`data/exports/<channel>/messages.jsonl`、`sounds.json`、フォントなどの実行データは別途用意します。実データをこのリポジトリへコピーしないでください。

## 秘密情報と実行データ

認証情報は `.env` など、Git から除外した場所に保管します。初期設定の雛形として [.env.example](.env.example) を用意していますが、ここにも実際の token は書かないでください。

```bash
cp .env.example .env
```

次のものはリポジトリに含めません。

- Discord token、API key、SSH secret key、passkey、Cookie
- 音声、参照音声、画像生成物、モデル、フォント
- 会話 log、SQLite、export、ユーザー設定、そのほかの実行データ

意図しないファイルが追跡対象になっていないかは `git status --ignored` で確認できます。追跡対象の機械検査には次を使用します。

```bash
uv run python tools/check-repository.py
```

## ドキュメント

- [全体再設計書](PLUGIN_ARCHITECTURE.md)
- [プラグイン開発ガイド](docs/plugin-development.md)
- [互換性ステータス](docs/migration-status.md)
- [開発参加ガイド](CONTRIBUTING.md)
- [セキュリティ方針](SECURITY.md)

## ライセンス

[MIT License](LICENSE)
