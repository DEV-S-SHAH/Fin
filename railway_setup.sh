#!/bin/bash
# Railway Setup Script - Useful for running manual ingestion in Railway
#
# Prerequisites in Railway:
# 1. Persistent Volume attached to service fin-graph:
#    - Mount Path: /data
#    - Size: 500 MB (or larger)
# 2. Environment Variables configured in Railway:
#    FINGRAPH_DATA_DIR=/data
#    FINGRAPH_DEV_LOGIN=0
#    FINGRAPH_CORS_ORIGIN=https://fin-graph-production.up.railway.app
#    FINGRAPH_AUTH_SECRET=<generate with: openssl rand -hex 32>
#    NVIDIA_API_KEY=<your-key>

set -e

echo "========================================="
echo "FinGraph Railway Setup & Ingestion"
echo "========================================="

if [ ! -d "/data" ]; then
    echo "ERROR: /data directory not found. Ensure persistent volume is mounted at /data."
    exit 1
fi

export FINGRAPH_DATA_DIR=/data

echo "Step 1: Building LadybugDB from committed SEC filings..."
python3 -m sandbox_engine --reset

echo ""
echo "Database created at: /data/sandbox.lbug"
ls -lh /data/sandbox.lbug

echo ""
echo "Setup complete!"
