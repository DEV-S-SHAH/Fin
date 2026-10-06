#!/bin/bash
# Render Setup Script - Run ONCE in Render Shell after adding Persistent Disk
# 
# Prerequisites:
# 1. Add Persistent Disk in Render Dashboard: Settings → Disks → Add Disk
#    - Name: fingraph-data
#    - Mount Path: /data
#    - Size: 1 GB
# 2. Set Environment Variables in Render Dashboard: Settings → Environment
#    FINGRAPH_DATA_DIR=/data
#    FINGRAPH_DEV_LOGIN=0
#    FINGRAPH_CORS_ORIGIN=https://your-app-name.onrender.com
#    FINGRAPH_AUTH_SECRET=<generate with: openssl rand -hex 32>

set -e

echo "========================================="
echo "FinGraph Render Database Setup"
echo "========================================="

# Verify persistent disk is mounted
if [ ! -d "/data" ]; then
    echo "ERROR: /data directory not found. Add Persistent Disk in Render Dashboard first."
    exit 1
fi

echo "✓ Persistent disk mounted at /data"
echo "  Available space: $(df -h /data | awk 'NR==2 {print $4}')"

# Set FINGRAPH_DATA_DIR for this session
export FINGRAPH_DATA_DIR=/data

echo ""
echo "Step 1: Ingesting SEC filings for AAPL (2020-2026)..."
echo "This takes 5-10 minutes..."
python -m ingestion.cli --ticker AAPL --start-year 2020 --end-year 2026

echo ""
echo "Step 2: Building LadybugDB from staged data..."
echo "This takes 5-10 minutes..."
python -m sandbox_engine --reset

echo ""
echo "========================================="
echo "✓ Setup Complete!"
echo "========================================="
echo ""
echo "Database created at: /data/sandbox.lbug"
ls -lh /data/sandbox.lbug
echo ""
echo "Next steps:"
echo "1. Go to Render Dashboard → Manual Deploy → Deploy latest commit"
echo "2. Your app will now start successfully"
echo ""
echo "To add more companies later, run in Render Shell:"
echo "  export FINGRAPH_DATA_DIR=/data"
echo "  python -m ingestion.cli --ticker MSFT --start-year 2020 --end-year 2026"
echo "  python -m sandbox_engine --reset"