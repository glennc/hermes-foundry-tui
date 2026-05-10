#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! command -v azd >/dev/null 2>&1; then
  echo "Foundry remote TUI requires azd on PATH." >&2
  exit 1
fi

cd "$ROOT_DIR"

azd_env_get() {
  local name="$1"
  local value

  if ! value="$(azd env get-value "$name" 2>/dev/null)"; then
    echo "azd environment value $name is not set. Run 'azd up' or select the deployed environment." >&2
    exit 1
  fi

  if [[ -z "$value" ]]; then
    echo "azd environment value $name is empty. Run 'azd up' or select the deployed environment." >&2
    exit 1
  fi

  printf '%s\n' "$value"
}

extract_api_version() {
  local endpoint="$1"
  local api_version

  api_version="$(printf '%s\n' "$endpoint" | sed -n 's/.*[?&]api-version=\([^&]*\).*/\1/p')"
  if [[ -z "$api_version" ]]; then
    echo "Could not parse api-version from AGENT_HERMES_FOUNDRY_AGENT_INVOCATIONS_ENDPOINT." >&2
    echo "Set HERMES_FOUNDRY_API_VERSION explicitly or redeploy the agent." >&2
    exit 1
  fi

  printf '%s\n' "$api_version"
}

export HERMES_FOUNDRY_ENDPOINT="$(azd_env_get AZURE_AI_PROJECT_ENDPOINT)"
export HERMES_FOUNDRY_AGENT_NAME="$(azd_env_get AGENT_HERMES_FOUNDRY_AGENT_NAME)"
export HERMES_FOUNDRY_API_VERSION="$(extract_api_version "$(azd_env_get AGENT_HERMES_FOUNDRY_AGENT_INVOCATIONS_ENDPOINT)")"

unset HERMES_FOUNDRY_INVOCATIONS_PATH
unset HERMES_FOUNDRY_INVOCATIONS_URL

exec "$ROOT_DIR/scripts/run-foundry-tui.sh" "$@"
