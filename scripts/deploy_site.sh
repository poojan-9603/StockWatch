#!/usr/bin/env bash
# Deploy the site and its data (out/site/data) to Cloudflare Pages, behind Access (docs/M6.md). Used by the nightly
# and site-deploy workflows. Env: CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID, ACCESS_TEAM_DOMAIN, ACCESS_AUD, BUCKET,
# PAGES_PROJECT. Without the first two it warns and stops; without the Access values it only creates the project.
set -u
for v in CLOUDFLARE_API_TOKEN CLOUDFLARE_ACCOUNT_ID; do
  [ -n "${!v}" ] || { echo "::warning::$v isn't set, so the site wasn't deployed (docs/M6.md)"; exit 0; }
done
set -e
(cd worker && npm ci --no-audit --no-fund)
# create the project first and show its address; nothing is published until Access protects that address
(cd site && ../worker/node_modules/.bin/wrangler pages project create "$PAGES_PROJECT" --production-branch main \
  || echo "project $PAGES_PROJECT exists")
(cd site && ../worker/node_modules/.bin/wrangler pages project list) | grep -i "$PAGES_PROJECT" || true
for v in ACCESS_TEAM_DOMAIN ACCESS_AUD; do
  [ -n "${!v}" ] || { echo "::warning::$v isn't set: protect the address above with Access first (docs/M6.md)"; exit 0; }
done
(cd site && npm ci --no-audit --no-fund && npm run build)
cp -r out/site/data site/dist/data
# the Pages project's settings live here, built from secrets: the trade function's R2 binding and Access
printf '%s\n' "name = \"$PAGES_PROJECT\"" 'pages_build_output_dir = "./dist"' 'compatibility_date = "2026-10-01"' \
  '[vars]' "ACCESS_TEAM_DOMAIN = \"$ACCESS_TEAM_DOMAIN\"" "ACCESS_AUD = \"$ACCESS_AUD\"" \
  '[[r2_buckets]]' 'binding = "INBOX"' "bucket_name = \"$BUCKET\"" > site/wrangler.toml
cd site && ../worker/node_modules/.bin/wrangler pages deploy dist --project-name "$PAGES_PROJECT" --branch main
