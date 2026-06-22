#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FRONTEND_DIR="$PROJECT_ROOT/frontend"
XAU_WEB_DIR="$PROJECT_ROOT/arb-bot/web"
WEB_ROOT="/var/www/redzhong.top"
ALPHA_WEB_ROOT="/var/www/alpha.redzhong.top"

cd "$FRONTEND_DIR"
npm run build

rsync -r --delete \
  --chmod=Du=rwx,Dgo=rx,Fu=rw,Fgo=r \
  "$FRONTEND_DIR/dist/" "$WEB_ROOT/"

install -d -m 755 "$WEB_ROOT/xau-arb"
install -m 644 "$XAU_WEB_DIR/index.html" "$WEB_ROOT/xau-arb/index.html"

install -d -m 755 "$ALPHA_WEB_ROOT"
install -m 644 "$FRONTEND_DIR/dist/alpha-alert/index.html" "$ALPHA_WEB_ROOT/index.html"

find "$WEB_ROOT" -type d -exec chmod 755 {} +
find "$WEB_ROOT" -type f -exec chmod 644 {} +
find "$ALPHA_WEB_ROOT" -type d -exec chmod 755 {} +
find "$ALPHA_WEB_ROOT" -type f -exec chmod 644 {} +
