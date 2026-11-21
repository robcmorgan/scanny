#!/usr/bin/env bash
# Deploy the working tree to the scanny container on otis and rebuild.
# Ships code only — .env, secrets/ and data/ (DB, tokens) live on otis and are never overwritten.
set -euo pipefail
cd "$(dirname "$0")"
tar czf - --exclude=.git --exclude=__pycache__ --exclude='*.db*' --exclude=filemaker-import \
  --exclude='*.log' --exclude=.env --exclude=secrets --exclude=data . \
  | ssh otis 'mkdir -p ~/scanny && tar xzf - -C ~/scanny'
ssh otis 'cd ~/scanny && docker compose up -d --build && docker compose ps'
