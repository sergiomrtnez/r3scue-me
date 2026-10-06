#!/data/data/com.termux/files/usr/bin/bash
# ==============================================================================
# r3scue-me: Automated Provisioning & Setup Script for Termux (Android)
#
# Transforms legacy Android devices into autonomous AI automation servers.
# Supports native llama.cpp compilation, modular workflow activation,
# ntfy push notifications, and background cron scheduling.
# ==============================================================================

set -o pipefail

# ANSI Color Palette
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

echo -e "${CYAN}${BOLD}"
echo "============================================================"
echo "              🤖 r3scue-me Installer (Termux)               "
echo "   Autonomous AI Server Engine for Repurposed Androids     "
echo "============================================================"
echo -e "${NC}"

# Detect execution directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ------------------------------------------------------------------------------
# STEP 1: Environment Validation & Wake Lock
# ------------------------------------------------------------------------------
echo -e "${BLUE}[1/6] Checking Termux environment & power management...${NC}"

if command -v termux-wake-lock >/dev/null 2>&1; then
    echo -e "${GREEN}✓ Acquiring Termux Wake Lock (preventing Android deep sleep)...${NC}"
    termux-wake-lock
else
    echo -e "${YELLOW}! termux-wake-lock not found. Ensure you are running Termux from F-Droid.${NC}"
fi

# ------------------------------------------------------------------------------
# STEP 2: Package Installation
# ------------------------------------------------------------------------------
echo -e "${BLUE}[2/6] Installing core system packages...${NC}"
if command -v pkg >/dev/null 2>&1; then
    pkg update -y
    pkg install -y python openssh clang cmake git termux-api cronie make curl jq
else
    echo -e "${YELLOW}! 'pkg' manager not detected. Assuming standard Linux environment...${NC}"
    if command -v apt-get >/dev/null 2>&1; then
        sudo apt-get update -y && sudo apt-get install -y python3 python3-pip clang cmake git make curl jq cron
    fi
fi

# Setup isolated Python virtual environment (.venv) to avoid polluting global environment
echo -e "${BLUE}Configuring isolated Python virtual environment (.venv)...${NC}"
SYS_PYTHON="$(command -v python || command -v python3)"
if [ ! -d "$SCRIPT_DIR/.venv" ]; then
    "$SYS_PYTHON" -m venv "$SCRIPT_DIR/.venv"
fi
source "$SCRIPT_DIR/.venv/bin/activate"

# Install Python requirements inside .venv
echo -e "${BLUE}Installing Python library dependencies inside virtual environment...${NC}"
pip install --upgrade pip
pip install -r "$SCRIPT_DIR/requirements.txt"

# ------------------------------------------------------------------------------
# STEP 3: Notification Setup (ntfy)
# ------------------------------------------------------------------------------
echo -e "\n${BLUE}[3/6] Configuring Push Notifications (ntfy.sh)...${NC}"
echo -e "${CYAN}ntfy allows zero-registration push alerts directly to your mobile phone or desktop.${NC}"
read -rp "Enter your desired ntfy topic name [e.g. r3scue-me-alert-$(head /dev/urandom | tr -dc a-z0-9 | head -c 6)]: " NTFY_TOPIC
if [ -z "$NTFY_TOPIC" ]; then
    NTFY_TOPIC="r3scue-me-alert-$(head /dev/urandom | tr -dc a-z0-9 | head -c 6)"
fi
echo -e "${GREEN}✓ Selected ntfy topic: ${BOLD}$NTFY_TOPIC${NC}"
echo -e "  (Subscribe via the ntfy app at: https://ntfy.sh/$NTFY_TOPIC)\n"

