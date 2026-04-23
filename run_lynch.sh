#!/bin/bash

# Navigate to the correct project directory
PROJECT_DIR="$HOME/workplace/the-lynch-pin"
cd "$PROJECT_DIR" || exit

# Create a logs directory if it doesn't exist
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

# 2. OPTIONAL: Load .env file if you use one
if [ -f ".env" ]; then
    echo "📝 Loading .env file..." | tee -a "$LOG_FILE"
    export $(grep -v '^#' .env | xargs)
fi

# 3. TOKEN VALIDATION
echo "🔍 Checking API Tokens..." | tee -a "$LOG_FILE"

# Masking the keys for log safety (shows first 4 and last 4 chars)
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

# 4. Network Check (Wait up to 15 mins)
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

# 5. Determine command based on Day
DAY=$(date +%u) 
case $DAY in
    1) CMD="python main.py --src database/mags.txt --top 4 --excl-bad --post" ;;
    2) CMD="python main.py --src database/nasdaq_100.txt --top 10 --excl-bad --post" ;;
    3) CMD="python main.py --src database/schd.txt --top 10 --excl-bad --post" ;;
    4) CMD="python main.py --src database/smh.txt --top 6 --excl-bad --post" ;;
    5) CMD="python main.py --src database/igv.txt --top 10 --excl-bad --post" ;;
    *) echo "Weekend. No scan." ; exit 0 ;;
esac

# 6. Execute (Commented out for your test)
echo "🚀 Executing: $CMD" | tee -a "$LOG_FILE"
$CMD >> "$LOG_FILE" 2>&1

echo "--- Finished Check: $(date) ---" | tee -a "$LOG_FILE"
