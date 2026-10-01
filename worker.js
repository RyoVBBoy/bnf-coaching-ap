// Cloudflare Worker: Gemini APIキーをブラウザに置かないための中継
// 設定: Secret「GEMINI_API_KEY」= Geminiのキー / 変数「ALLOWED_ORIGIN」= https://あなたのユーザー名.github.io
export default {
  async fetch(req, env) {
    const cors = { "Access-Control-Allow-Origin": env.ALLOWED_ORIGIN, "Access-Control-Allow-Methods": "POST, OPTIONS", "Access-Control-Allow-Headers": "Content-Type" };
    if (req.method === "OPTIONS") return new Response(null, { headers: cors });
    if (req.method !== "POST" || req.headers.get("Origin") !== env.ALLOWED_ORIGIN)
      return new Response("forbidden", { status: 403, headers: cors });
    const { prompt, model } = await req.json();
    const m = ["gemini-2.5-flash", "gemini-2.5-pro"].includes(model) ? model : "gemini-2.5-flash";
    const r = await fetch(`https://generativelanguage.googleapis.com/v1beta/models/${m}:generateContent`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-goog-api-key": env.GEMINI_API_KEY },
      body: JSON.stringify({ contents: [{ parts: [{ text: String(prompt).slice(0, 8000) }] }] }),
    });
    if (!r.ok) return new Response("upstream " + r.status, { status: r.status, headers: cors });
    const j = await r.json();
    const text = (j.candidates?.[0]?.content?.parts || []).map(p => p.text || "").join("");
    return new Response(JSON.stringify({ text }), { headers: { ...cors, "Content-Type": "application/json" } });
  },
};
