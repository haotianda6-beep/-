# Arbitrage Control Platform

Arbitrage execution and monitoring platform for single-exchange cash-carry,
MT4-to-exchange spread monitoring, and the independent XAU executor.

The implementation is intentionally guarded by global safety switches:

- dashboard API and WebSocket
- opportunity ranking with deterministic filters for active strategies
- verified-trade history model
- editable risk and execution settings
- encrypted API credential vault with frontend API management
- exchange adapter contracts for real integrations
- database schema draft

Live order placement remains controlled by `TRADING_ENABLED`,
`ORDER_EXECUTION_ENABLED`, `API_READ_ONLY_MODE`, and the per-strategy switches.

## Run Locally

Create local environment placeholders first:

```bash
cd /root/perp-arb-bot
cp .env.example .env
```

Keep these defaults until all read-only data and reconciliation checks are
verified:

```env
LIVE_DATA_ENABLED=false
TRADING_ENABLED=false
ORDER_EXECUTION_ENABLED=false
API_READ_ONLY_MODE=true
```

Exchange API keys can be entered in the frontend `API 管理` page. They are sent
to the backend and stored encrypted in `config/credentials.enc`. The browser
only shows masked status and never receives saved plaintext secrets. `.env`
exchange keys are still supported as a compatibility fallback.

Backend:

```bash
cd /root/perp-arb-bot/backend
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

If this machine has no `python3.10-venv`, use the same fallback used during
initial validation:

```bash
cd /root/perp-arb-bot/backend
python3 -m pip install --user -r requirements.txt
python3 -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Frontend:

```bash
cd /root/perp-arb-bot/frontend
npm install
npm run dev -- --host 0.0.0.0 --port 5173
```

Open `http://localhost:5173`.

## Safety Defaults

- `cash_carry_auto_open_enabled` is off.
- `cash_carry_auto_close_enabled` is off.
- `manual_confirm_required` is on.
- `max_leverage` defaults to `3`.
- exchange and AI secrets should be entered through `API 管理` or supplied via
  environment variables.
- encrypted credential files are ignored by Git.
- `.env` is ignored and must not be committed.

## Files Never To Commit

- `.env`
- `config/credentials.enc`
- `config/credentials.key`
- `config/settings.json`
- log files

## Git Deployment

Initialize and push after creating a private remote repository:

```bash
cd /root/perp-arb-bot
git init
git add .
git commit -m "feat: 初始化套利系统"
git branch -M main
git remote add origin <your-private-repo-url>
git push -u origin main
```

On another server:

```bash
git clone <your-private-repo-url> /root/perp-arb-bot
cd /root/perp-arb-bot
cp .env.example .env
python3 -m pip install --user -r backend/requirements.txt
npm --prefix frontend install
npm --prefix frontend run build
```

Then configure domain, HTTPS, service manager, and open the frontend `API 管理`
page to enter exchange API keys. Do not copy plaintext API keys through Git.

## Validation

```bash
cd /root/perp-arb-bot/backend
. .venv/bin/activate
pytest

cd /root/perp-arb-bot/frontend
npm run build
```
