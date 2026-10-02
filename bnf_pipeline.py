"""
BNF逆張りシグナル検知パイプライン (J-Quants V2版 / 引け後=EOD運用)

【重要】J-Quantsの株価四本値は毎営業日16:30頃に更新される「日足」です（リアルタイムではありません）。
 → 引け後(17:00以降)に実行し、「翌営業日に打診する候補」を出す使い方が前提です。
 → data_asof(データ基準日)が古い場合は安全側で見送りにします（プランによるデータ遅延対策）。

準備:  pip install requests pandas anthropic yfinance
環境変数: JQUANTS_API_KEY, ANTHROPIC_API_KEY, (任意) JQ_RPM=60 (プランの毎分上限), DISCORD_WEBHOOK_URL
実行:  python bnf_pipeline.py            # 1回
       python bnf_pipeline.py --git-push # 結果をGitHubへpush（HTML画面が読めるように）
"""
import argparse, json, os, re, subprocess, time
from datetime import date, datetime, timedelta
from pathlib import Path
import pandas as pd
import requests
from anthropic import Anthropic
NTFY_TOPIC = "ここを自分だけの長いランダム文字列に変える"

def notify(title, message, priority=3):
    try:
        requests.post("https://ntfy.sh", json={"topic": NTFY_TOPIC, "title": title,
                      "message": message[:1900], "priority": priority}, timeout=10)
    except Exception as e:
        print("[警告] ntfy通知に失敗:", e)

# ───── 設定 ─────
BASE = "https://api.jquants.com/v2"
HEADERS = {"x-api-key": os.getenv("JQUANTS_API_KEY", "")}
SLEEP = 60.0 / float(os.getenv("JQ_RPM", "60"))          # レート制限対策
CACHE = Path("cache"); CACHE.mkdir(exist_ok=True)
THRESH = {"large": -10.0, "tech": -15.0, "small": -25.0}   # 乖離率(%)以下
MIN_TURNOVER = {"prime": 5e9, "standard": 1e9, "growth": 1e9}
DECLINE_CRITICAL = 1400
MAX_CANDIDATES = 30
MAX_STALE_DAYS = 5
MODEL = os.getenv("BNF_MODEL", "claude-sonnet-5-5")
MARKETS = {"0111": "prime", "0112": "standard", "0113": "growth"}
TECH_S17 = {"9", "10"}   # 電機・精密 / 情報通信・サービスその他 → ハイテク扱い（要調整）

SYSTEM_PROMPT = """あなたは伝説的な個人投資家B.N.F（小手川隆）氏の思考とトレードロジックを100%搭載したAI判定エンジンです。
入力された【地合いデータ】と【スクリーニング済み候補銘柄リスト】を冷徹に査定し、エントリー可否を出力してください。

【判断アルゴリズム】
1. 地合い判定（最優先）：
   - `prime_declining_count`（値下がり銘柄数）が 1,400 未満の場合、全銘柄強制的に【見送り（ノーポジ維持）】と出力すること。
   - 先物（`nikkei_futures_trend`）が強い下落トレンド中の場合も、下げ止まりが確認できるまで全銘柄【見送り】とすること。
2. 個別銘柄査定（地合い条件クリア時のみ）：
   - セクター別乖離率（大型・バリュー -10%以下、ハイテク・主力グロース -15%以下、新興・小型 -25%以下）を満たし、板の崩れが止まっている銘柄のみ【買出動】とする。
   - データは引け後の日足です。指値は直近終値を基準に、翌営業日の打診位置として提示すること。
3. 出力：買い推奨は 銘柄コード/名前、打診買い指値、利確(+2〜3%)、損切り(買値から-1%程度) を明記。条件未達は「現在地合い不十分（値下がり数:〇〇）。全銘柄見送り（ノーポジ維持）」と一言で理由を述べる。

【出力形式】次のJSONのみ（説明文・Markdown不可）：
{"market_status":"BUY"|"WAIT","reason":"...","buy_candidates":[{"code":"","name":"","entry_price":0,"take_profit_price":0,"stop_loss_price":0,"comment":""}]}
WAITのとき buy_candidates は空配列。"""


