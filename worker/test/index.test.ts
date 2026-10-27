import assert from "node:assert/strict";
import { test } from "node:test";
import worker, { validate, verifyAccess } from "../src/index.ts";

const buy = { kind: "buy", date: "2026-10-20", symbol: "AAPL", shares: 10, price: 250 };

test("validator accepts what ingest_inbox accepts", () => {
  assert.equal(validate(buy), null);
  assert.equal(validate({ ...buy, kind: "sell", lot_id: "7", fees: 1, note: "trim", cik: 320193 }), null);
  assert.equal(validate({ kind: "deposit", date: "2026-10-01", amount: 1000 }), null);
  assert.equal(validate({ kind: "withdrawal", date: "2026-10-01", amount: 50, fees: 0 }), null);
});

test("validator rejects what ingest_inbox rejects, and bad types", () => {
  const bad: unknown[] = [
    null, [], "buy", { ...buy, hack: 1 }, { ...buy, kind: "short" }, { ...buy, date: undefined }, { ...buy, date: "" },
    { ...buy, symbol: "" }, { ...buy, shares: 0 }, { ...buy, shares: -1 }, { ...buy, price: 0 },
    { kind: "sell", date: "2026-10-20", symbol: "AAPL", shares: 1 },
    { ...buy, shares: "10" }, { ...buy, price: null }, { ...buy, fees: "1" }, { ...buy, fees: -1 }, { ...buy, cik: 1.5 },
    { ...buy, symbol: 5 }, { ...buy, date: "10/20/2026" }, { ...buy, date: "2026-02-30" },
    { kind: "deposit", date: "2026-10-01" }, { kind: "deposit", date: "2026-10-01", amount: -5 },
  ];
  for (const t of bad) assert.notEqual(validate(t), null, JSON.stringify(t));
});

async function accessSetup(kid: string) {  // a fresh key id per test: the Worker caches keys by id
  const pair = await crypto.subtle.generateKey(
    { name: "RSASSA-PKCS1-v1_5", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" },
    true, ["sign", "verify"]);
  const jwk = { ...(await crypto.subtle.exportKey("jwk", pair.publicKey)), kid };
  globalThis.fetch = (async () => new Response(JSON.stringify({ keys: [jwk] }))) as typeof fetch;
  const enc = (o: object) => Buffer.from(JSON.stringify(o)).toString("base64url");
  return async (claims: object, useKid = kid) => {
    const body = `${enc({ alg: "RS256", kid: useKid })}.${enc(claims)}`;
    const sig = await crypto.subtle.sign("RSASSA-PKCS1-v1_5", pair.privateKey, new TextEncoder().encode(body));
    return `${body}.${Buffer.from(sig).toString("base64url")}`;
  };
}

test("Access token: signature, audience, issuer and expiry all checked", async () => {
  const sign = await accessSetup("k1");
  const now = 1_800_000_000, team = "t.cloudflareaccess.com", good = { aud: ["A"], iss: `https://${team}`, exp: now + 60 };
  assert.equal(await verifyAccess(await sign(good), team, "A", now), true);
  assert.equal(await verifyAccess(await sign({ ...good, aud: ["B"] }), team, "A", now), false);
  assert.equal(await verifyAccess(await sign({ ...good, iss: "https://evil" }), team, "A", now), false);
  assert.equal(await verifyAccess(await sign({ ...good, exp: now - 1 }), team, "A", now), false);
  assert.equal(await verifyAccess(await sign(good, "other"), team, "A", now), false);
  const [h, , s] = (await sign(good)).split(".");
  const forged = Buffer.from(JSON.stringify({ ...good, aud: ["A", "x"] })).toString("base64url");
  assert.equal(await verifyAccess(`${h}.${forged}.${s}`, team, "A", now), false);
  assert.equal(await verifyAccess("not-a-jwt", team, "A", now), false);
});

test("handler writes only valid, authenticated trades, only under inbox/", async () => {
  const sign = await accessSetup("k2");
  const team = "t.cloudflareaccess.com";
  const puts: [string, string][] = [];
  const env = { INBOX: { put: async (k: string, v: string) => puts.push([k, v]) }, ACCESS_TEAM_DOMAIN: team, ACCESS_AUD: "A" };
  const token = await sign({ aud: "A", iss: `https://${team}`, exp: Date.now() / 1000 + 60 });
  const call = (body: unknown, headers: Record<string, string> = { "Cf-Access-Jwt-Assertion": token }, path = "/api/trade") =>
    worker.fetch(new Request(`https://x${path}`, { method: "POST", headers, body: JSON.stringify(body) }), env);

  assert.equal((await call(buy, {})).status, 403);
  assert.equal((await call(buy, { "Cf-Access-Jwt-Assertion": "junk" })).status, 403);
  assert.equal((await call(buy, undefined, "/api/other")).status, 404);
  assert.equal((await call({ ...buy, shares: 0 })).status, 400);
  assert.equal((await worker.fetch(new Request("https://x/api/trade", { headers: { "Cf-Access-Jwt-Assertion": token } }), env)).status, 405);
  assert.equal((await worker.fetch(new Request("https://x/api/trade", { method: "POST", body: "{", headers: { "Cf-Access-Jwt-Assertion": token } }), env)).status, 400);
  assert.equal(puts.length, 0);

  const r = await call(buy);
  assert.equal(r.status, 201);
  assert.equal(puts.length, 1);
  assert.match(puts[0][0], /^inbox\/\d{8}T\d{9}Z-[0-9a-f-]{36}\.json$/);
  assert.deepEqual(JSON.parse(puts[0][1]), buy);
  assert.deepEqual(await r.json(), { file: puts[0][0] });

  const unset = { INBOX: env.INBOX };
  assert.equal((await worker.fetch(new Request("https://x/api/trade", { method: "POST", body: "{}", headers: { "Cf-Access-Jwt-Assertion": token } }), unset)).status, 500);
});
