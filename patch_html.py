"""
既存のHTMLに今回の修正を反映します。
使い方:  python patch_html.py index.html      → index_patched.html が出来ます（元ファイルは変更しません）
反映内容: ①パイプライン判定カード(last_result.json表示) ②APIキーのWorker経由化(バックアップからキー除外)
          ③ナンピン計算(買い増し株数入力) ④ログ集計の数値判定
"""
import sys
src = sys.argv[1] if len(sys.argv) > 1 else "index.html"
h = open(src, encoding="utf-8").read()

def rep(old, new, label):
    global h
    assert h.count(old) == 1, f"【失敗】{label}: 置換対象が見つかりません（HTMLを編集済みの可能性）"
    h = h.replace(old, new)

# ① 判定カード（地合い画面の上部）
rep('<div class="score-hero">', '''<div id="pipeline-card" class="ai-report-card" style="border-style:solid;">
    <div class="ai-report-hdr"><span><i class="fa-solid fa-robot"></i> パイプライン判定（引け後・J-Quants）</span>
      <span id="pl-time" style="font-size:.7rem;color:var(--muted);">未取得</span></div>
    <div id="pl-body" class="ai-report-body">last_result.json を読み込み中...</div>
  </div>
  <div class="score-hero">''', "判定カード")

# ② Worker経由化
rep('''  if (!window.GoogleGenerativeAI) {
    throw new Error('GoogleGenerativeAI ライブラリが読み込まれていません。');
  }
  if (!geminiApiKey) {
    throw new Error('Gemini APIキーが設定されていません。設定画面（歯車アイコン）でAPIキーを保存してください。');
  }

  const genAI = new window.GoogleGenerativeAI(geminiApiKey);
  const model = genAI.getGenerativeModel({ model: modelName });''',
'''  let model;
  if (workerUrl) {   // 推奨: APIキーはWorker側に保管し、ブラウザには置かない
    model = { generateContent: async (p) => {
      const r = await fetch(workerUrl, { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prompt: p, model: modelName }) });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const j = await r.json();
      return { response: { text: () => j.text } };
    }};
  } else {
    if (!window.GoogleGenerativeAI) throw new Error('GoogleGenerativeAI ライブラリが読み込まれていません。');
    if (!geminiApiKey) throw new Error('設定画面でCloudflare Worker URL（推奨）またはAPIキーを設定してください。');
    model = new window.GoogleGenerativeAI(geminiApiKey).getGenerativeModel({ model: modelName });
  }''', "Worker経由化")
rep("    apiKey: geminiApiKey,\n", "", "バックアップからAPIキー除外")
rep('        if(d.apiKey) localStorage.setItem("bnf_gemini_key", d.apiKey);\n', "", "復元時のAPIキー")
rep('<div class="setting-lbl">Gemini API Key</div>',
    '<div class="setting-lbl">Gemini API Key（Worker未使用時のみ / Worker利用を推奨）</div>', "設定ラベル")

# ③ ナンピン計算
rep('''<div><div class="cell-lbl">口座残高（円）</div><input type="number" id="pos-cash" placeholder="例: 3000000" oninput="calcPos()"></div>''',
'''<div><div class="cell-lbl">口座残高（円）</div><input type="number" id="pos-cash" placeholder="例: 3000000" oninput="calcPos()"></div>
        <div><div class="cell-lbl">買い増し株数</div><input type="number" id="pos-add" placeholder="例: 100" oninput="calcPos()"></div>''', "買い増し入力欄")
rep("function calcPos(){", "function calcPosOld(){", "旧ナンピン関数")

# ④ ログ集計
rep("savedLogs.unshift(entry);", "entry.kairiNum = parseFloat(kairi); savedLogs.unshift(entry);", "ログ数値保存")
rep("savedLogs.filter(l=>l.kairi.includes('-15')).length", "savedLogs.filter(l=>logKairi(l)<=-15).length", "S級集計")
rep("savedLogs.filter(l=>l.kairi.includes('-10')).length", "savedLogs.filter(l=>logKairi(l)<=-10&&logKairi(l)>-15).length", "A級集計")

