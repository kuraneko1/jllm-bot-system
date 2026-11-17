export function mount(panel) {
  panel.innerHTML = `
    <div class="intro" style="display:flex;justify-content:space-between;align-items:flex-end;gap:20px;flex-wrap:wrap">
      <div>
        <h2 style="margin:0;font-size:22px">全体像</h2>
        <p style="margin:4px 0 0;color:var(--muted)">Discord・x230（ボット）・GPU機の役割と、いま使える機能の一覧です。</p>
      </div>
      <div class="ov-intro-note">音声は x230 で受け取り、重い処理だけ GPU機 に投げます。<br>このタブは静的な説明で、自動更新しません。</div>
    </div>

    <section class="card">
      <div class="card-head">
        <div><h2>概念図</h2><p>音声は x230 で受け取り、重い処理だけ GPU機 に投げます。落ちても自動で退避します。</p></div>
        <span class="ov-badge">構成</span>
      </div>
      <div class="ov-diagram">
        <div class="ov-col discord">
          <h3><span class="ov-dot p"></span>Discord</h3>
          <div class="ov-chip">テキスト投稿 <span>→ 読み上げ</span></div>
          <div class="ov-chip">VCの音声 <span>→ 聞き取り</span></div>
          <div class="ov-chip">返信 + <b>miq</b> <span>→ 引用画像</span></div>
          <div class="ov-chip">スラッシュコマンド <span>/join ・ /tts</span></div>
        </div>
        <div class="ov-flows">
          <div class="ov-flow"><div>音声（VC）</div><div class="ov-arrow">→</div></div>
          <div class="ov-flow back"><div>読み上げ音声</div><div class="ov-arrow">←</div></div>
          <div class="ov-flow"><div>HTTP</div><div class="ov-arrow">→</div></div>
        </div>
        <div class="ov-col x230">
          <h3><span class="ov-dot t"></span>x230（ボット本体）</h3>
          <div class="ov-chip">読み上げ <span>辞書・速度・音量</span></div>
          <div class="ov-chip">空気読み <span>喋っている間だけ下げる</span></div>
          <div class="ov-chip">聞き取り <span>パケットの到着で判定</span></div>
          <div class="ov-chip">miq 引用画像 <span>描画はCPU</span></div>
          <div class="ov-chip">翻訳・会話の入口</div>
          <div class="ov-chip">入室案内・ユーザー制限</div>
        </div>
        <div class="ov-flows">
          <div class="ov-flow"><div>合成要求</div><div class="ov-arrow">→</div></div>
          <div class="ov-flow back"><div>音声WAV</div><div class="ov-arrow">←</div></div>
          <div class="ov-flow back"><div>疎通NGなら退避</div><div class="ov-arrow">←</div></div>
        </div>
        <div class="ov-col gpu">
          <h3><span class="ov-dot a"></span>GPU機（RTX 4070）</h3>
          <div class="ov-chip">読み上げエンジン <span>speech.cpp / Irodori</span></div>
          <div class="ov-chip">予備エンジン <span>VOICEVOX（x230内蔵）</span></div>
          <div class="ov-chip">翻訳・会話LLM <span>Sarashina / ローカル / atmos</span></div>
          <div class="ov-chip">画像生成 <span>anima</span></div>
          <div class="ov-chip">VRAM制御 <span>全開放・復帰</span></div>
        </div>
      </div>
      <div class="ov-diagram ov-layer-row">
        <div class="ov-layer">
          <h3><span class="ov-dot b"></span>Web UI（ダッシュボード）</h3>
          <div class="ov-chip">音声ファイル・特殊読み・GIF</div>
          <div class="ov-chip">話者クローン <span>登録・削除</span></div>
          <div class="ov-chip">読み上げエンジン切替</div>
          <div class="ov-chip">聞き取り <span>公開・音量カーブ</span></div>
          <div class="ov-chip">実験場・AIチャット・レコーダー</div>
        </div>
      </div>
    </section>

    <div class="ov-cmd-grid">
      <section class="card ov-feature-card">
        <div class="card-head"><div><h2>コマンド一覧</h2><p>Discordのスラッシュコマンド</p></div><span class="ov-badge">操作</span></div>
        <div class="ov-table-wrap">
          <table class="ov-cmd">
            <tr><th>コマンド</th><th>説明</th></tr>
            <tr><td><code>/join</code></td><td>VCに参加して読み上げを開始</td></tr>
            <tr><td><code>/leave</code></td><td>読み上げを停止して退出</td></tr>
            <tr><td><code>/mytalk</code></td><td>自分の読み上げ設定</td></tr>
            <tr><td><code>/dict add</code></td><td>語句を登録wavに差し替える辞書</td></tr>
            <tr><td><code>/emoji</code></td><td>非言語音絵文字の有効/無効</td></tr>
            <tr><td><code>/help</code></td><td>読み上げの使い方</td></tr>
            <tr><td><code>/translate</code></td><td>翻訳・会話の設定パネル</td></tr>
            <tr><td><code>/img</code></td><td>画像を生成</td></tr>
            <tr><td><code>/tts status</code> / <code>switch</code> / <code>check</code></td><td>読み上げエンジンの確認・切替</td></tr>
            <tr><td><code>/forcevoice</code></td><td>声の強制変更 <span class="ov-owner">管理者</span></td></tr>
            <tr><td><code>/resettalk</code></td><td>話者設定リセット <span class="ov-owner">管理者</span></td></tr>
            <tr><td><code>/restrict</code></td><td>再生・無視設定 <span class="ov-owner">管理者</span></td></tr>
            <tr><td><code>/wavplay</code></td><td>辞書wav再生切替 <span class="ov-owner">管理者</span></td></tr>
            <tr><td><code>/imggen</code></td><td>画像生成サーバ制御 <span class="ov-owner">管理者</span></td></tr>
            <tr><td><code>/bookmark</code></td><td>保存投稿一覧 <span class="ov-owner">管理者</span></td></tr>
          </table>
        </div>
      </section>

      <section class="card ov-feature-card">
        <div class="card-head"><div><h2>コマンド以外の操作</h2><p>言葉と声で動かす</p></div><span class="ov-badge">操作</span></div>
        <div class="ov-trigger"><b>miq</b><span>返信＋「miq」で引用画像を生成</span></div>
        <div class="ov-trigger"><b>s</b><span>「s」だけ送ると読み上げをスキップ（⏩ リアクションも可）</span></div>
        <div class="ov-trigger"><b>; / :</b><span>先頭に付けるとその投稿だけ読まない</span></div>
        <div class="ov-trigger"><b>@ボット</b><span>会話モードで応答・要約・Web検索</span></div>
      </section>
    </div>

    <section class="card ov-feature-card">
      <div class="card-head">
        <div><h2>読み上げの効果（本文に書くトークン）</h2><p>投稿の本文に <code>ec3</code> のように書くと、その部分だけ音が変わる</p></div>
        <span class="ov-badge">効果 15種</span>
      </div>
      <div class="ov-fx">
        <div class="ov-row"><code>f2</code><span>速さ（<b>f&lt;数字&gt;</b>＝倍速）</span></div>
        <div class="ov-row"><code>1〜20</code><span>強さ（省略時は 1）</span></div>
        <div class="ov-row"><code>rb</code><span>リバーブ</span></div>
        <div class="ov-row"><code>ec / ecl</code><span>エコー / 従来エコー</span></div>
        <div class="ov-row"><code>ch / vi</code><span>コーラス / ビブラート</span></div>
        <div class="ov-row"><code>lo / bs</code><span>ローファイ / 重低音</span></div>
        <div class="ov-row"><code>pi</code><span>ピッチ（<code>pi4</code>, <code>pi-3</code> / <code>-pi3</code>）</span></div>
        <div class="ov-row"><code>rv / lm / wd</code><span>逆再生 / ラウド / 広がり</span></div>
        <div class="ov-row"><code>ph / ex</code><span>フェイザー / エキサイター</span></div>
        <div class="ov-row"><code>kk / tc</code><span>キラキラ / ツリーチャイム</span></div>
      </div>
    </section>

    <div class="ov-cmd-grid">
      <section class="card ov-feature-card">
        <div class="card-head"><div><h2>掛かる範囲のルール</h2></div><span class="ov-badge">書き方</span></div>
        <div class="ov-rules">
          <div class="ov-r"><code>前半 後半 ec3</code><span>文末に書くと全体に掛かる</span></div>
          <div class="ov-r"><code>前半 ec3 後半</code><span>直前の区間（「前半」）だけに掛かる</span></div>
          <div class="ov-r"><code>(後半)ec3</code><span>括弧で囲んだ部分だけに掛かる（入れ子可）</span></div>
          <div class="ov-r"><code>本文 ec3 bs20</code><span>並べた順に重ねて掛かる</span></div>
        </div>
      </section>
      <section class="card ov-feature-card">
        <div class="card-head"><div><h2>読み上げを止める・飛ばす</h2></div><span class="ov-badge">操作</span></div>
        <div class="ov-trigger"><b>s / ⏩</b><span>再生中の読み上げをスキップ</span></div>
        <div class="ov-trigger"><b>; / :</b><span>先頭に書くと読み上げない</span></div>
        <div class="ov-trigger"><b>秒数</b><span><code>(文)3s</code> や <code>語句 3s</code> で再生上限を指定</span></div>
      </section>
    </div>`;
}
