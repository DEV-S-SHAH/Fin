#!/bin/bash
# FinGraph UI Server - Persistent runner
cd /Users/dev/Downloads/Fin
# Load .env file for persistent configuration (auth secret, CORS, etc.)
set -a
source .env 2>/dev/null || true
set +a
# Local development only: enables the "Continue locally" sign-in button on the
# auth page. Without this, POST /api/auth/session rejects provider "dev" with
# HTTP 400. Never set this in a real deployment -- it disables the OAuth
# token verification path.
export FINGRAPH_DEV_LOGIN=1
# Run on multiple ports (9100, 9101) so you can switch ports without losing sessions
exec python -m ui.fingraph --port 9100,9101 --host 127.0.0.1 --no-browser