NEW_JS = r'''
/* ════ 追加機能（patch_html.py） ════ */
function logKairi(l){ return (l.kairiNum !== undefined) ? l.kairiNum : parseFloat(l.kairi); }

function calcPos(){
  const v = id => parseFloat(document.getElementById(id).value) || 0;
  const buy=v('pos-buy'), qty=v('pos-qty'), now=v('pos-now'), cash=v('pos-cash'), add=v('pos-add');
  const box = document.getElementById('pos-result');
  if(!buy || !qty || !now){ box.style.display = "none"; return; }
  const f = n => Math.round(n).toLocaleString();
  const pnl = (now - buy) * qty, pnlPct = (now - buy) / buy * 100;
  let html = `<div><b>含み損益:</b> <span style="color:${pnl>=0?'var(--red)':'var(--blue)'};font-weight:800;">${f(pnl)} 円 (${pnlPct.toFixed(2)}%)</span></div>
    <div><b>現在の評価額:</b> ${f(now*qty)} 円</div>`;
  if(add > 0){
    const cost = now * add, newQty = qty + add, avg = (buy*qty + now*add) / newQty;
    const need = (avg - now) / now * 100;
    html += `<div style="margin-top:8px;border-top:1px solid var(--bdr);padding-top:8px;"><b>${add}株ナンピンした場合</b></div>
      <div>追加資金: ${f(cost)} 円 / 合計 ${newQty.toLocaleString()} 株</div>
      <div>平均取得単価: <b>${avg.toFixed(1)} 円</b>（建値回復に必要な上昇率 ${need.toFixed(2)}%）</div>`;
    if(cash > 0){
      html += cost > cash ? `<div style="color:var(--red);">⚠ 資金不足（残高 ${f(cash)} 円）</div>`
                          : `<div>実行後の残高: ${f(cash-cost)} 円</div>`;
    }
  } else {
    html += `<div style="margin-top:6px;font-size:.78rem;color:var(--muted);">「買い増し株数」を入れると平均取得単価を計算します。</div>`;
  }
  box.style.display = "block"; box.innerHTML = html;
}

async function loadPipelineResult(){
  const body = document.getElementById('pl-body'), tm = document.getElementById('pl-time');
  if(!body) return;
  try{
    const r = await fetch('last_result.json?ts=' + Date.now());
    if(!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    const buy = d.market_status === 'BUY';
    let h = `<div style="font-size:1.3rem;font-weight:900;color:${buy?'var(--green)':'var(--muted)'};">${buy?'🔥 BUY':'⏸ WAIT'}</div>
      <div style="margin:6px 0;">${escapeHtml(d.reason||'')}</div>
      <div style="font-size:.74rem;color:var(--muted);">データ基準日: ${escapeHtml(d.data_asof||'?')} / 値下がり ${escapeHtml(d.market?.prime_declining_count??'?')} 銘柄 / 先物: ${escapeHtml(d.market?.nikkei_futures_trend||'?')} / 通過 ${escapeHtml(d.screened_count??'?')} 銘柄</div>`;
    (d.warnings||[]).forEach(w => { h += `<div style="color:var(--orange);margin-top:6px;">⚠ ${escapeHtml(w)}</div>`; });
    (d.buy_candidates||[]).forEach(b => {
      h += `<div style="margin-top:10px;padding:10px;background:var(--s2);border-radius:10px;">
        <b>[${escapeHtml(b.code)}] ${escapeHtml(b.name)}</b>
        <div>指値 <b>${escapeHtml(b.entry_price)}</b> / 利確 <span style="color:var(--green);">${escapeHtml(b.take_profit_price)}</span> / 損切り <span style="color:var(--red);">${escapeHtml(b.stop_loss_price)}</span></div>
        <div style="font-size:.78rem;color:var(--muted);">${escapeHtml(b.comment||'')}</div></div>`;
    });
    body.style.whiteSpace = 'normal'; body.innerHTML = h;
    tm.innerText = (d.generated_at||'').replace('T',' ');
  }catch(e){
    body.innerText = 'last_result.json を読み込めません（パイプライン未実行、またはファイル未公開）: ' + e.message;
  }
}
window.addEventListener("DOMContentLoaded", () => { loadPipelineResult(); setInterval(loadPipelineResult, 60000); });

'''
rep("function resetAllApp(){", NEW_JS + "function resetAllApp(){", "追加JS")

out = src.replace(".html", "_patched.html")
open(out, "w", encoding="utf-8").write(h)
print("完了:", out)
