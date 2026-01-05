## FastAPI WhatsApp Bot (PNP Lite)

This repo now contains a minimal FastAPI skeleton wired for Twilio WhatsApp + MongoDB + GPT-4 expansion. Fill the `.env` (copy from `.env.example`) with the credentials below.

### Required env (pilot values provided)
- `MONGO_URI` `mongodb+srv://pnpliteuser:pnplite2025@pnplite.e2lfreq.mongodb.net/`
- `TWILIO_ACCOUNT_SID` `AC17e5cddb33ed805bdb5ee6ad56e40d21`
- `TWILIO_AUTH_TOKEN` `0fe1709044807ba7392eab8f66f6012d`
- `OPENAI_API_KEY` `sk-...`
- `ADMIN_NUMBERS` `+2348083265499`
- `PRICE_SHEET_URL` optional (link to current weekly price sheet)

### Run locally
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # then edit OPENAI_API_KEY, etc.
uvicorn app.main:app --reload --port 8000
```

- Twilio inbound webhook URL: `https://<host>/whatsapp/webhook`
- Health check: `GET /healthz`

### What is implemented now
- Mongo connection via `MONGO_URI` (Atlas ready) and basic collections (`members`, `orders`, `messages`, `config` for price sheet).
- Twilio signature verification on webhook.
- Onboarding state machine (name → city → membership → payment proof prompt) + referral code helper.
- User intents: `PRICE`, `ORDER`, `HELP`, `REFERRAL`; order capture stores free-text orders to Mongo.
- Admin commands (whitelisted numbers): `/set_price_sheet <url>`, `/orders`, `/members`, `/mark_paid <phone?>`, `/broadcast <city|all> <message>` (sends WhatsApp messages via Twilio), `/broadcast` uses `TWILIO_FROM_NUMBER`.
- GPT-4 FAQ assist hook (uses `OPENAI_API_KEY`, model `gpt-4o-mini`) with a constrained system prompt; falls back to menu if unavailable.
- Admin HTTP endpoints (require `phone` query as whitelisted admin): `GET /admin/messages?phone=...&limit=`, `GET /admin/members`, `GET /admin/orders/summary`.
- Admin UI (HTTP Basic, username must be whitelisted admin phone; password from `ADMIN_DASH_PASSWORD` if set): `/ui/admin/messages`, `/ui/admin/members`, `/ui/admin/orders`, `/ui/admin/broadcasts`, `/ui/admin/status`.

### Next to implement (recommended)
1) Delivery status queries and Twilio template IDs for broadcasts (currently plain message body).
2) Build admin UI (FastAPI templates or SPA) for viewing `messages`, `members`, `orders`, triggering broadcasts, and price sheet uploads.
3) Add price-linked totals, payment proof media handling, and rate limiting; webhook proxy headers for signature validation if behind a load balancer.
4) Secure admin UI behind VPN or auth proxy in production.

### Reference IDs (provided)
- WhatsApp Business Account ID: `878779904725428`
- Meta Business Manager ID: `1873336879738647`
- Active sender: `+2348083265499` (Osusu by PackNPay)
