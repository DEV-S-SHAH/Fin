#!/bin/bash
# Railway Startup Script
# Handles persistent volume verification, database initialization from pre-built DB,
# and starts the FinGraph server.

set -e

# Trap signals for graceful exit
trap 'echo "FinGraph startup script exiting..."; exit 0' SIGTERM SIGINT

echo "========================================="
echo "FinGraph Railway Startup"
echo "========================================="

# Verify persistent volume is mounted
if [ ! -d "/data" ]; then
    echo "ERROR: /data directory not found. Ensure persistent volume is mounted at /data in Railway."
    exit 1
fi

echo "Persistent volume mounted at /data"
echo "Available space: $(df -h /data | awk 'NR==2 {print $4}')"

# Set data directory for persistent storage
export FINGRAPH_DATA_DIR=/data

# Set LadybugDB memory and concurrency limits to keep container well within Railway RAM limits
export LADYBUG_BUFFER_POOL_BYTES="${LADYBUG_BUFFER_POOL_BYTES:-134217728}"
export LADYBUG_MAX_THREADS="${LADYBUG_MAX_THREADS:-4}"

DB_PATH="/data/sandbox.lbug"

# Check whether the database exists and has at least one company
check_db_populated() {
    python3 -c "
import sys
from sandbox_engine.query_ui import KnowledgeGraph
from ui.fingraph.server import companies
try:
    kg = KnowledgeGraph('$DB_PATH', read_only=True)
    c = companies(kg)
    sys.exit(0 if len(c) > 0 else 1)
except Exception:
    sys.exit(1)
" 2>/dev/null
}

if [ ! -f "$DB_PATH" ] || ! check_db_populated; then
    echo "Database missing or empty at $DB_PATH. Restoring pre-built LadybugDB..."
    
    # 1. Check for uncompressed pre-built database
    if [ -f "data/sandbox.lbug" ]; then
        echo "Found pre-built data/sandbox.lbug ($(ls -lh data/sandbox.lbug | awk '{print $5}')). Copying to persistent volume..."
        cp -f data/sandbox.lbug "$DB_PATH"
    # 2. Check for compressed pre-built database
    elif [ -f "data/sandbox.lbug.gz" ]; then
        echo "Found compressed pre-built data/sandbox.lbug.gz ($(ls -lh data/sandbox.lbug.gz | awk '{print $5}')). Decompressing to persistent volume..."
        gunzip -c data/sandbox.lbug.gz > "$DB_PATH"
    # 3. Fallback: build from filings if files exist
    elif [ -d "sandbox_engine/data" ]; then
        echo "No pre-built DB found. Building LadybugDB from SEC filings..."
        python3 -m sandbox_engine --reset || echo "Warning: sandbox_engine --reset had non-zero exit code"
    fi

    # Sync concepts registry if present
    if [ -f "data/concepts.json" ]; then
        echo "Syncing concept registry to persistent volume..."
        cp -f data/concepts.json /data/concepts.json
    fi

    echo "Database restoration complete!"
    ls -lh "$DB_PATH" 2>/dev/null || true
else
    echo "Database found at $DB_PATH with populated issuers."
    ls -lh "$DB_PATH"
fi

echo ""
echo "========================================="
echo "Starting FinGraph Server"
echo "========================================="

PORT="${PORT:-8080}"
echo "Railway assigned PORT: $PORT"

# Listen on both $PORT and 9100 so Railway's proxy routes successfully regardless of targetPort configuration
if [ "$PORT" != "9100" ]; then
    LISTEN_PORTS="$PORT,9100"
else
    LISTEN_PORTS="$PORT"
fi

echo "Binding server to: $LISTEN_PORTS on host 0.0.0.0..."
exec python3 -m ui.fingraph --port "$LISTEN_PORTS" --host 0.0.0.0 --no-browser