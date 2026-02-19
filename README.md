# PhoneRepairOffers

Web interface to find the best phone-repair parts offers from Leboncoin and eBay.

## Features

- Server-side search scraping (Leboncoin + eBay)
- Ranking by total cost (`price + shipping`)
- Favorites tab with persistent SQLite storage
- Search cache (15 minutes by default)
- FastAPI backend + lightweight web UI
- Docker-ready for VPS deployment

## API

- `GET /health`
- `POST /api/search`
- `GET /api/favorites`
- `POST /api/favorites`
- `DELETE /api/favorites/{favoriteId}`
- `POST /api/favorites/toggle`

## Local run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8091
```

Open `http://localhost:8091`.

## Docker local

```bash
docker compose -f docker-compose.offers.yml up -d --build
```

## VPS integration (existing /opt/grocy stack)

- Service path: `/opt/grocy/services/phone-repair-offers`
- Data path: `/opt/grocy/services/phone-repair-offers/data`
- Domain: `offers.actually-caring-about-billionaires.online`

### Required DNS

Create this A record in Hostinger:

- Type: `A`
- Name: `offers`
- Value: `207.180.235.68`
- TTL: `300`

## CI/CD

- `ci.yml`: lint + format check + tests + docker build
- `deploy-vps.yml`: auto deploy on push to `main`

Required repo secrets:

- `VPS_HOST`
- `VPS_USER`
- `VPS_SSH_PRIVATE_KEY`
- `VPS_PORT`

## Notes

- This project scrapes third-party pages. HTML can change; update parser tests when needed.
- Keep request rate low and rely on cache to reduce load.
