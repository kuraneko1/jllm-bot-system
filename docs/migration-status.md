# 旧 jllm-voice との互換性ステータス

この文書は、最終的な完全互換を目指す移行の進行状況を管理します。「実装した」「テストした」「実環境で確認した」を分けて記録し、旧機能が残っていないかを確認します。

## 現状の位置付け

| 領域 | 状態 | 確認内容 |
| --- | --- | --- |
| Phase 1: plugin kernel | 実装・検証済み | discovery、binding、設定検証、lifecycle、CLI、fake plugin。31件＋1件のテスト、Ruff、wheel clean install |
| Phase 2: Discord・WavRank | 実装・mock検証済み | adapter、storage、制限設定、WavRank。49件＋11件の結合を含むテスト。実 Discord は未確認 |
| Phase 3: TTS・Reader | 実装・mock検証済み | 独立 contract、4種類のprovider、direct adapter、failover router、音声効果、辞書、再生 queue、Reader 補助 service、Reader 中核、/mytalk、/restrict、/forcevoice、bookmark、メディア辞書、/dict、/reading、HTML登録、/tts、VRAM制御。実 Discord・実音声は未確認 |
| Phase 4: LLM・会話 | 実装・mock検証済み | provider、router、読み上げモード、翻訳 Cog、会話・session・Kitasan。実 LLM・実 Discord は未確認 |
| Phase 5: Web | 実装・mock/HTTP検証済み | 認証、共通 board、管理 API、各 panel。実ブラウザ・実公開環境は未確認 |
| Phase 6: 独立機能 | 実装・互換/実画像検証済み | Miq、Umigame、Anima、D1、Playground。実フォントのMiq生成も確認 |
| 本番 data 移行 | 部分調査・未移行 | 配置と規模は調査済み。全ファイルの所有者確認、移行、逆変換、rollback は未実施 |
| 実 Discord・実音声 | 未検証 | 現在の検証は mock・fixture・clean install が中心 |
| 最終互換監査 | 自動検証済み | 旧 tests の追加移植を含む1275件＋builtins 11件が成功（2026-10-29 時点）。Ruff、wheel clean install、profile 検証も完了 |

## 本番準備の追加作業（2026-10-29）

- x230 の構成を読み取り調査し、[調査結果](x230-cutover-investigation.md) と [切り替え手順案](x230-cutover-runbook.md) を作成した。現地の本番データは変更していない。
- Reader の許可リストと永続トグル、TTS の旧 ID・表示名・状態保護、Web パスキーと保存先、音声設定、Miq・今北産業の発火語、旧環境ファイル読み込みを改善し、合成データとモックを使った回帰テストを追加した。
- 旧設定の移行先を [対応表](legacy-configuration-map.md) にまとめた。表は自動変換ではなく、未対応項目の確認も残る。
- 停止中のデータを退避する [限定スナップショット](../tools/offline_snapshot.md) と [明示台帳のスナップショット](../tools/tree_snapshot.md) を追加した。どちらも停止確認・台帳の正しさ・全 writer の停止を運用者に委ね、本番データでの検証は未実施。
- [統合プロファイルの雛形](../profiles/production.example.yaml) は 9 系統の実設定や ID を含まない。仮のデータと外部通信を禁止した環境で起動・停止を検証したもので、本番用設定ではない。
- スナップショットと復元の手順・ツールは限定された条件下の準備段階であり、実本番データの一貫した退避・復元、書き込み後の逆移行、writer 排他は未検証。
- 本番の設定変換、実 Discord・VC・TTS・LLM・Web の確認と切り替えは未実施。過去のテスト結果を本番互換の証明として扱わない。

## 旧環境の baseline

- 移行元は `/home/ryu/irodori-tts/jllm-voice` の現在の working tree とする。
- Git HEAD には無い変更・未追跡機能があるため、commit だけを正としない。
- 本番実行データ・認証情報は x230 が管理元であり、コピーせず、このリポジトリへも含めない。
- 旧環境では `.venv/bin/python -m unittest discover tests -v` を実行し、**987件すべて成功**を確認した。これがコード互換の基準である。
- 旧テストには WavRank と command 同期の専用テストがないため、移行側で characterization test を追加する。
- 旧テストでは未閉鎖 file に関する ResourceWarning が残っている。移行時の新たな不具合と区別する。
- x230 との差分、実データ inventory、実 Discord・実音声の確認は最終切替前に別途記録する。

## 完全互換の判定に必要なもの

1. 旧 slash command、引数、権限、表示、添付、エラーメッセージ。
2. message の scope、trigger、抑止、複数応答、reaction。
3. TTS の声、速度、streaming、failover、queue、skip、音声効果。
4. 翻訳・会話の session、fork、model、prompt、履歴、公開範囲。
5. Web の認証、API、表示、登録・承認、rate limit。
6. Miq を含む独立機能の UX と安全制限。
7. 旧 data の件数・参照・path・時刻を保つ移行と、書き込み後の rollback。
8. x230 の ffmpeg と実 Discord での確認。

各項目は、実装だけで完了にせず、対応する互換テストと実環境での確認結果を追記する。
