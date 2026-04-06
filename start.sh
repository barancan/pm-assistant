#!/bin/bash
set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo ""
echo "  PM Assistant"
echo "  ────────────"
echo ""

# Check Python 3.11+
# Prefer python3.11 explicitly, fall back to python3 if it meets version requirement
PYTHON_BIN=""
if command -v python3.11 &> /dev/null; then
    PYTHON_BIN="python3.11"
elif command -v python3.12 &> /dev/null; then
    PYTHON_BIN="python3.12"
elif command -v python3 &> /dev/null; then
    PYTHON_BIN="python3"
else
    echo -e "${RED}Error: python3 not found. Install Python 3.11+${NC}"
    exit 1
fi

PYTHON_VERSION=$($PYTHON_BIN -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
REQUIRED="3.11"
if [ "$(printf '%s\n' "$REQUIRED" "$PYTHON_VERSION" | sort -V | head -n1)" != "$REQUIRED" ]; then
    echo -e "${RED}Error: Python 3.11+ required. Found: $PYTHON_VERSION${NC}"
    echo -e "${YELLOW}Try: brew install python@3.11${NC}"
    exit 1
fi

echo -e "${GREEN}✓${NC} Python $PYTHON_VERSION ($PYTHON_BIN)"

# Check Ollama
if ! curl -s http://localhost:11434/api/tags > /dev/null 2>&1; then
    echo -e "${YELLOW}⚠ Ollama not running. Start Ollama and run this script again.${NC}"
    echo "  Run: ollama serve"
    echo "  Then: ollama pull gemma4:e4b"
    exit 1
fi
echo -e "${GREEN}✓${NC} Ollama running"

# Create .env from example if not exists
if [ ! -f .env ]; then
    cp .env.example .env
    echo -e "${YELLOW}⚠ Created .env from .env.example — add your ANTHROPIC_API_KEY${NC}"
fi
echo -e "${GREEN}✓${NC} Environment file present"

# Install requirements
echo "  Installing dependencies..."
$PYTHON_BIN -m pip install -r backend/requirements.txt -q
echo -e "${GREEN}✓${NC} Dependencies installed"

# Remind about file permissions
echo ""
echo -e "${YELLOW}Security reminder:${NC} Run these commands to lock workspace files:"
echo "  chmod 444 workspace/CLAUDE.md"
echo "  chmod 444 workspace/_core/**"
echo "  chmod 444 workspace/_config/**"
echo "  chmod 444 workspace/**/CONTEXT.md"
echo ""

# Start server
echo -e "${GREEN}Starting PM Assistant at http://localhost:3000${NC}"
echo "  Press Ctrl+C to stop"
echo ""

# Open browser after delay (background)
(sleep 2 && open http://localhost:3000 2>/dev/null || \
 xdg-open http://localhost:3000 2>/dev/null || \
 echo "Open http://localhost:3000 in your browser") &

# Start FastAPI
cd backend
$PYTHON_BIN -m uvicorn main:app --host 0.0.0.0 --port 3000 --reload
