// POST /api/trade on the Pages site: the trade inbox Worker (worker/src/index.ts, tested there) run as a Pages
// Function, so the site, its data files and the trade form share one address behind one Cloudflare Access app.
// The Pages project needs the R2 binding INBOX and the variables ACCESS_TEAM_DOMAIN and ACCESS_AUD (docs/M6.md).
import worker from "../../../worker/src/index";

export const onRequest = (ctx: { request: Request; env: Parameters<typeof worker.fetch>[1] }) =>
  worker.fetch(ctx.request, ctx.env);