# ------------------------------------------------------------------------------
# STEP 4: AI Engine Configuration (Cloud API vs. Native llama.cpp)
# ------------------------------------------------------------------------------
echo -e "${BLUE}[4/6] Configuring AI Inference Engine...${NC}"
echo "Choose your AI backend:"
echo "  1) Cloud API (OpenAI, OpenRouter, Groq, DeepSeek) - Fast, zero RAM usage"
echo "  2) Local Engine (Native llama.cpp) - 100% Offline, runs on-device CPU"
read -rp "Select option [1/2] (Default: 1): " AI_CHOICE
AI_CHOICE=${AI_CHOICE:-1}

AI_MODE="api"
API_BASE_URL="https://openrouter.ai/api/v1"
API_KEY=""
API_MODEL="qwen/qwen-2.5-7b-instruct"
LOCAL_BIN_PATH="$HOME/llama.cpp/build/bin/llama-cli"
LOCAL_MODEL_PATH=""
LOCAL_THREADS=4

if [ "$AI_CHOICE" = "1" ]; then
    AI_MODE="api"
    echo -e "\n${CYAN}--- Cloud API Setup ---${NC}"
    read -rp "Enter API Base URL [Default: https://openrouter.ai/api/v1]: " INPUT_URL
    API_BASE_URL=${INPUT_URL:-$API_BASE_URL}

    read -rp "Enter Model ID [Default: qwen/qwen-2.5-7b-instruct]: " INPUT_MODEL
    API_MODEL=${INPUT_MODEL:-$API_MODEL}

    read -rp "Enter your API Key: " API_KEY
    if [ -z "$API_KEY" ]; then
        echo -e "${YELLOW}! Warning: API Key left blank. Remember to export AI_API_KEY later.${NC}"
    fi

