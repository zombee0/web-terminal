// Private control-plane relay for Boxes whose egress cannot reach app.boxlite.ai.
// No terminal traffic, user JWTs, arbitrary destinations, or request logging.
const encoder = new TextEncoder();
async function recoverManager(): Promise<Response> {
  const box = Deno.env.get("WT_MANAGER_BOX_ID");
  const entry = Deno.env.get("WT_MANAGER_URL");
  if (!box || !entry) return new Response("Not configured", { status: 503 });
  try {
    const health = await fetch(entry + "/healthz", { signal: AbortSignal.timeout(10000), redirect: "error" });
    if (health.ok && (await health.text()).trim() === "ok") return new Response("Healthy");
  } catch { /* Recover the recorded management Box; never create another. */ }
  const headers = { Authorization: "Bearer " + Deno.env.get("WT_BOXLITE_API_KEY"), "Content-Type": "application/json" };
  const base = "https://app.boxlite.ai/api/v1/boxes/" + encodeURIComponent(box);
  try {
    const info = await fetch(base, { headers, signal: AbortSignal.timeout(15000) });
    if (!info.ok) return new Response("Management Box unavailable", { status: 503 });
    const data = await info.json();
    if (!["running", "started"].includes(data.status)) {
      const started = await fetch(base+"/start", { method: "POST", headers, body: "{}", signal: AbortSignal.timeout(45000) });
      if (!started.ok) return new Response("Start pending", { status: 503 });
    }
    const code = `import pathlib,subprocess,fcntl
lock=open('/opt/web-terminal/start.lock','w')
fcntl.flock(lock,fcntl.LOCK_EX)
target='/opt/web-terminal/current/supabase/supervise.py'
alive=False
for p in pathlib.Path('/proc').glob('[0-9]*/cmdline'):
 try:
  if target.encode() in p.read_bytes().split(bytes([0])): alive=True
 except OSError: pass
if not alive:
 with open('/var/log/web-terminal-manager.log','ab') as log:
  subprocess.Popen(['/opt/web-terminal/venv/bin/python',target],stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
`;
    const response = await fetch(base+"/exec", { method: "POST", headers,
      body: JSON.stringify({ command: "/usr/local/bin/python3", args: ["-c", code], user: "root", timeout_secs: 20 }),
      signal: AbortSignal.timeout(30000) });
    return new Response(response.ok ? "Recovery requested" : "Recovery pending", { status: response.ok ? 200 : 503 });
  } catch { return new Response("Recovery pending", { status: 503 }); }
}
async function authorized(value: string): Promise<boolean> {
  const expected = Deno.env.get("WT_RELAY_TOKEN");
  if (!expected || value.length > 256) return false;
  const key = await crypto.subtle.importKey("raw", encoder.encode(expected),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign", "verify"]);
  const signature = await crypto.subtle.sign("HMAC", key, encoder.encode(expected));
  return crypto.subtle.verify("HMAC", key, signature, encoder.encode(value));
}

Deno.serve(async (request: Request) => {
  const deny = (status: number) => new Response("Request unavailable", { status });
  if (!await authorized(request.headers.get("authorization")?.replace(/^Bearer /, "") || "")) return deny(401);
  if (request.headers.get("upgrade")) return deny(400); // Executions use durable REST status polling.
  const input = new URL(request.url);
  const path = input.pathname.replace(/^\/boxlite-relay/, "").replace(/^\/api\//, "/");
  if (path === "/watchdog" && request.method === "POST") return await recoverManager();
  if (!/^\/v1\/boxes(?:\/[a-zA-Z0-9_-]+(?:\/[a-zA-Z0-9_/-]+)?)?$/.test(path)) return deny(404);
  if (!["GET", "POST", "PUT", "DELETE"].includes(request.method)) return deny(405);
  const headers = new Headers({ Authorization: "Bearer " + Deno.env.get("WT_BOXLITE_API_KEY") });
  const contentType = request.headers.get("content-type");
  if (contentType) headers.set("content-type", contentType);
  try {
    const response = await fetch("https://app.boxlite.ai/api" + path + input.search, {
      method: request.method, headers, redirect: "manual",
      body: ["GET", "HEAD"].includes(request.method) ? undefined : request.body,
      signal: AbortSignal.timeout(90000),
    });
    if (response.status >= 300 && response.status < 400) return deny(502);
    return new Response(response.body, { status: response.status,
      headers: { "Content-Type": response.headers.get("content-type") || "application/octet-stream",
                 "Cache-Control": "no-store" } });
  } catch { return deny(502); }
});
