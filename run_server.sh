#!/bin/bash
# FinGraph UI Server - Persistent runner
cd /Users/dev/Downloads/Fin
# Local development only: enables the "Continue locally" sign-in button on the
# auth page. Without this, POST /api/auth/session rejects provider "dev" with
# HTTP 400. Never set this in a real deployment -- it disables the OAuth
# token verification path.
export FINGRAPH_DEV_LOGIN=1
exec python -m ui.fingraph --port 9100 --host 127.0.0.1 --no-browser
