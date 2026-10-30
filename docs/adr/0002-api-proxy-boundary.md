# ADR-0002: The browser talks only to our API

- Status: accepted
- Date: 2026-10-22

## Context
Frigate's unauthenticated port (5000) grants admin rights to anyone who can reach it. Its authenticated port uses Frigate's own user store, which is separate from our operator accounts. Camera stream URLs contain credentials.

## Decision
Only the `web` container (Caddy) is published on the LAN. It serves the UI and proxies `/api/*` to our API, which talks to Frigate on the internal Docker network:
- Live video goes through an authenticated websocket relay in our API.
- Snapshots and clips are fetched by the API and served from its own media store, behind sign-in.
- API responses never contain stream URLs.

## Consequences
- One login, one audit point, and one place to enforce roles later.
- The relay adds a small hop of latency to live video. It is acceptable for a LAN, and gets measured during verification.
- Administrators use Frigate's own UI only through `127.0.0.1:8971`, for example over an SSH tunnel.
