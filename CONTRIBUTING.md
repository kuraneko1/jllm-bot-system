# 開発参加ガイド

## 開発環境

Python 3.14 以上と uv を用意し、workspace の全 package を導入します。

```bash
uv sync --all-packages
uv run python -m unittest discover -s tests -v
uv run python -m unittest discover -s plugins/builtins/tests -v
```

変更後は、少なくとも上記のテストを実行してください。実際の Discord、音声、外部 service を使う検証は、個人情報や認証情報を含まない専用の開発環境で行います。

## 設計を守るための規則

- kernel に Discord、TTS、LLM、辞書などの機能を直接 import しません。
- plugin 同士は具象 module ではなく、version 付き capability と domain contract で接続します。
- 新しい設定には schema と既定値を用意し、起動前に検証します。
- 購読、task、接続、file handle には所有者を持たせ、停止時に await して解放します。
- 機能の追加・削除は profile から確認できるようにします。

大きな設計変更を始める前に [PLUGIN_ARCHITECTURE.md](PLUGIN_ARCHITECTURE.md) を読み、対象外の機能まで同時に書き換えないでください。

## テスト

次の変更には、動作を固定するテストを添えます。

- dependency、binding、lifecycle の変更
- 設定の解釈、権限、認証、公開範囲の変更
- データ形式と migration
- 外部 service との通信、timeout、cancel、retry

秘密情報や実際の会話を fixture にしないでください。音声・画像などの大きな fixture も Git に入れず、テスト内で生成できる小さな代替を使います。

## 秘密情報と実行データ

実際の token、API key、SSH secret key、passkey、会話 log、参照音声、モデルを commit しないでください。`.env.example` には架空の値だけを記載します。追加したデータ形式が `.gitignore` で除外されるかも確認してください。

## 文書とコード

README、開発文書、利用者向けの説明は日本語で書きます。コードの identifier は英語を使い、コメントはコードだけでは分からない理由に絞ります。変更内容と確認結果がほかの人に伝わるよう、pull request に記載してください。

投稿された変更は [MIT License](LICENSE) の下で公開されます。
