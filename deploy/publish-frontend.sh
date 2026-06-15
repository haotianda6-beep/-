#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FRONTEND_DIR="$PROJECT_ROOT/frontend"
XAU_WEB_DIR="$PROJECT_ROOT/arb-bot/web"
WEB_ROOT="/var/www/redzhong.top"

cd "$FRONTEND_DIR"
npm run build

rsync -r --delete \
  --chmod=Du=rwx,Dgo=rx,Fu=rw,Fgo=r \
  "$FRONTEND_DIR/dist/" "$WEB_ROOT/"

install -d -m 755 "$WEB_ROOT/xau-arb"
install -m 644 "$XAU_WEB_DIR/index.html" "$WEB_ROOT/xau-arb/index.html"

find "$WEB_ROOT" -type d -exec chmod 755 {} +
find "$WEB_ROOT" -type f -exec chmod 644 {} +
