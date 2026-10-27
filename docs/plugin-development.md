# プラグイン開発ガイド

この文書では、現在の kernel を使った最小の plugin 作成手順を説明します。同梱の `fake.provider`、`fake.router`、`fake.consumer` は、discovery・binding・lifecycle を確認するための例です。実機能の例として、`adapter.discord`、`storage.local`、`policy.restrictions`、`feature.wavrank` も用意しています。

## 1. 配置

初期段階では、論理 plugin を `plugins/builtins` にまとめます。独立した公開・交換・任意依存が必要になったものだけを別 package に分けます。

```text
plugins/builtins/
├── pyproject.toml
└── src/jllm_plugins/
    └── example/
        ├── __init__.py
        ├── plugin.json
        ├── config.schema.json
        └── plugin.py
```

`pyproject.toml` の entry point は、実装 class ではなく manifest resource を指します。

```toml
[project.entry-points."jllm.plugins"]
"example" = "jllm_plugins.example:plugin.json"
```

kernel は profile で有効化された plugin だけを activation します。install 済み plugin がすべて自動起動するわけではありません。

## 2. Manifest

```json
{
  "id": "example",
  "version": "1.0.0",
  "api": ">=1.0,<2.0",
  "entry": "jllm_plugins.example.plugin:Plugin",
  "provides": [
    {"capability": "example.output", "version": 1}
  ],
  "requires": [],
  "optional": [],
  "permissions": [],
  "config_schema": "config.schema.json",
  "multi_instance": true
}
```

- `id`: profile 上の名前です。`/` を含めません。
- `version`: plugin 自体の semver です。
- `api`: 対応する plugin API の範囲です。
- `entry`: 実装 class です。manifest の検査後に読み込まれます。
- `provides`: この plugin が公開する capability です。
- `requires` / `optional`: 必要な capability と cardinality です。
- `config_schema`: 起動前に検査する JSON Schema です。
- `multi_instance`: 複数 instance を許す場合だけ `true` にします。

## 3. 実装

plugin は `setup`、`start`、`stop` を持ちます。

```python
class Plugin:
    async def setup(self, ctx):
        self.prefix = ctx.config.get("prefix", "")
        ctx.scope.add(self.close_resource)
        return {"example.output": self}

    async def start(self):
        pass

    async def stop(self):
        pass

    async def close_resource(self):
        pass
```

`PluginContext` には次が渡されます。

| 属性 | 内容 |
| --- | --- |
| `config` | JSON Schema で検証済みの instance 設定 |
| `dependencies` | binding で解決した service。`many` は list |
| `scope` | disposer と asyncio task を所有する resource scope |
| `logger` | instance id 付きの logger |

`setup` は、manifest の `provides` と同じ capability 名を key に持つ dict を返します。外部接続や常駐処理の開始は `start`、停止は `stop` に置きます。

`ctx.scope.add(...)` で登録した disposer は逆順に await されます。`ctx.scope.create_task(...)` で作った task は、scope の終了時に cancel され、終了まで待たれます。独自に `asyncio.create_task` へ逃がさないでください。

`close()` や `stop()` の呼び出しがキャンセルされても、内部の cleanup は共有 task で最後まで実行されます。呼び出し側には cleanup 完了後にキャンセルが伝わります。同時に停止を要求した場合も、同じ cleanup の完了を待ちます。

## 4. Profile と binding

```yaml
plugins:
  example:
    enabled: true
    instances:
      first:
        prefix: "first: "
      second:
        prefix: "second: "
  example.consumer:
    enabled: true
    critical: false
bindings:
  example.consumer:
    example.output: example/first
```

instance id は `<plugin id>/<instance name>` です。

- `one`: provider が 1 件だけ、または binding で 1 件を指定します。
- `many`: binding へ順序付きの list を指定します。install 済み provider の暗黙収集は行いません。
- `optional_one`: 0 件を許します。2 件以上は明示します。
- 曖昧な binding、未知の instance、version 不整合は profile 全体を拒否します。
- 必須 provider が 0 件の場合、その依存先は `BLOCKED` になります。critical な依存先があれば起動を中止します。

`critical` は dependency の必須・任意とは別です。重要な plugin の失敗時に全体を止めるかどうかを指定します。

## 5. 検査

```bash
uv sync --all-packages
uv run jllm plugins list
uv run jllm config validate --profile profiles/minimal.yaml
uv run jllm doctor --profile profiles/minimal.yaml
uv run python -m unittest discover -s tests -v
```

`doctor` は profile と dependency を検査します。`PENDING` はまだ起動していない正常な状態です。`BLOCKED` がある場合は理由と終了 status 1 を返します。

設定値や例外に token を含めないでください。秘密情報は環境変数や別の secret store から渡し、ログ・テスト・fixture・commit に出さないでください。
