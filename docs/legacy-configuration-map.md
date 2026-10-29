# 旧設定から新しい構成への対応表

旧 `jllm-voice` の設定（`.env`、`bot-policy.env`、`tts-backends.json` など）を、新しいプラグイン構成の profile へ移すときの対応表です。**自動変換はありません。** 値は各自の管理下で移し、この文書・リポジトリ・ログへ実値を書かないでください。

## 前提

- 旧実装は `.env` を読んだあと `bot-policy.env` を上書き優先で読みます。新実装は自動探索せず、`jllm run --env-file ... --policy-file ...` のように明示します。優先順位は **policy > 起動プロセスの環境 > env ファイル**です（[CLI](../src/jllm/cli.py)）。
- 新実装の設定は profile の `plugins.<id>.config` に置きます。環境変数は「秘密を profile に書かない」ための経路で、旧変数が自動で profile に反映されるわけではありません。
- `storage.local.config.data_dir` を変えても、Reader・Irodori・Miq・WavRank などの個別パスは自動追随しません。各項目を明示します。
- 移行の実施手順と安全確認は [切り替え手順書](x230-cutover-runbook.md) を参照してください。

## 共通・権限

| 旧設定 | 新設定 | 注意 |
| --- | --- | --- |
| `DISCORD_TOKEN` | `adapter.discord.config.token_env` が指す環境変数 | 値は YAML に書かない。旧トークンと同時稼働しない。 |
| `DATA_DIR` | `storage.local.config.data_dir` | 既定は作業ディレクトリ相対の `data`。 |
| `OWNER_ID` | `feature.reader`、`feature.translation`、`dictionary.media` の `owner_id` | 1か所では足りない。 |
| `TTS_SWITCH_USER_IDS` | `feature.reader`、`feature.translation` の `tts_switch_user_ids` | 両方に設定する。 |
| `MEDIA_APPROVER_ID` | `dictionary.media.approver_id` と `feature.html-library.approver_id` | 両 store 有効時は media 側が優先される経路があるため揃える。 |
| `HTML_REQUIRE_APPROVAL` | `feature.html-library.require_approval` | |
| `USER_RESTRICTIONS_PATH` | `policy.restrictions.config.path` | `data_dir` の外は指定できない。外にあるなら移設する。 |

## 読み上げ・音声

| 旧設定 | 新設定 | 注意 |
| --- | --- | --- |
| `MESSAGE_CHANNEL_WHITELIST` | `feature.reader.allowed_channel_ids` と `feature.translation.allowed_channel_ids` | **空配列は「全許可」ではなく「対象なし」**（旧実装と同じ）。両機能に設定する。 |
| `GLOBAL_REACTION_WORDS` | `feature.reader.global_reaction_words` | 許可外チャンネルでの反応語。 |
| `DEFAULT_SPEED`、`DEFAULT_SPEAKER` | `feature.reader` と `speech.playback` | 読み上げ側と再生側の両方に効く項目がある。 |
| `DEFAULT_VOLUME`、`SAMPLE_VOLUME`、`ALLOW_NONVERBAL_EMOJIS`、`TAIL_SILENCE_MS` | `speech.playback.config` | |
| `DUCK_ENABLED`、`DUCK_*` | `feature.reader.duck_enabled` と `speech.playback` | 曲線ファイルは `web.dashboard.config.duck_curve_path` も合わせる。 |
| `VC_ANNOUNCEMENT_ENABLED`、`VC_ANNOUNCEMENT_COOLDOWN_SECONDS` | `feature.reader` の同名項目 | |
| `VC_ANNOUNCEMENT_VOICE`、`VC_ANNOUNCEMENT_CACHE_DIR`、`VC_ANNOUNCEMENT_CACHE_LIMIT`、`VC_ANNOUNCEMENT_HISTORY_PATH` | `voice.settings` の `announcement_*` | 実使用は voice.settings 側。Reader 側にも同名項目があるが通常は voice.settings を設定する。 |
| `VOICE_SPEEDS_PATH`、`VOICE_VOLUMES_PATH` | `voice.settings.speeds_path`、`volumes_path` | 保存形式は互換。 |
| `USER_VOICES_PATH`、`FORCED_VOICES_PATH` | `voice.settings.user_voices_path`、`forced_voices_path` | 値は文字列へ正規化して読む（旧と同じ）。 |
| `MYTALK_HIDDEN_VOICES` | `feature.reader.mytalk_hidden_voices` | 未指定時は旧既定の2名。`[]` で非表示なし。 |
| `BOOKMARK_REACTION`、`BOOKMARK_NOTIFY` など | `feature.bookmarks`、`feature.reader` | |
| `TOGGLES_JSON` | `feature.reader.toggles_path` | 本番では `require_existing_toggles: true` を推奨。不在・破損・型不正で起動を止める。 |
| `AUDIO_SILENCE_THRESHOLD_DB`、`AUDIO_SILENCE_KEEP_START_SECONDS`、`AUDIO_SILENCE_KEEP_END_SECONDS`、`AUDIO_LOUDNESS_I`、`AUDIO_LOUDNESS_TP`、`AUDIO_LOUDNESS_LRA` | `dictionary.sound.audio_processing` の `threshold_db`、`keep_start_seconds`、`keep_end_seconds`、`loudness_i`、`loudness_tp`、`loudness_lra` | 文字列の数値は ffmpeg 引数への注入を防ぐため形式を検証する。 |

