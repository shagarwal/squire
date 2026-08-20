# Marketing site + waitlist (implementation-plan: `apps/web`, Railway service `web`).
#
# Caddy does two jobs (see Caddyfile):
#   1. serves the static site in /srv,
#   2. reverse-proxies /api/* -> control-api over Railway's PRIVATE network,
#      so the browser only ever talks same-origin (no CORS) and the waitlist
#      route needs no second public domain.
FROM caddy:2-alpine

COPY Caddyfile /etc/caddy/Caddyfile
COPY site/ /srv/

# Railway injects $PORT; the default matches the control-api/ingress convention.
ENV PORT=8080
EXPOSE 8080