elif [ "$AI_CHOICE" = "2" ]; then
    AI_MODE="local"
    echo -e "\n${CYAN}--- Local llama.cpp Native Compilation (Zero proot overhead) ---${NC}"

    # Hardware resource audit
    TOTAL_RAM_MB=$(free -m 2>/dev/null | awk '/^Mem:/{print $2}' || echo "2048")
    echo -e "Detected System RAM: ${BOLD}${TOTAL_RAM_MB} MB${NC}"

    CPU_CORES=$(grep -c ^processor /proc/cpuinfo 2>/dev/null || echo "4")
    LOCAL_THREADS=$(( CPU_CORES > 2 ? CPU_CORES - 1 : 2 ))
    echo -e "Allocated CPU Threads for Inference: ${BOLD}${LOCAL_THREADS}${NC}"

    # Clone & compile llama.cpp natively
    LLAMA_DIR="$HOME/llama.cpp"
    if [ ! -f "$LLAMA_DIR/build/bin/llama-cli" ]; then
        echo -e "${BLUE}Cloning llama.cpp repository...${NC}"
        git clone --depth 1 https://github.com/ggerganov/llama.cpp.git "$LLAMA_DIR"
        echo -e "${BLUE}Building llama.cpp with CMake & Clang...${NC}"
        cmake -B "$LLAMA_DIR/build" -S "$LLAMA_DIR" -DCMAKE_BUILD_TYPE=Release
        cmake --build "$LLAMA_DIR/build" --config Release -j"$LOCAL_THREADS"
    else
        echo -e "${GREEN}✓ Existing llama.cpp binary found at $LLAMA_DIR/build/bin/llama-cli${NC}"
    fi
    LOCAL_BIN_PATH="$LLAMA_DIR/build/bin/llama-cli"

    # Model Selection according to RAM
    mkdir -p "$SCRIPT_DIR/models"
    echo -e "\n${CYAN}Select lightweight Quantized GGUF Model to download:${NC}"

    if [ "$TOTAL_RAM_MB" -lt 4000 ]; then
        echo -e "${YELLOW}Device has < 4GB RAM. Lightweight models recommended:${NC}"
        echo "  1) Qwen2.5 0.5B Instruct Q4_K_M (~398 MB) [Recommended]"
        echo "  2) SmolLM2 360M Instruct Q4_K_M (~240 MB)"
        echo "  3) Custom GGUF file path"
        read -rp "Selection [1-3] (Default: 1): " MODEL_PICK
        MODEL_PICK=${MODEL_PICK:-1}

        case "$MODEL_PICK" in
            2)
                MODEL_URL="https://huggingface.co/HuggingFaceTB/SmolLM2-360M-Instruct-GGUF/resolve/main/smollm2-360m-instruct-q4_k_m.gguf"
                LOCAL_MODEL_PATH="$SCRIPT_DIR/models/smollm2-360m-instruct-q4_k_m.gguf"
                ;;
            3)
                read -rp "Enter absolute path to your .gguf model: " LOCAL_MODEL_PATH
                ;;
            *)
                MODEL_URL="https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/qwen2.5-0.5b-instruct-q4_k_m.gguf"
                LOCAL_MODEL_PATH="$SCRIPT_DIR/models/qwen2.5-0.5b-instruct-q4_k_m.gguf"
                ;;
        esac
    else
        echo -e "${GREEN}Device has >= 4GB RAM. Higher capacity models supported:${NC}"
        echo "  1) Qwen2.5 1.5B Instruct Q4_K_M (~980 MB) [Recommended]"
        echo "  2) Llama 3.2 1B Instruct Q4_K_M (~800 MB)"
        echo "  3) Qwen2.5 0.5B Instruct Q4_K_M (~398 MB) [Ultra-fast]"
        echo "  4) Custom GGUF file path"
        read -rp "Selection [1-4] (Default: 1): " MODEL_PICK
        MODEL_PICK=${MODEL_PICK:-1}

        case "$MODEL_PICK" in
            2)
                MODEL_URL="https://huggingface.co/bartowski/Llama-3.2-1B-Instruct-GGUF/resolve/main/Llama-3.2-1B-Instruct-Q4_K_M.gguf"
                LOCAL_MODEL_PATH="$SCRIPT_DIR/models/llama-3.2-1b-instruct-q4_k_m.gguf"
                ;;
            3)
                MODEL_URL="https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/qwen2.5-0.5b-instruct-q4_k_m.gguf"
                LOCAL_MODEL_PATH="$SCRIPT_DIR/models/qwen2.5-0.5b-instruct-q4_k_m.gguf"
                ;;
            4)
                read -rp "Enter absolute path to your .gguf model: " LOCAL_MODEL_PATH
                ;;
            *)
                MODEL_URL="https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/qwen2.5-1.5b-instruct-q4_k_m.gguf"
                LOCAL_MODEL_PATH="$SCRIPT_DIR/models/qwen2.5-1.5b-instruct-q4_k_m.gguf"
                ;;
        esac
    fi

    if [ -n "$MODEL_URL" ] && [ ! -f "$LOCAL_MODEL_PATH" ]; then
        echo -e "${BLUE}Downloading model from Hugging Face...${NC}"
        curl -L -o "$LOCAL_MODEL_PATH" "$MODEL_URL"
    fi
fi

# ------------------------------------------------------------------------------
# STEP 5: Module Selection & User Inputs
# ------------------------------------------------------------------------------
echo -e "\n${BLUE}[5/6] Selecting Automation Module...${NC}"
echo "Choose which automation module to activate initially:"
echo "  1) Task Reminder (AI coaching & daily task breakdown)"
echo "  2) News Summarizer (Scrapes RSS/Web feeds into executive digests)"
echo "  3) Smart Notes (Ingests notes & categorizes them into Markdown vault)"
echo "  4) Deal Finder (Lightweight web scraping for keywords with AI filtering)"
read -rp "Select module [1-4] (Default: 1): " MOD_CHOICE
MOD_CHOICE=${MOD_CHOICE:-1}

ACTIVE_MODULE="task_reminder"
TASK_LIST_JSON='["Review codebase pull requests", "Run server backup routine", "Review daily objectives"]'
NEWS_URLS_JSON='["https://news.ycombinator.com/rss"]'
DEALS_URLS_JSON='["https://news.ycombinator.com/show"]'
DEALS_KEYWORDS_JSON='["open source", "release", "library"]'
RAW_NOTES_JSON='[]'