# ───── J-Quants API ─────
def jq_get(path: str, **params) -> list[dict]:
    rows, key = [], None
    while True:
        p = dict(params, **({"pagination_key": key} if key else {}))
        for attempt in range(4):
            r = requests.get(f"{BASE}{path}", headers=HEADERS, params=p, timeout=60)
            time.sleep(SLEEP)
            if r.status_code == 429:
                time.sleep(15 * (attempt + 1)); continue
            break
        if r.status_code >= 400:
            raise RuntimeError(f"J-Quants {path} {r.status_code}: {r.text[:200]}")
        j = r.json(); rows += j.get("data", []); key = j.get("pagination_key")
        if not key:
            return rows


# ───── universe 自動生成（上場銘柄一覧 → 市場区分/クラス分け） ─────
def classify(row) -> str:
    if MARKETS.get(row["Mkt"]) == "growth":
        return "small"
    if str(row["S17"]) in TECH_S17:
        return "tech"
    sc = str(row.get("ScaleCat", ""))
    return "small" if ("Small" in sc or sc in ("-", "")) else "large"


def build_universe() -> pd.DataFrame:
    m = pd.DataFrame(jq_get("/equities/master"))
    m = m[m["Mkt"].isin(MARKETS)].copy()
    m["market"] = m["Mkt"].map(MARKETS)
    m["klass"] = m.apply(classify, axis=1)
    u = m.rename(columns={"Code": "code", "CoName": "name"})[["code", "name", "market", "klass"]]
    if os.path.exists("universe_overrides.csv"):          # 手動補正: code,klass
        ov = pd.read_csv("universe_overrides.csv", dtype=str).set_index("code")["klass"]
        u["klass"] = u["code"].map(ov).fillna(u["klass"])
    u.to_csv("universe.csv", index=False, encoding="utf-8-sig")
    return u


# ───── 日足の取得（過去分はキャッシュ、新しい日だけ取得） ─────
def load_bars(n_days: int = 45) -> pd.DataFrame:
    frames, today = [], date.today()
    for i in range(n_days, -1, -1):
        d = today - timedelta(days=i)
        if d.weekday() >= 5:
            continue
        f = CACHE / f"bars_{d:%Y%m%d}.csv"
        if not f.exists():
            if d == today and datetime.now().hour * 60 + datetime.now().minute < 17 * 60:
                continue                                  # 当日分は引け後(17時以降)のみ
            rows = jq_get("/equities/bars/daily", date=d.isoformat())
            pd.DataFrame(rows).to_csv(f, index=False)     # 空(祝日)も空ファイルとして保存
        try:
            df = pd.read_csv(f, dtype={"Code": str})
            if not df.empty:
                frames.append(df)
        except pd.errors.EmptyDataError:
            pass
    if not frames:
        raise RuntimeError("株価データが取得できません（APIキー/プランを確認）")
    return pd.concat(frames)


def earnings_codes(asof: date) -> tuple[set, bool]:
    """基準日の前後1営業日に決算発表予定の銘柄。取得失敗時は (空, False)。"""
    days = {asof - timedelta(days=1 if asof.weekday() else 3), asof,
            asof + timedelta(days=1 if asof.weekday() < 4 else 3)}
    out = set()
    try:
        for d in days:
            out |= {r["Code"] for r in jq_get("/fins/earnings-date", scheduled_date=d.isoformat())}
        return out, True
    except Exception as e:
        print("[警告] 決算予定日の取得に失敗:", e)
        return out, False


def futures_trend() -> str:
    """日経先物の5分足トレンド(yfinance・約20分遅延)。J-Quantsの先物は日次のため代用。"""
    try:
        import yfinance as yf
        c = yf.download("NIY=F", period="1d", interval="5m", progress=False)["Close"].squeeze().dropna().tail(12)
        chg = (c.iloc[-1] - c.iloc[0]) / c.iloc[0] * 100
        holds = c.tail(4).min() >= c.iloc[:-4].min()
        return ("下げ止まり" if holds else "下落中") if chg <= -0.3 else ("上昇中" if chg >= 0.3 else "横ばい")
    except Exception:
        return "不明"