## TTS

| 旧設定 | 新設定 | 注意 |
| --- | --- | --- |
| `tts-backends.json` の `backends[]` | 各 `provider.tts.<kind>.instances.<name>` | 旧 `kind` ごとに provider を選び、instance 名を付ける。 |
| 旧 backend の並び順・優先順 | `bindings.tts.router.tts.provider` の配列順 | |
| 旧 backend の `id` | `tts.router.config.backend_ids`（完全ID → 旧ID） | 指定するときは**全 provider を網羅**し、一意で空白なし。旧状態の `preferred`/`active`/`streaming` をそのまま引き継げる。 |
| 旧 backend の `display` | `tts.router.config.backend_displays`（完全ID → 表示名） | 部分指定可。 |
| `TTS_STATE_FILE` | `tts.router.config.state_file` | `data_dir` 配下。 |
| `TTS_FAILOVER`、`TTS_HEALTH_SECONDS` | `tts.router.config.failover`、`health_seconds` | 保存済み状態の `failover` が優先される。 |
| `TTS_BACKEND`（単一構成時） | 単一の provider instance と binding | |
| `TTS_VOICEVOX_URL`、`TTS_VOICEVOX_SPEAKER` | `provider.tts.voicevox.instances.<name>.url`、`default_speaker` | |
| `TTS_SPEECHCPP_URL`、`_VOICES_DIR`、`_VOICE`、`SPEECHCPP_MODEL`、`SPEECHCPP_TIMEOUT` | `provider.tts.speechcpp` の `url`、`voices_dir`、`voice`、`model`、`timeout` | |
| `TTS_OPENAI_URL`、`_VOICE`、`_VOICES`、`_VOICES_PATH`、`_HEALTH_PATH`、`_MODEL`、`_SPEED_MODE`、`_DEFAULT_SPEED`、`_IGNORE_SPEED`、`_SEED`、`_SILENCE_RETRIES`、`_SILENCE_RMS_DBFS`、`_TIMEOUT`、`_STREAM` | `provider.tts.openai` の `url`、`voice`、`voices`、`voices_path`、`health_path`、`model`、`speed_mode`、`default_speed`、`ignore_speaker_speed`、`seed`、`silence_retries`、`silence_rms_dbfs`、`timeout`、`streaming` | |
| `TTS_IRODORI_URL`、`_VOICE`、`_VOICE_ORDER`、`IRODORI_CAPTION`、`IRODORI_MODEL`、`IRODORI_LORA_MAP_FILE`、`IRODORI_VOICES_CACHE`、`VOICE_DISABLED_PATH`、`TTS_IRODORI_STREAM`、`_CHUNK_MIN_CHARS`、`_FIRST_CHUNK_MIN_CHARS` | `provider.tts.irodori` の `url`、`voice`、`voice_order`、`caption`、`model`、`lora_map_file`、`voices_cache`、`disabled_voices_path`、`streaming`、`chunk_min_chars`、`first_chunk_min_chars` | API キーは `api_key_env` に**環境変数名**を書く。`data_dir` は storage と独立。 |
| `TTS_CONTROL_*`、`TTS_CONTROL_Z170_*` | 変更なし（環境変数として渡す） | 3点（host/key/known_hosts）が揃わないと lifecycle は動かない。どのバックエンドをどのホストの unit にするかは `tts.lifecycle.config.targets` に書く（`local_only` と `unmanaged_hosts` も同様）。lifecycle の対象は旧 ID なので `backend_ids` と整合させる。 |