case "$MOD_CHOICE" in
    2)
        ACTIVE_MODULE="news_summarizer"
        echo -e "\n${CYAN}--- News Summarizer Configuration ---${NC}"
        read -rp "Enter news feed or website URLs separated by commas: " USER_URLS
        if [ -n "$USER_URLS" ]; then
            NEWS_URLS_JSON=$(echo "$USER_URLS" | awk -F',' '{
                printf "["
                for (i=1; i<=NF; i++) {
                    gsub(/^[ \t]+|[ \t]+$/, "", $i)
                    printf "\"%s\"%s", $i, (i==NF ? "" : ", ")
                }
                printf "]"
            }')
        fi
        ;;
    3)
        ACTIVE_MODULE="smart_notes"
        echo -e "\n${CYAN}--- Smart Notes Configuration ---${NC}"
        echo "Notes inbox will be located at: $SCRIPT_DIR/data/notes_inbox"
        echo "Organized Markdown vault will be located at: $SCRIPT_DIR/data/notes_vault"
        mkdir -p "$SCRIPT_DIR/data/notes_inbox" "$SCRIPT_DIR/data/notes_vault"

        echo "Enter initial notes (type 'DONE' on a new line when finished, or leave blank to skip):"
        USER_NOTES=()
        while true; do
            read -rp "> " NOTE_LINE || break
            if [ "$NOTE_LINE" = "DONE" ] || [ "$NOTE_LINE" = "done" ] || [ "$NOTE_LINE" = "FIN" ] || [ "$NOTE_LINE" = "fin" ]; then
                break
            fi
            if [ -z "$NOTE_LINE" ] && [ ${#USER_NOTES[@]} -eq 0 ]; then
                break
            fi
            if [ -n "$NOTE_LINE" ]; then
                USER_NOTES+=("$NOTE_LINE")
            fi
        done

        if [ ${#USER_NOTES[@]} -gt 0 ]; then
            RAW_NOTES_JSON=$(printf '%s\n' "${USER_NOTES[@]}" | jq -R . | jq -s .)
            echo -e "${GREEN}✓ Recorded ${#USER_NOTES[@]} note(s).${NC}"
        fi
        ;;
    4)
        ACTIVE_MODULE="deal_finder"
        echo -e "\n${CYAN}--- Deal Finder Configuration ---${NC}"
        read -rp "Enter target search/listing URLs separated by commas: " USER_DEALS_URLS
        if [ -n "$USER_DEALS_URLS" ]; then
            DEALS_URLS_JSON=$(echo "$USER_DEALS_URLS" | awk -F',' '{
                printf "["
                for (i=1; i<=NF; i++) {
                    gsub(/^[ \t]+|[ \t]+$/, "", $i)
                    printf "\"%s\"%s", $i, (i==NF ? "" : ", ")
                }
                printf "]"
            }')
        fi

        read -rp "Enter search keywords separated by commas (e.g. laptop, rtx, thinkpad): " USER_KEYWORDS
        if [ -n "$USER_KEYWORDS" ]; then
            DEALS_KEYWORDS_JSON=$(echo "$USER_KEYWORDS" | awk -F',' '{
                printf "["
                for (i=1; i<=NF; i++) {
                    gsub(/^[ \t]+|[ \t]+$/, "", $i)
                    printf "\"%s\"%s", $i, (i==NF ? "" : ", ")
                }
                printf "]"
            }')
        fi
        ;;
    *)
        ACTIVE_MODULE="task_reminder"
        echo -e "\n${CYAN}--- Task Reminder Configuration ---${NC}"
        echo "Enter your pending tasks (type 'DONE' on a new line when finished):"
        USER_TASKS=()
        while true; do
            read -rp "> " TASK_LINE
            if [ "$TASK_LINE" = "DONE" ] || [ "$TASK_LINE" = "done" ]; then
                break
            fi
            if [ -n "$TASK_LINE" ]; then
                USER_TASKS+=("$TASK_LINE")
            fi
        done

        if [ ${#USER_TASKS[@]} -gt 0 ]; then
            TASK_LIST_JSON=$(printf '%s\n' "${USER_TASKS[@]}" | jq -R . | jq -s .)
        fi
        ;;
esac

# ------------------------------------------------------------------------------
# STEP 6: config.json Generation
# ------------------------------------------------------------------------------
echo -e "\n${BLUE}[6/6] Generating config.json and setting up background runner...${NC}"

cat <<EOF > "$SCRIPT_DIR/config.json"
{
  "ai": {
    "mode": "$AI_MODE",
    "api": {
      "base_url": "$API_BASE_URL",
      "api_key": "$API_KEY",
      "model": "$API_MODEL",
      "timeout_seconds": 60
    },
    "local": {
      "binary_path": "$LOCAL_BIN_PATH",
      "model_path": "$LOCAL_MODEL_PATH",
      "threads": $LOCAL_THREADS,
      "context_size": 2048,
      "timeout_seconds": 180
    }
  },
  "notifications": {
    "server": "https://ntfy.sh",
    "topic": "$NTFY_TOPIC",
    "auth_token": null,
    "timeout_seconds": 15
  },
  "active_modules": [
    "$ACTIVE_MODULE"
  ],
  "modules": {
    "task_reminder": {
      "tasks": $TASK_LIST_JSON
    },
    "news_summarizer": {
      "urls": $NEWS_URLS_JSON
    },
    "smart_notes": {
      "inbox_dir": "data/notes_inbox",
      "vault_dir": "data/notes_vault",
      "raw_notes": $RAW_NOTES_JSON
    },
    "deal_finder": {
      "urls": $DEALS_URLS_JSON,
      "keywords": $DEALS_KEYWORDS_JSON
    }
  }
}
EOF

echo -e "${GREEN}✓ Successfully generated config.json!${NC}"

# ------------------------------------------------------------------------------
# Automation Scheduling (cronie / crontab)
# ------------------------------------------------------------------------------
echo -e "\n${CYAN}--- Background Execution Setup ---${NC}"
VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python"
if [ ! -f "$VENV_PYTHON" ]; then
    VENV_PYTHON="$(command -v python || command -v python3)"
fi
AGENT_SCRIPT="$SCRIPT_DIR/agent.py"
LOG_FILE="$SCRIPT_DIR/r3scue-me.log"

CRON_CMD="*/30 * * * * cd $SCRIPT_DIR && $VENV_PYTHON $AGENT_SCRIPT >> $LOG_FILE 2>&1"

if command -v crontab >/dev/null 2>&1; then
    # Add to crontab if not already registered
    (crontab -l 2>/dev/null | grep -v "r3scue-me"; echo "$CRON_CMD # r3scue-me") | crontab -
    echo -e "${GREEN}✓ Crontab configured to execute every 30 minutes using .venv.${NC}"
    # Start crond daemon if available in Termux
    pgrep crond >/dev/null 2>&1 || crond
else
    echo -e "${YELLOW}! crontab command not found. You can execute manually with:${NC}"
    echo -e "    source .venv/bin/activate && python agent.py"
fi

echo -e "\n${GREEN}${BOLD}============================================================${NC}"
echo -e "${GREEN}${BOLD}        🚀 r3scue-me Installation Completed!                ${NC}"
echo -e "${GREEN}${BOLD}============================================================${NC}"
echo -e "To run an immediate test execution:"
echo -e "  ${BOLD}source .venv/bin/activate && python agent.py${NC}\n"
echo -e "To view logs:"
echo -e "  ${BOLD}tail -f $LOG_FILE${NC}\n"
echo -e "Subscribed ntfy Topic: ${BOLD}https://ntfy.sh/$NTFY_TOPIC${NC}"
