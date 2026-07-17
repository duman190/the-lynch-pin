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
check_token "THREADS_ACCESS_TOKEN"
check_token "THREADS_USER_ID"

DAY=$(date +%u)

# 4. Weekly Threads Token Refresh (Saturdays)
if [ "$DAY" = "6" ]; then
    echo "🔄 Refreshing Threads long-lived token..." | tee -a "$LOG_FILE"
    if [ -n "$THREADS_ACCESS_TOKEN" ]; then
        REFRESH_RESPONSE=$(curl -s "https://graph.threads.net/refresh_access_token?grant_type=th_refresh_token&access_token=$THREADS_ACCESS_TOKEN")
        NEW_TOKEN=$(echo "$REFRESH_RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('access_token',''))" 2>/dev/null)
        if [ -n "$NEW_TOKEN" ]; then
            # Update token in venv/bin/activate
            sed -i '' "s|export THREADS_ACCESS_TOKEN=.*|export THREADS_ACCESS_TOKEN=\"$NEW_TOKEN\"|" venv/bin/activate
            export THREADS_ACCESS_TOKEN="$NEW_TOKEN"
            EXPIRES=$(echo "$REFRESH_RESPONSE" | python3 -c "import sys,json; print(int(json.load(sys.stdin).get('expires_in',0))//86400)" 2>/dev/null)
            echo "  [✓] Token refreshed. Valid for ${EXPIRES} days." | tee -a "$LOG_FILE"
        else
            echo "  [!] Refresh failed: $REFRESH_RESPONSE" | tee -a "$LOG_FILE"
        fi
    else
        echo "  [!] THREADS_ACCESS_TOKEN not set, skipping refresh." | tee -a "$LOG_FILE"
    fi
fi

# 5. Network Check with Stabilization Delay
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
# Generates a random sleep between 60 and 180 seconds
RAND_SLEEP=$((60 + RANDOM % 121))
echo "  ⌛ Shifting start time by ${RAND_SLEEP}s to avoid the 1:00 PM API rush..." | tee -a "$LOG_FILE"
sleep $RAND_SLEEP

# 6. Determine command based on Day (1=Mon, 5=Fri)
case $DAY in
    1) ARGS="main.py --src database/mag7.txt --top 5 --excl-bad --post --post_threads" ;;
    2) ARGS="main.py --src database/nasdaq_100.txt --top 8 --excl-bad --post --post_threads" ;;
    3) ARGS="main.py --src database/schd.txt --top 8 --excl-bad --post --post_threads" ;;
    4) ARGS="main.py --src database/smh.txt --top 8 --excl-bad --post --post_threads" ;;
    5) ARGS="main.py --src database/igv.txt --top 8 --excl-bad --post --post_threads" ;;
    6) ARGS="main.py --weekly --top 8 --excl-bad --post --post_threads" ;;
    *) echo "Sunday. No scan." | tee -a "$LOG_FILE" ; exit 0 ;;
esac

# 7. Execute with Caffeinate
# -i prevents idle sleep, -s prevents system sleep when plugged in.
echo "🚀 Executing: $PYTHON_EXEC $ARGS" | tee -a "$LOG_FILE"

caffeinate -is $PYTHON_EXEC $ARGS >> "$LOG_FILE" 2>&1

echo "--- Finished Check: $(date) ---" | tee -a "$LOG_FILE"
