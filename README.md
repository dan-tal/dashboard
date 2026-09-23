# dashboard

A single-file Flask dashboard for a homelab Docker host: live CPU/RAM/disk/temperature/network
stats with history, per-container CPU/memory usage, disk-usage forecasting, threshold alerts,
and auto-discovered links to your other containers (via Traefik `Host()` labels or published
ports).

## Requirements

- Docker + Docker Compose
- A `local-net` Docker network (see `compose.yml`) — create it with:
  ```
  docker network create local-net
  ```
- (Optional) Traefik on the same network if you want the `dashboard.home` routing label to work.

## Setup

1. Copy `.env.example` to `.env` and fill in your values:
   ```
   cp .env.example .env
   ```
   - `HOST_IP` — your Docker host's LAN IP. Used as a fallback link for containers that publish
     a port but have no Traefik `Host()` label.
   - `LAN_SUBNET` — the subnet allowed through the Traefik `ipallowlist` middleware that gates
     access to the dashboard.

2. (Optional) Copy `overrides.example.json` to `data/overrides.json` to manually set or hide the
   link shown for specific containers (useful for host-network containers with no Traefik label):
   ```
   cp overrides.example.json data/overrides.json
   ```

3. Start it:
   ```
   docker compose up -d --build
   ```

The dashboard listens on port 5000 inside the container; expose it however you like (the included
Traefik labels route `dashboard.home`, restricted to `LAN_SUBNET`).

## Data

Runtime state (metrics history as SQLite, and `overrides.json`) lives in `./data`, bind-mounted
into the container — nothing is stored outside this project directory.

## License

MIT — see [LICENSE](LICENSE).
