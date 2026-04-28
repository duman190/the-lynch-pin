#!/bin/bash

# --- CONFIGURATION ---
PROJECT_DIR="$HOME/workplace/the-lynch-pin"
PYTHON_EXEC="$PROJECT_DIR/venv/bin/python"
cd "$PROJECT_DIR" || exit

# Create logs directory
mkdir -p logs
LOG_FILE="logs/run_$(date +%Y%m%d).log"

echo "--- Automation Check: $(date) ---" | tee -a "$LOG_FILE"

# 1. Activate Virtual Environment
if [ -f "venv/bin/activate" ]; then
    source venv/bin/activate
else
    echo "❌ Virtual env not found!" | tee -a "$LOG_FILE"
    exit 1
fi

# 2. Robust .env Loading (Handles special chars/spaces)
if [ -f ".env" ]; then
    echo "📝 Loading .env file..." | tee -a "$LOG_FILE"
    set -a
    source .env
    set +a
fi

# 3. TOKEN VALIDATION (Masked for logs)
echo "🔍 Checking API Tokens..." | tee -a "$LOG_FILE"
check_token() {
    local name=$1
    local val=${!name}
    if [ -z "$val" ]; then
        echo "  [!] $name is MISSING" | tee -a "$LOG_FILE"
    else
        echo "  [✓] $name is present (${val:0:4}...${val: -4})" | tee -a "$LOG_FILE"
    fi
}

check_token "GEMINI_API_KEY"
check_token "X_API_KEY"
check_token "X_API_SECRET"
check_token "X_ACCESS_TOKEN"
check_token "X_ACCESS_SECRET"

# 4. Network Check with Stabilization Delay
MAX_RETRIES=90 
COUNT=0
echo "🌐 Checking network..." | tee -a "$LOG_FILE"
while ! ping -c 1 -t 1 8.8.8.8 &> /dev/null; do
    if [ $COUNT -ge $MAX_RETRIES ]; then
        echo "❌ Network failed after 15 mins." | tee -a "$LOG_FILE"
        exit 1
    fi
    sleep 10
    ((COUNT++))
done

echo "  [✓] Network Online" | tee -a "$LOG_FILE"
echo "  ⌛ Waiting 20s for Wi-Fi card to fully stabilize..." | tee -a "$LOG_FILE"
sleep 20

# 5. Determine command based on Day (1=Mon, 5=Fri)
DAY=$(date +%u) 
case $DAY in
    1) ARGS="main.py --src database/mag7.txt --top 4 --excl-bad --post" ;;
    2) ARGS="main.py --src database/nasdaq_100.txt --top 10 --excl-bad --post" ;;
    3) ARGS="main.py --src database/schd.txt --top 10 --excl-bad --post" ;;
    4) ARGS="main.py --src database/smh.txt --top 6 --excl-bad --post" ;;
    5) ARGS="main.py --src database/igv.txt --top 10 --excl-bad --post" ;;
    *) echo "Weekend. No scan." | tee -a "$LOG_FILE" ; exit 0 ;;
esac

# 6. Execute with Caffeinate
# -i prevents idle sleep, -s prevents system sleep when plugged in.
echo "🚀 Executing: $PYTHON_EXEC $ARGS" | tee -a "$LOG_FILE"

caffeinate -is $PYTHON_EXEC $ARGS >> "$LOG_FILE" 2>&1

echo "--- Finished Check: $(date) ---" | tee -a "$LOG_FILE"
