# HTMLダッシュボードのURL統合（2026-11-15 実装済み）

> 実装結果: https://html.example.com へ移行済み。旧 Tailscale Funnel :10000 は停止。
> 配信は systemd user `html-viewer.service`（nginx コンテナ、enabled + linger）で常駐。

## 1. 目的

生成HTMLのダッシュボード（html-library のビューア）を、Tailscale の一時URL
（https://host.example.ts.net:10000）から、x230 の独自ドメイン配下
（https://html.example.com）へ移す。設定・案内・ブックマークを x230 側へ一本化する。

## 2. 現状

| 項目 | いまの値 |
| --- | --- |
| 共通ボード（本体） | https://bot.example.com → cloudflared → 127.0.0.1:8765 |
| HTMLダッシュボード | https://host.example.ts.net:10000 → Tailscale Funnel → 127.0.0.1:8030 |
| 配信 | docker の nginx（コンテナ名 html-viewer、127.0.0.1:8030） |
| 実体 | /home/ryu/jllm-voice/data/html（index.html + users/...） |
| 設定 | feature.html-library.config.viewer_base_url / web.dashboard.config.viewer_url が tailscale URL |
| DNS | html.example.com は未作成 |
| コンテナ永続化 | RestartPolicy = no（手動 docker run。再起動で復帰しない） |

cloudflared は tunnel <TUNNEL_ID>（systemd、enabled）。
ingress はホスト名単位（パス単位の振り分けは不可）。

## 3. 方針の比較

### A. サブドメイン html.example.com → 127.0.0.1:8030（推奨）

- cloudflared の ingress に1行追加するだけ。アプリ改修なし。
- 別オリジンになるので、共通ボードのパスキーCookie（reading_dashboard_session）が
  ユーザー生成HTMLに渡らない。現在の nginx の CSP/sandbox もそのまま使える。
- 旧 :10000 は移行期間だけ残せる。

### B. 同一ホストのパス /html/ に統合（非推奨）

- cloudflared はパスルーティングができないため、手前に nginx を置く必要がある。
- さらに同一オリジンになるため、ユーザー生成HTMLのJSが共通ボードの /api/... を
  セッションCookie付きで叩けてしまう（管理APIを含む）。Cookieはホスト単位なので
  パス分離では防げない。
- どうしてもパスにするなら、bot側で配信しつつ content.html を sandbox iframe に
  限定する等の追加設計が必要。今回は採らない。

### C. Cloudflare Workers/Transform でパス振り分け

- 可能だが設定が分散し、デバッグが難しい。Aで足りる。

## 4. 推奨構成（A）

    https://bot.example.com/  → cloudflared → 127.0.0.1:8765（bot dashboard）
    https://html.example.com/        → cloudflared → 127.0.0.1:8030（nginx html-viewer）

どちらも同じ tunnel で、ホスト名だけ分ける。Tailscale Funnel :10000 は廃止候補。

## 5. 実装手順

1. コンテナの永続化（先にやる）
   - html-viewer は RestartPolicy=no。--restart unless-stopped で作り直すか、
     systemd user unit（docker run を ExecStart に）にする。
   - unit 化して enable --now。停止時は docker stop html-viewer。
2. DNS
   - cloudflared tunnel route dns <TUNNEL_ID> html.example.com
   - 作成後 dig +short html.example.com が Cloudflare のIPを返すこと。
3. ingress 追加（catch-all より前）

~~~yaml
  - hostname: html.example.com
    service: http://127.0.0.1:8030
~~~

   - cloudflared tunnel ingress validate
   - systemctl restart cloudflared（全ホストで数秒の瞬断）
4. 動作確認（切替前）
   - curl -sI https://html.example.com/ → 200 / CSP / no-store
   - 一覧ページのカードから content.html が開ける
   - 旧 :10000 も並行して生きていること
5. 設定の切り替え
   - profiles.local/production.yaml:
     - feature.html-library.config.viewer_base_url: https://html.example.com
     - web.dashboard.config.viewer_url: https://html.example.com
   - bot を再起動（起動時に rebuild_dashboards が走る）
6. 再生成と表示確認
   - index.html のフッター「共通ボード」リンクが従来どおりであること
   - 共通ボードのヘッダー「HTMLダッシュボード」が新URLを開くこと
   - 新規HTML登録の返信メッセージが新URLになること
7. 旧URLの整理（1〜2週間後）
   - Tailscale Funnel の :10000 を停止
   - 必要なら旧URLから新URLへの301（nginx で Host を見て返す）を一時設置
8. ロールバック
   - 設定を tailscale URL に戻して再起動
   - ingress から html.example.com を削除して cloudflared 再起動
   - DNS は残っていても ingress が無ければ 404 なので影響なし

## 6. セキュリティ確認

- 共通ボードのCookieは host-only。html.* には送られない（DevToolsで確認）。
- nginx の CSP / X-Frame-Options / sandbox は変更しない。
- content.html は sandbox allow-scripts allow-top-navigation-by-user-activation のまま。
- HTMLはユーザー生成。共通ボードの管理APIと同一オリジンにしない（Aの必須条件）。
- 公開範囲は現状と同じ（ログイン不要の閲覧のみ、GET/HEAD限定）。

## 7. 可用性・運用

- cloudflared 再起動は全ホストに影響（数秒）。
- html-viewer が落ちると HTML ダッシュボードだけ 502。unit 化で自動復帰させる。
- ディスク: data/html は読み取り専用マウント。生成は bot 側。
- 監視: curl -fsS https://html.example.com/ >/dev/null を日次で。

## 8. 検証チェックリスト

- [ ] html.example.com の A/CNAME が tunnel を向く
- [ ] https で 200、証明書エラーなし
- [ ] CSP / no-store / sandbox ヘッダーが旧URLと同一
- [ ] 一覧→content.html→戻る が動く
- [ ] 共通ボードのヘッダーリンクが新URL
- [ ] 登録完了メッセージが新URL
- [ ] 旧 :10000 が移行期間中も動く
- [ ] 再起動後も html-viewer が自動復帰
- [ ] 共通ボードのCookieが html.* に送られない

## 9. 見積

- 作業 30〜60分（DNS/ingress 10分、unit化 15分、設定・再起動 10分、検証 15分）
- 瞬断: cloudflared 再起動の数秒、bot 再起動の約45秒

## 10. 未決事項

- サブドメイン名は html.example.com でよいか（別名でも可）
- 旧 :10000 をいつ止めるか（即日 / 1週間 / 1ヶ月）
- html-viewer を systemd unit にするか、docker の restart policy だけにするか
- HTMLダッシュボードに簡易な認証（Cloudflare Access 等）を付けるか
