// Trade inbox Worker: POST /api/trade with one trade as JSON, validated with ingest_inbox's rules, written to R2 as
// inbox/<timestamp>-<uuid>.json. It touches nothing else in R2: the pipeline is the only writer of the database.

interface Env {
  INBOX: { put(key: string, value: string, options?: { httpMetadata?: { contentType?: string } }): Promise<unknown> };
  ACCESS_TEAM_DOMAIN?: string; // e.g. yourteam.cloudflareaccess.com
  ACCESS_AUD?: string; // the Access application's audience tag
}

const FIELDS = new Set(["kind", "date", "cik", "symbol", "lot_id", "shares", "price", "amount", "fees", "note"]);
const KINDS = ["buy", "sell", "deposit", "withdrawal"];

// Same rules as pipeline/db.py ingest_inbox, plus type, date and amount checks: a string where ingest expects a
// number would crash or corrupt the nightly run, so it is stopped here.
export function validate(t: unknown): string | null {
  if (typeof t !== "object" || t === null || Array.isArray(t)) return "trade must be a JSON object";
  const r = t as Record<string, any>;
  const extra = Object.keys(r).filter((k) => !FIELDS.has(k)).sort();
  if (extra.length) return `unknown fields: ${extra.join(", ")}`;
  if (!KINDS.includes(r.kind)) return "kind must be buy, sell, deposit or withdrawal";
  if (typeof r.date !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(r.date) || isNaN(Date.parse(r.date)) ||
      new Date(r.date).toISOString().slice(0, 10) !== r.date) return "date must be a real YYYY-MM-DD date";
  for (const k of ["shares", "price", "amount", "fees"])
    if (r[k] != null && !(typeof r[k] === "number" && Number.isFinite(r[k]))) return `${k} must be a number`;
  for (const k of ["symbol", "lot_id", "note"]) if (r[k] != null && typeof r[k] !== "string") return `${k} must be text`;
  if (r.cik != null && !Number.isInteger(r.cik)) return "cik must be a whole number";
  if (r.fees != null && r.fees < 0) return "fees can't be negative";
  if ((r.kind === "buy" || r.kind === "sell") && !(r.symbol && r.shares > 0 && r.price > 0))
    return "buy and sell need symbol, shares > 0 and price > 0";
  if ((r.kind === "deposit" || r.kind === "withdrawal") && !(r.amount > 0))
    return "deposit and withdrawal need amount > 0";
  return null;
}

const b64url = (s: string) => Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/")), (c) => c.charCodeAt(0));
const part = (s: string) => JSON.parse(new TextDecoder().decode(b64url(s)));
let certs: (JsonWebKey & { kid?: string })[] | null = null; // cached per isolate, refetched on an unknown key id

async function accessKey(team: string, kid: string, refresh: boolean) {
  if (!certs || refresh) {
    const r = await fetch(`https://${team}/cdn-cgi/access/certs`);
    if (!r.ok) throw new Error(`Access certs: HTTP ${r.status}`);
    certs = ((await r.json()) as { keys: (JsonWebKey & { kid?: string })[] }).keys;
  }
  return certs.find((k) => k.kid === kid);
}

// Full check of the Cloudflare Access token: RS256 signature against the team's published keys, audience, issuer
// and expiry. Header presence alone would trust anything that reaches the Worker without passing Access.
export async function verifyAccess(jwt: string, team: string, aud: string, now = Date.now() / 1000) {
  try {
    const [h, p, s] = jwt.split(".");
    const header = part(h), claims = part(p);
    if (!s || header.alg !== "RS256") return false;
    const jwk = (await accessKey(team, header.kid, false)) ?? (await accessKey(team, header.kid, true));
    if (!jwk) return false;
    const key = await crypto.subtle.importKey("jwk", jwk, { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["verify"]);
    const ok = await crypto.subtle.verify("RSASSA-PKCS1-v1_5", key, b64url(s), new TextEncoder().encode(`${h}.${p}`));
    return ok && [claims.aud].flat().includes(aud) && claims.iss === `https://${team}` && claims.exp > now &&
      (claims.nbf ?? 0) <= now;
  } catch (e) {
    console.error(`Access token check failed: ${e}`);
    return false;
  }
}

const reply = (status: number, body: object) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    if (new URL(req.url).pathname !== "/api/trade") return reply(404, { error: "not found" });
    if (req.method !== "POST") return reply(405, { error: "POST only" });
    const jwt = req.headers.get("Cf-Access-Jwt-Assertion");
    if (!jwt) return reply(403, { error: "missing Cloudflare Access token" });
    if (!env.ACCESS_TEAM_DOMAIN || !env.ACCESS_AUD) return reply(500, { error: "Access check not configured" });
    if (!(await verifyAccess(jwt, env.ACCESS_TEAM_DOMAIN, env.ACCESS_AUD))) return reply(403, { error: "bad Access token" });
    const text = await req.text();
    if (text.length > 4096) return reply(413, { error: "trade too large" });
    let trade: unknown;
    try {
      trade = JSON.parse(text);
    } catch {
      return reply(400, { error: "body must be JSON" });
    }
    const err = validate(trade);
    if (err) return reply(400, { error: err });
    const key = `inbox/${new Date().toISOString().replace(/[-:.]/g, "")}-${crypto.randomUUID()}.json`;
    await env.INBOX.put(key, JSON.stringify(trade), { httpMetadata: { contentType: "application/json" } });
    return reply(201, { file: key });
  },
};