旧 backend ごとの任意 `env`・未知キーは新 schema が受け付けません（`additionalProperties: false`）。必要な項目は provider の対応キーへ明示的に移してください。

状態ファイルの読み込みは、**壊れた JSON・未知のバックエンド ID を黙って先頭へ戻さず、起動を止めます**（旧実装は黙って既定へ戻し、そのまま上書きしていました）。`backend_ids` の設定漏れや保管先の取り違えを早く見つけるための意図的な違いです。

**旧実装へ戻す場合**: 新 ID（`provider.tts..../name`）を旧 `tts-backends.json` は解釈しません。`backend_ids` で旧 ID を維持していれば、状態ファイルの ID は旧形式のままです。切替前に状態ファイルを退避してください。

## LLM・翻訳・会話

| 旧設定 | 新設定 | 注意 |
| --- | --- | --- |
| `TRANSLATION_API_URL`、`TRANSLATION_API_KEY_FILE`、`TRANSLATION_BACKEND` | `llm.router.config.local`（`api_url`、`key_file`）と `backend` | router は自身の `local`/`sarashina`/`atmos` 設定から client を作る。provider 側だけ設定しても通信先は変わらない。 |
| `SARASHINA_API_URL`、`_KEY_FILE`、`_MODEL`、`_TRANSLATE_MODEL`、`_RPM`、`_TRANSLATE_RPM`、`_MAX_WAIT` | `llm.router.config.sarashina` の `api_url`、`key_file`、`model`、`translate_model`、`requests_per_minute`、`translate_rpm`、`max_wait` | 旧はモデル別 RPM を既定で持ち、新の `0` は「制限なし」。実効値を明示する。 |
| `ATMOS_API_URL`、`_MODEL`、`_TIMEOUT_SECONDS`、`_MAX_TOKENS`、`_CONTEXT` | `llm.router.config.atmos` の `api_url`、`model`、`timeout`、`max_tokens`、`context` | |
| `LLM_BACKEND_PATH` | `llm.router.config.state_file` | `data_dir` 配下。保存済みの全体 backend は起動時に優先して復元される。 |
| `TALK_CALL_SUFFIX`、`_SESSION_SECONDS`、`_MENTION_GATE`、`_OPEN_CHANNELS`、`_MODEL`、`_SYSTEM_PROMPT_FILE` | `feature.translation` の同名（小文字）項目 | |
| `TALK_MODEL` | `feature.translation.talk_model` と `web.dashboard.talk_model` | 旧は未設定時に `SARASHINA_MODEL` へ落ちる。旧の実効値を入れる。 |
| `SESSION_DIR` | `store.sessions.config.directory` | `data_dir` の外は不可。 |
| `TALK_LOG_PATH`、`TALK_STATS_PATH` | `feature.talk-log.config.log_path`、`stats_path` | |
| `KITASAN_ENABLED`、`_WORDS`、`_MESSAGES`、`_ITEM_CHARS`、`_MAX_CHARS` | `feature.kitasan.config` の `enabled`、`words`、`messages`、`item_chars`、`max_chars` | 下限は旧が丸め、新は schema で拒否（20/100）。移行時は丸めた値を入れる。読み上げ・翻訳の除外判定も同じ service を使う。 |
| `MIQ_WORDS` | `feature.miq.config.words` | 発火語は Miq 本体と、読み上げ・翻訳からの除外の両方に効く。空配列は schema で拒否。 |

