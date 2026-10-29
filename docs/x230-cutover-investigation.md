# x230 本番入れ替え調査

調査日時: 2026-10-29（日本時間）。対象新実装: Git revision `050913b`、旧実装: 手元 working tree と x230 の稼働環境。これは調査結果であり、切り替え承認や完全互換の証明ではありません。

## 調査範囲と本番の現状

- SSH で読み取り中心の調査を行い、本番コード・設定・サービス・データは変更していない。会話内容、トークンや鍵の値は取得・記録していない。JSON の一部は、設定の有無・型・集計値と運用トグルの確認だけに使用。
- `jllm-voice.service` は active/running・自動起動有効、`Restart=always`、再起動間隔5秒。作業ディレクトリは `/home/ryu/jllm-voice`。
- 仮想環境 Python 3.14.7、システム Python 3.13.5、ffmpeg 7.1.5。uv は `/home/ryu/.local/bin/uv` に存在。ディスク空き約90 GiB（調査時）。
- 旧配布対象に相当する手元の147ファイルは SHA-256 が本番と一致。運用 policy と TTS バックエンド設定もそれぞれ一致。ただし、この比較は本番の追加ファイル、すべての依存、実行データの一致を保証しない。本番 Git は追跡済み変更7件・未追跡61項目。
- 実効設定からの `DATA_DIR` は `/home/ryu/jllm-voice/data`。データには会話・Webチャット・認証・辞書・音声・メディア・フォント・モデルなどがある。集計例: セッション 2,317 ファイル、exports 約935 MB、sounds 約353 MB。数値は稼働中の観測で、確定したバックアップ検証値ではない。
- `jllm-voice-export.timer` は有効で、起動3分後から15分間隔で `jllm-voice-export.service` を起動。実体は本番の `tools/fetch-channel-messages.py` で、Git 未追跡。エクスポート JSONL と Discord トークンに関係することを静的に確認。停止・復旧手順の対象に含め、タイマー起動中のサービスも処理完了を確認する。
- リリース領域は約8.6 GiB、うち `code.tar` が171件。旧配布処理は管理対象コードのバックアップであり、これを実行データの復元可能性の証拠としない（[旧 deploy 実装](</home/ryu/irodori-tts/scripts/deploy-x230.py#L76-L85>)）。実データのバックアップの有無・復元可能性は未確認。リリース領域とデータは同一ファイルシステム上。

## 切り替え前に解決する重大事項

1. **空の許可リストが全許可になる**。旧実装は空集合なら通常投稿を拒否するが、新実装は空なら全チャンネル許可（[旧設定](</home/ryu/irodori-tts/jllm-voice/config.py#L210-L220>)、[新判定](../plugins/builtins/src/jllm_plugins/reader/core.py#L165-L166)）。[既存 reader 例](../profiles/reader.yaml#L47-L51) は空配列であり、旧本番では許可リストと反応語がともに設定されている。新側ではメディア反応や登録処理も通りうる。読み上げ自体には別途 VC 接続・対象テキストチャンネル等の条件がある（[イベント処理](../plugins/builtins/src/jllm_plugins/reader/core.py#L672-L702)）。全拒否・許可・例外反応を比較するテストと、本番設定の正確な移植が必須。
2. **画像生成停止状態が引き継がれない**。旧側は保存済みトグルを復元するが、新 Reader は初期化時に `img=True` とし、wav を profile 値で初期化する（[旧読込](</home/ryu/irodori-tts/jllm-voice/cogs/reader.py#L2187-L2208>)、[新初期化](../plugins/builtins/src/jllm_plugins/reader/core.py#L199-L229)）。本番の保存値は調査時点で img=false、wav=true。新側では変更時に保存先を上書きするため、停止状態を保つ修正と起動・切り戻しテストが必要。
3. **TTS の9バックエンドと状態 ID が一致しない**。本番設定は speechcpp 3、irodori 2、voicevox 1、openai 3 の計9。旧選択状態は旧設定の ID と整合している。新 [reader 例](../profiles/reader.yaml#L14-L20) は VoiceVox 単一、新 [router 例](../profiles/tts-router.yaml#L37-L43) の4 ID は本番旧 ID と共通ゼロ。新 router は provider のインスタンス ID を使用し（[実装](../plugins/builtins/src/jllm_plugins/tts_router/plugin.py#L6-L25)）、未知の選択・streaming ID を既定に戻す（[実装](../plugins/builtins/src/jllm_plugins/tts_router/router.py#L215-L225)）。9件の設定と双方向 ID 対応表、声・streaming・優先先・フェイルオーバー・旧版への復元を検証する必要がある。
4. **認証ファイルを見失った状態で公開しない**。新 dashboard は storage 下の認証パスを固定している（[設定](../plugins/builtins/src/jllm_plugins/dashboard/plugin.py#L17-L30)）。パスキーファイルが存在しない場合は空の認証情報で初期化され、未登録時は管理セッションなしで初回登録を許す（[実装](../plugins/builtins/src/jllm_plugins/dashboard/dashboard_passkeys.py#L47-L65)、[登録条件](../plugins/builtins/src/jllm_plugins/dashboard/dashboard_passkeys.py#L115-L143)）。保存先・既存パスキー・公開 URL と RP ID の引き継ぎを確認し、検証完了まで外部公開しない。例の [dashboard profile](../profiles/dashboard.yaml#L31-L36) は待受 port 0 と URL の 8765 が一致しない。
5. **旧設定ファイルの自動読み込みが同等ではない**。旧は `.env` 読込後に `bot-policy.env` を上書き優先で読む（[旧設定](</home/ryu/irodori-tts/jllm-voice/config.py#L4-L9>)）。新 CLI に相当する dotenv 自動読込は見当たらず、Discord はプロセス環境のトークンを参照（[新実装](../plugins/builtins/src/jllm_plugins/discord/plugin.py#L55-L60)）。旧の policy・owner・TTS 切替権限・スコープ・API 接続等を本番用 profile と保護された環境へ明示的に対応付ける。値をリポジトリやログへ出さない。
6. **データ保存先を機能ごとに照合する**。旧側は個別指定、明示 DATA_DIR、旧配置を優先して選ぶ（[旧実装](</home/ryu/irodori-tts/jllm-voice/config.py#L24-L57>)）。新 `storage.local` は profile の `data_dir` 下を使う（[実装](../plugins/builtins/src/jllm_plugins/storage/plugin.py#L7-L22)）。さらに Reader の一部パスや Irodori・Miq のデータパスは独立している（[Reader](../plugins/builtins/src/jllm_plugins/reader/core.py#L1747-L1756)、[Irodori](../plugins/builtins/src/jllm_plugins/tts_irodori/plugin.py#L9-L24)、[Miq](../plugins/builtins/src/jllm_plugins/miq/miq_fonts.py#L11-L14)）。DATA_DIR だけ変更して済むと仮定せず、各入力・出力・認証・外部鍵の所在を照合する。
7. **全機能を含む本番用構成と依存が未確定**。旧起動は Reader、Miq、Translation、WavRank、Umigame、dashboard を組み合わせる（[旧起動](</home/ryu/irodori-tts/jllm-voice/main.py#L53-L73>)）。現行の例は複数 profile に分かれ、reader 単独では同じ組合せではない。各プラグインの依存・binding・権限・同時利用を組み合わせ検証する。Sarashina の URL・鍵・モデル別 RPM も例と旧既定が異なる（[旧設定](</home/ryu/irodori-tts/jllm-voice/config.py#L107-L147>)、[新スキーマ](../plugins/builtins/src/jllm_plugins/llm/provider_sarashina.schema.json#L4-L11)）。
8. **既存の doctor は実機接続診断ではない**。[CLI](../src/jllm/cli.py#L52-L64) は依存解決状態を表示し、Discord 接続・実 TTS 音声・外部 API・ffmpeg を検証しない。実環境試験を別に行う。

## 次の実装・検証計画（今回の調査では実行しない）

1. 上記の互換問題を修正し、旧本番設定を反映する統合 profile と、秘密を含まない移行仕様を作成する。環境設定の優先順位、9 TTS ID の対応、認証と保存パスを確定する。
2. 同一時点のバックアップを取得できるよう、Bot と export timer・実行中ジョブ・その他の書き込み元を洗い出し、停止・処理完了の確認と単一書き込みの手順を用意する。SQLite を含むデータ、設定、外部鍵等を、権限を保って安全な保管先に退避する。作業ディスク上のリリースコピーだけを災害対策とみなさない。
3. 隔離環境に復元し、ファイル・DB の整合性と件数・参照を検証する。移行前だけでなく**新 Bot による書き込み後**の逆変換・切り戻しを検証し、取り戻せないデータと許容損失を事前合意する。切り替え後のデータを古いスナップショットで無条件に上書きしない。
4. テスト用 Bot と隔離データで Discord コマンド・権限・scope・VC・辞書・9 TTS・フェイルオーバー・LLM・音声効果・Web の既存パスキー・画像生成停止・タイマー・再起動を検証する。共有本番トークンによる新旧同時稼働や共有データへの二重書き込みを避ける。テスト Bot で検証できない事項は別途切り替え時のチェックに残す。
5. 切り替え作業の担当、停止時間、復旧の判断期限、バックアップ保管先、許容データ損失、監視期間を事前合意。最終停止→処理完了を確認→最終バックアップ・整合性照合→移行→新 Bot のみ起動→検証の順に実施。失敗時は新旧を排他し、新側の書き込みを保全してから検証済み手順で旧側に戻す。

## 未実施・調査上の限界

本番データのコピー、バックアップの取得・復元、切り替え、実 Discord/VC、実 TTS/LLM、Web ブラウザ、公開範囲、外部 API、VRAM 動作の検証は今回行っていない。外部の全書き込み元と保存先、追加ファイルの役割も未確定。比較対象147ファイル以外の差分、実データ形式と整合性、実トラフィック下の互換性は保証できない。公開前の実機試験と明示承認が必要。
