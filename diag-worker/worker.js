// Cloudflare Worker: receives the zip PC2Sonos's "Export Diagnostics" posts
// and emails it to the developer via Resend. The email API key lives in the
// Worker's secrets, never in the app.
//
// Secrets/vars (see README.md in this folder):
//   RESEND_API_KEY  secret  - Resend API key
//   TO_EMAIL        var     - where diagnostics are delivered
//   APP_TOKEN       var     - shared token the app sends (public in the exe;
//                             only keeps casual drive-by posts out)
const MAX_BYTES = 6 * 1024 * 1024;

export default {
  async fetch(request, env) {
    if (request.method !== "POST") return new Response("POST only", { status: 405 });
    if (request.headers.get("X-PC2Sonos-Token") !== env.APP_TOKEN)
      return new Response("forbidden", { status: 403 });
    const buf = await request.arrayBuffer();
    if (buf.byteLength === 0 || buf.byteLength > MAX_BYTES)
      return new Response("bad size", { status: 413 });
    const name = (request.headers.get("X-PC2Sonos-Filename") || "PC2Sonos-Diagnostics.zip")
      .replace(/[^A-Za-z0-9._-]/g, "_").slice(0, 80);
    const version = (request.headers.get("X-PC2Sonos-Version") || "?").slice(0, 20);
    const note = (request.headers.get("X-PC2Sonos-Note") || "").slice(0, 300);

    let bin = "";
    const bytes = new Uint8Array(buf);
    for (let i = 0; i < bytes.length; i += 0x8000)
      bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));

    const r = await fetch("https://api.resend.com/emails", {
      method: "POST",
      headers: { Authorization: `Bearer ${env.RESEND_API_KEY}`, "Content-Type": "application/json" },
      body: JSON.stringify({
        from: "PC2Sonos Diagnostics <onboarding@resend.dev>",
        to: [env.TO_EMAIL],
        subject: `PC2Sonos diagnostics (v${version})`,
        text: `A user sent diagnostics from PC2Sonos v${version}.\n\n${note}`,
        attachments: [{ filename: name, content: btoa(bin) }],
      }),
    });
    return r.ok ? new Response("ok") : new Response("mail failed", { status: 502 });
  },
};
