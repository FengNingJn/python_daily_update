#!/bin/sh
set -eu
cd /volume1/docker/nga-update
exec /usr/local/bin/docker compose -f docker-compose.yml up -d --build
