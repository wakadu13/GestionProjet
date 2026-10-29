#!/usr/bin/env bash
# Lance Hangar (installe automatiquement les dépendances au premier lancement).
cd "$(dirname "$0")"
UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
if [ ! -x "$UV" ]; then
  echo "uv est introuvable. Installe-le avec : curl -LsSf https://astral.sh/uv/install.sh | sh"
  exit 1
fi
exec "$UV" run hangar run "$@"
