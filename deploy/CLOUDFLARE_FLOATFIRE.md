# Cloudflare settings for floatfire.com

Do this **after** nginx + Let’s Encrypt on the origin are working (or in parallel, but DNS must point to the server).

## DNS

- **A** record: `floatfire.com` → your server **IPv4** (same IP nginx listens on).
- **AAAA** (optional): if you use IPv6, add it; otherwise omit.
- **CNAME** or **A** for `www`: point `www` to `@` or the same IP (needed if you requested a cert for `www.floatfire.com`).

## Proxy (orange cloud)

- Turn **Proxied** (orange cloud) **on** for `floatfire.com` and `www` once HTTPS works end-to-end.  
  You can leave **DNS only** (gray) while testing Certbot, then enable proxy.

## SSL/TLS encryption mode

**SSL/TLS** → **Overview** → set encryption mode:

- **Full (strict)** — **recommended** once Let’s Encrypt is installed on the server and nginx serves a valid cert for `floatfire.com`.  
  Origin must have a cert that matches the hostname (Let’s Encrypt does).

- **Full** — only if you *cannot* use strict yet; still use a real cert on origin.

- **Flexible** — **avoid** for this app (browser → Cloudflare HTTPS, Cloudflare → origin HTTP). It hides cert problems but is weaker and can confuse apps that expect `X-Forwarded-Proto`.

## Edge Certificates

- Leave **Universal SSL** (and **Automatic HTTPS Rewrites**) enabled for the zone.

## Optional

- **Always Use HTTPS**: ON (redirect HTTP → HTTPS at the edge).
- **Minimum TLS**: 1.2 is fine.

## If Certbot fails while Cloudflare is proxied

- Ensure **port 80** on the server is reachable from the internet (Cloudflare forwards HTTP for HTTP-01 challenges).  
- If something still blocks, temporarily set the record to **DNS only** (gray), run Certbot, then turn proxy back **on**.
