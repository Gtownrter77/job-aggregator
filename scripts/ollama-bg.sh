#!/usr/bin/env bash
# Start a local Ollama server (free, open source) in the background if it isn't
# already answering. Survives the calling shell (setsid + nohup). Logs: logs/ollama.log
set -uo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs
URL="${OLLAMA_URL:-http://127.0.0.1:11434}"
if curl -fsS -m 3 "$URL/api/tags" >/dev/null 2>&1; then
  echo "ollama already running"; exit 0
fi
OLLAMA_BIN="${OLLAMA_BIN:-$(command -v ollama || echo /usr/local/bin/ollama)}"
# CPU box: one request at a time, one model in RAM, unload after 15 min idle.
export OLLAMA_HOST=127.0.0.1:11434 OLLAMA_NUM_PARALLEL=1 OLLAMA_MAX_LOADED_MODELS=1 OLLAMA_KEEP_ALIVE=15m
setsid nohup "$OLLAMA_BIN" serve >> logs/ollama.log 2>&1 < /dev/null &
echo $! > logs/ollama.pid
for _ in $(seq 1 30); do
  curl -fsS -m 2 "$URL/api/tags" >/dev/null 2>&1 && { echo "ollama started (pid $(cat logs/ollama.pid))"; exit 0; }
  sleep 1
done
echo "ollama failed to start; see logs/ollama.log" >&2; exit 1
