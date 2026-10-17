# Deploy

Pushes to `main` build a `linux/amd64` image and publish it to GHCR
(`.github/workflows/publish.yml`):

- `ghcr.io/hthnlndn/food-service:latest`
- `ghcr.io/hthnlndn/food-service:<full commit sha>` — pin this to roll back

## Pull access on the server

Needs a GitHub PAT (classic) with the `read:packages` scope:

```bash
echo "$GHCR_PAT" | docker login ghcr.io -u <github-username> --password-stdin
```

## Services

Two containers:

- **`food-service`** is the app. Before uvicorn starts, it runs `alembic upgrade head`.
- **`food-service-db`** is PostgreSQL 18. It's reachable only on the internal network
  `food-service-internal`, never on the proxy network.

## USDA nutrition store (required before first start)

`usda.db` is the read-only nutrition store. **It isn't in the image**: it's built locally
from the USDA CSV zips (see README). Copy your local `data/usda.db` to the server. Without
it, planning fails at the first ingredient lookup, while the rest of the app works. The app
container runs as uid/gid `10001` and only reads the file:

```bash
mkdir -p /srv/food-service/usda
cp data/usda.db /srv/food-service/usda/          # from your dev machine (scp)
sudo chown -R 10001:10001 /srv/food-service/usda
```

App state (plans, ratings, caches, telemetry) lives in Postgres, in the named volume
`food-service-pgdata`.

## docker-compose

```yaml
services:
  food-service:
    image: ghcr.io/hthnlndn/food-service:latest   # or :<commit sha> to pin
    user: "10001:10001"
    restart: unless-stopped
    mem_limit: 512m
    cpus: 1.0
    env_file: ./food-service.env
    volumes:
      - /srv/food-service/usda:/app/data:ro
    networks:
      - mealplanner             # reverse proxy reaches it at http://food-service:8000
      - food-service-internal   # talks to the DB
    depends_on:
      food-service-db:
        condition: service_healthy
    # no ports:

  food-service-db:
    image: postgres:18
    container_name: food-service-db   # fixed name, so the backup cron below can `docker exec` it
    restart: unless-stopped
    mem_limit: 512m
    cpus: 1.0
    env_file: ./food-service-db.env
    volumes:
      - food-service-pgdata:/var/lib/postgresql   # Postgres 18 images mount here, not .../data
    networks:
      - food-service-internal
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U food -d food_service"]
      interval: 5s
      timeout: 3s
      retries: 10
    # no ports:

volumes:
  food-service-pgdata:

networks:
  mealplanner:
    external: true
  food-service-internal:
    internal: true
```

## Runtime environment

Both env files live on the server with `chmod 600`. You supply all the secrets; the image has
no defaults for them.

`food-service-db.env`:

| Variable | Required | Notes |
|---|---|---|
| `POSTGRES_USER` | yes | `food` (matches the healthcheck and `DATABASE_URL`) |
| `POSTGRES_PASSWORD` | yes | Secret. Pick a long random one (`openssl rand -hex 24`) |
| `POSTGRES_DB` | yes | `food_service` |

`food-service.env`:

| Variable | Required | Notes |
|---|---|---|
| `DATABASE_URL` | yes | Secret. `postgresql://food:<POSTGRES_PASSWORD>@food-service-db:5432/food_service`. URL-encode the password if it has special characters (hex doesn't). |
| `SHELFATLAS_API_KEY` | yes | Secret. `sa_live_…` key from app.shelfatlas.com |
| `LLM_BASE_URL` | yes | OpenAI-compatible endpoint (presets in `.env.example`) |
| `LLM_MODEL` | yes | Model name for that endpoint |
| `LLM_API_KEY` | yes | Secret. Key for that provider |
| `TRANSLATE_BASE_URL` | no | Separate endpoint for display translation; unset = reuse `LLM_*` |
| `TRANSLATE_MODEL` | no | As above |
| `TRANSLATE_API_KEY` | no | Secret. As above |

`USDA_DB_PATH` is already set to `/app/data/usda.db` in the image. Only override it if you
also change the volume mount.

## Backups

Run a nightly `pg_dump` from the host's crontab, which keeps two weeks of dumps. Write them
outside any git checkout:

```cron
30 3 * * * docker exec food-service-db pg_dump -U food -Fc food_service > /srv/backups/food-service/$(date +\%F).dump && find /srv/backups/food-service -name '*.dump' -mtime +14 -delete
```

Restore into an empty database:

```bash
docker exec -i food-service-db pg_restore -U food -d food_service --clean --if-exists < dump
```

## Upgrades and rollbacks

- **App:** pull the new tag and `docker compose up -d`. Migrations run on start, and the whole
  upgrade runs in one transaction. If anything fails, the schema is left exactly as it was
  and the container exits (the logs show why). Redeploy the previous `:<sha>` tag.
- **Migration rule:** every migration must keep the *previous* image working. Add tables and
  columns freely. Rename or drop in a later release (expand, then contract). This is what
  makes rolling back the app to the previous `:<sha>` safe without `alembic downgrade`.
- **Postgres major version** (e.g. 18 → 19): `pg_dump` from the old container, start the new
  version on a fresh volume, then `pg_restore`.

## Notes

- Listens on port `8000`. There's no health endpoint, so the image has no `HEALTHCHECK`.
- Runs a single uvicorn worker on purpose: the background planner runs in-process, and only
  one planning job runs at a time. Don't scale it to multiple replicas or workers.
- If the database goes away, pages return 503 "Database unavailable" and recover on their own
  once it's back.
- The app has no authentication. Put access control (basic auth, SSO, IP allowlist) on the
  reverse proxy.
