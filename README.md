# JLLM Bot System

JLLM Bot System は、Discord Bot「jllm-voice」を全面再設計するプロジェクトです。DeepSeek Harness / Cordis の設計思想を取り入れ、主要機能を profile から着脱・交換できるプラグインとして実装します。

> [!NOTE]
> 現在は移行の初期段階です。プラグイン kernel と検証用 plugin を先行して整備しており、旧 jllm-voice の Discord 機能はまだ移行されていません。

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
uv sync
uv run python -m unittest discover -s tests -v
```

## 秘密情報と実行データ

認証情報は `.env` など、Git から除外した場所に保管します。初期設定の雛形として [.env.example](.env.example) を用意していますが、ここにも実際の token は書かないでください。

```bash
cp .env.example .env
```

次のものはリポジトリに含めません。

- Discord token、API key、SSH secret key、passkey、Cookie
- 音声、参照音声、画像生成物、モデル、フォント
- 会話 log、SQLite、export、ユーザー設定、そのほかの実行データ

意図しないファイルが追跡対象になっていないかは `git status --ignored` で確認できます。

## ドキュメント

- [全体再設計書](PLUGIN_ARCHITECTURE.md)
- [セキュリティ方針](SECURITY.md)

## ライセンス

[MIT License](LICENSE)
