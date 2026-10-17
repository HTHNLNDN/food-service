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

## Data volume (required before first start)

The app keeps its SQLite files in `/app/data` inside the container:

- `food_service.db` — created automatically on first start.
- `usda.db` — the nutrition store. **Not in the image** (it's built locally from the USDA CSV
  zips, see README). Copy your local `data/usda.db` into the volume. Without it, planning fails.

The container runs as uid/gid `10001`, so the host directory has to be writable by that user:

```bash
mkdir -p /srv/food-service/data
cp data/usda.db /srv/food-service/data/        # from your dev machine
sudo chown -R 10001:10001 /srv/food-service/data
```

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
      - /srv/food-service/data:/app/data
    networks:
      - mealplanner
    # no ports: — reached by the reverse proxy at http://food-service:8000

networks:
  mealplanner:
    external: true
```

## Runtime environment

Put these in `food-service.env` on the server (`chmod 600`). You supply all of them.
The image has no defaults for any of them.

| Variable | Required | Notes |
|---|---|---|
| `SHELFATLAS_API_KEY` | yes | Secret. `sa_live_…` key from app.shelfatlas.com |
| `LLM_BASE_URL` | yes | OpenAI-compatible endpoint (presets in `.env.example`) |
| `LLM_MODEL` | yes | Model name for that endpoint |
| `LLM_API_KEY` | yes | Secret. Key for that provider |
| `TRANSLATE_BASE_URL` | no | Separate endpoint for display translation; unset = reuse `LLM_*` |
| `TRANSLATE_MODEL` | no | As above |
| `TRANSLATE_API_KEY` | no | Secret. As above |

`FOOD_SERVICE_DATA_DIR` is already set to `/app/data` in the image. Don't override it unless
you also change the volume mount.

There's no database server and there are no DB credentials. Storage is the SQLite files in the volume.

## Notes

- Listens on port `8000`. There's no health endpoint, so the image has no `HEALTHCHECK`.
- Runs a single uvicorn worker on purpose (SQLite + in-process background planner). Don't scale
  it to multiple replicas or workers.
- The app has no authentication. Put access control (basic auth, SSO, IP allowlist) on the reverse proxy.
