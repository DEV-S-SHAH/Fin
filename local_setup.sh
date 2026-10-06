#!/bin/bash
# Local Development Setup - Run once to set up local environment

set -e

echo "========================================="
echo "FinGraph Local Development Setup"
echo "========================================="

# Check if .env exists
if [ ! -f ".env" ]; then
    echo "Creating .env from .env.example..."
    cp .env.example .env
    echo "✓ Created .env"
    echo ""
    echo "IMPORTANT: Edit .env and add your NVIDIA_API_KEY"
    echo "Get one at: https://build.nvidia.com"
    echo ""
    echo "Optional: Add Google OAuth for production auth:"
    echo "  FINGRAPH_GOOGLE_CLIENT_ID=xxx.apps.googleusercontent.com"
else
    echo "✓ .env already exists"
fi

# Generate auth secret if not set
if ! grep -q "FINGRAPH_AUTH_SECRET=" .env || grep -q "FINGRAPH_AUTH_SECRET=$" .env; then
    SECRET=$(openssl rand -hex 32)
    if [[ "$OSTYPE" == "darwin"* ]]; then
        sed -i '' "s/FINGRAPH_AUTH_SECRET=.*/FINGRAPH_AUTH_SECRET=$SECRET/" .env
    else
        sed -i "s/FINGRAPH_AUTH_SECRET=.*/FINGRAPH_AUTH_SECRET=$SECRET/" .env
    fi
    echo "✓ Generated FINGRAPH_AUTH_SECRET"
fi

# Set local defaults
if [[ "$OSTYPE" == "darwin"* ]]; then
    sed -i '' 's/FINGRAPH_DATA_DIR=.*/FINGRAPH_DATA_DIR=.\/data/' .env
    sed -i '' 's/FINGRAPH_DEV_LOGIN=.*/FINGRAPH_DEV_LOGIN=1/' .env
    sed -i '' 's/FINGRAPH_CORS_ORIGIN=.*/FINGRAPH_CORS_ORIGIN=http:\/\/127.0.0.1:9100/' .env
else
    sed -i 's/FINGRAPH_DATA_DIR=.*/FINGRAPH_DATA_DIR=.\/data/' .env
    sed -i 's/FINGRAPH_DEV_LOGIN=.*/FINGRAPH_DEV_LOGIN=1/' .env
    sed -i 's/FINGRAPH_CORS_ORIGIN=.*/FINGRAPH_CORS_ORIGIN=http:\/\/127.0.0.1:9100/' .env
fi

echo "✓ Configured local defaults:"
echo "  FINGRAPH_DATA_DIR=./data"
echo "  FINGRAPH_DEV_LOGIN=1"
echo "  FINGRAPH_CORS_ORIGIN=http://127.0.0.1:9100"
echo ""

# Create data directory
mkdir -p data
echo "✓ Created data/ directory"

echo ""
echo "========================================="
echo "Local setup complete!"
echo "========================================="
echo ""
echo "To build the database locally:"
echo "  python -m ingestion.cli --ticker AAPL --start-year 2020 --end-year 2026"
echo "  python -m sandbox_engine --reset"
echo ""
echo "To start the server:"
echo "  ./run_server.sh"
echo "  # or: python -m ui.fingraph --host 127.0.0.1 --port 9100 --no-browser"
echo ""
echo "Then open: http://127.0.0.1:9100"