#!/usr/bin/env bash
# Start the bridge. It opens no listener: it joins the Stage with the bot's
# participant token and streams each conversation to Nova Sonic over Bedrock,
# using the instance role.
#   BOT_TOKEN_FILE=~/bot-token ./infrastructure/run.sh     # preferred: token in a 0600 file
#   BOT_TOKEN=eyJ... ./infrastructure/run.sh               # or in the environment
set -euo pipefail

if [ -z "${BOT_TOKEN_FILE:-}" ] && [ -z "${BOT_TOKEN:-}" ]; then
  echo "set BOT_TOKEN_FILE (or BOT_TOKEN) to the bot's participant token; see README" >&2
  exit 1
fi

ROOT=$(cd "$(dirname "$0")/.." && pwd)
exec "$ROOT/src/bridge/bin/bridge"