## Web

| 旧設定 | 新設定 | 注意 |
| --- | --- | --- |
| `READINGS_DASHBOARD_URL` | `web.dashboard.config.public_url`（カンマ区切りの全 URL）と `feature.reader.dashboard_url`、`feature.html-library.dashboard_url`（表示用の先頭 URL） | パスキーの RP ID は `public_url` の先頭から決まる。複数 URL を渡すと Host で選ばれる。 |
| `READINGS_DASHBOARD_AUTH_FILE` | `web.dashboard.config.auth_file` | パスキーは `auth_file` の拡張子を `.passkeys.json` に変えた隣接ファイル。**両方**を退避・配置する。 |
| `DASHBOARD_HOST`、`DASHBOARD_PORT` | `web.dashboard.config.host`、`port` | 例は `host: 127.0.0.1`、`public_url` とポートを一致させる。 |
| `WEBCHAT_DIR` | `web.dashboard.config.webchat_dir` | |
| — | `web.dashboard.config.require_existing_passkey` | 本番では `true`。既存パスキーが無い・読めない場合は起動しない。最後の1件は削除できない。 |
| `D1_API_URL`、`D1_TIMEOUT_SECONDS` | `feature.umigame` と `web.dashboard` の同名項目 | D1 の起動制御は旧名の環境変数を直接読むため、client の URL と一致させる。 |
| `TURTLE_SOUP_PER_IP`、`TURTLE_SOUP_MAX_QUESTIONS` | `web.dashboard` の同名項目 | 旧は範囲を丸め、新は schema で拒否。 |

## 独立機能

| 旧設定 | 新設定 | 注意 |
| --- | --- | --- |
| `MIQ_SIZE`、`MIQ_TEXT_ALIGN`、`MIQ_LAYOUT`、`MIQ_MAX_CHARS`、`MIQ_EMOJI_CACHE_LIMIT` | `feature.miq.config` の `size`、`text_align`、`layout`、`max_chars`、`emoji_cache_limit` | |
| `MIQ_FONT_DIR` | 環境変数 `MIQ_FONT_DIR`（import 時に読む） | storage の `data_dir` とは連動しない。明示的に渡す。 |
| `MIQ_LOG_PATH`、`MIQ_EMOJI_CACHE_DIR` | `data_dir` 配下の `miq.jsonl`、`miq_emojis` | 個別パスの指定はできない。 |
| `UMIGAME_IDLE_MINUTES` | `feature.umigame.config.idle_minutes` | |
| `ANIMA_URL`、`_MODEL`、`_NEGATIVE`、`_WIDTH`、`_HEIGHT` | `imagegen.anima.config` の `base_url`、`model`、`negative`、`width`、`height` | 既定値が旧と異なる。空 model は送られないので明示する。 |
| `ANIMA_CONTROL_HOST`、`_KEY`、`_KNOWN_HOSTS` | 環境変数として渡す | plugin が旧名を直接読む。 |
| `WAV_RANK_*`（channel/archive/sounds/out/font/data dir） | `feature.wavrank.config` の `channel_id`、`archive`、`sounds`、`out_dir`、`font`、`data_dir` | 環境変数の自動参照はない。archive と channel の両方が必要。 |
| `VOSK_*` など音声認識 | 変更なし（環境変数） | |

## まだ確認が必要な項目

- 旧 `bot-policy.env` の全項目がこの表で網羅されている保証はありません。実ファイルと突き合わせ、未対応のものは設定先を追加するか、機能ごと無効化を決めてください。
- `ANIMA` と `Sarashina` の既定値（URL・モデル・RPM）は旧と異なります。**実効値の明示**が必要です。
- D1・TTS lifecycle・画像生成制御は環境変数を直接読むため、profile と環境変数の両方の整合が必要です。
- 空配列・0・空文字の意味は機能ごとに異なります。移行後に「拒否・許可・例外」を実際に試してください。