# ───── ステップ1・2 ─────
def screen(universe: pd.DataFrame):
    bars = load_bars()
    bars["Date"] = pd.to_datetime(bars["Date"])
    asof = bars["Date"].max().date()
    excl, earn_ok = earnings_codes(asof)
    info = universe.set_index("code")
    px = bars.pivot_table(index="Date", columns="Code", values="AdjC")
    last = bars[bars["Date"] == bars["Date"].max()].set_index("Code")

    prime = [c for c in info.index[info["market"] == "prime"] if c in px.columns]
    declining = int((px[prime].iloc[-1] < px[prime].iloc[-2]).sum())

    cands = []
    for c, r in info.iterrows():
        if c in excl or c not in px.columns or c not in last.index:
            continue
        s = px[c].dropna()
        if len(s) < 25:
            continue
        ma25 = float(s.tail(25).mean()); kairi = (float(s.iloc[-1]) - ma25) / ma25 * 100
        turnover = float(last.loc[c, "Va"])
        if turnover < MIN_TURNOVER[r["market"]] or kairi > THRESH[r["klass"]]:
            continue
        cands.append({"code": c[:4] if c.endswith("0") else c, "name": r["name"], "market": r["market"],
                      "klass": r["klass"], "close": float(last.loc[c, "C"]),
                      "kairi_pct": round(kairi, 2), "turnover_oku": round(turnover / 1e8, 1)})
    cands.sort(key=lambda x: x["kairi_pct"])
    market = {"prime_declining_count": declining, "nikkei_futures_trend": futures_trend()}
    return cands[:MAX_CANDIDATES], market, asof, earn_ok


# ───── ステップ3 ─────
def judge(cands, market, asof) -> dict:
    stale = (date.today() - asof).days
    if stale > MAX_STALE_DAYS:
        return {"market_status": "WAIT", "reason": f"データ基準日が古い({asof}, {stale}日前)。プランの遅延を確認。見送り", "buy_candidates": []}
    if market["prime_declining_count"] < DECLINE_CRITICAL:
        return {"market_status": "WAIT", "reason": f"現在地合い不十分（値下がり数:{market['prime_declining_count']}）。全銘柄見送り（ノーポジ維持）", "buy_candidates": []}
    if not cands:
        return {"market_status": "WAIT", "reason": "条件を満たす候補銘柄なし", "buy_candidates": []}
    res = Anthropic().messages.create(model=MODEL, max_tokens=1500, system=SYSTEM_PROMPT, messages=[
        {"role": "user", "content": json.dumps({"market": market, "candidates": cands}, ensure_ascii=False)}])
    try:
        d = json.loads(re.search(r"\{[\s\S]*\}", res.content[0].text).group(0))
        assert d["market_status"] in ("BUY", "WAIT"); d.setdefault("buy_candidates", [])
        return d
    except Exception:
        return {"market_status": "WAIT", "reason": "LLM応答の解析に失敗（安全側で見送り）", "buy_candidates": []}


def run_once(git_push=False):
    uni = build_universe().set_index("code", drop=False)
    cands, market, asof, earn_ok = screen(uni)
    result = judge(cands, market, asof)
    result.update(market=market, screened_count=len(cands), data_asof=str(asof), mode="EOD",
                  generated_at=datetime.now().isoformat(timespec="seconds"),
                  warnings=[] if earn_ok else ["決算予定日の取得に失敗：決算除外が未適用です"])
    Path("last_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if (url := os.getenv("DISCORD_WEBHOOK_URL")) and result["market_status"] == "BUY":
        lines = [f"🔥 BUY 値下がり{market['prime_declining_count']} / 先物:{market['nikkei_futures_trend']}"] + [
            f"[{b['code']}] {b['name']} 指値{b['entry_price']} 利確{b['take_profit_price']} 損切{b['stop_loss_price']}"
            for b in result["buy_candidates"]]
        requests.post(url, json={"content": "\n".join(lines)[:1900]}, timeout=10)
       if result["market_status"] == "BUY":
        lines = [f"[{b['code']}] {b['name']} 指値{b['entry_price']} 利確{b['take_profit_price']} 損切{b['stop_loss_price']}"
                 for b in result["buy_candidates"]]
        notify("BNF 買い候補", "\n".join(lines), priority=4)
    else:
        notify("BNF 見送り", result["reason"], priority=2)
     if git_push:
        for cmd in (["git", "add", "last_result.json"], ["git", "commit", "-m", f"result {asof}"], ["git", "push"]):
            subprocess.run(cmd, check=False)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--git-push", action="store_true")
    run_once(ap.parse_args().git_push)
