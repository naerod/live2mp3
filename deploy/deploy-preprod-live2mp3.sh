#!/bin/bash
# Déploie la preprod live2mp3 depuis la branche preprod. (installé sur CT110:/opt/apps/)
set -e
ZONE=a4fee77bc4ab0e55fe65b5190a64104f
echo "[deploy-preprod-live2mp3] Démarrage..."
cd /opt/apps/live2mp3-preprod
git fetch origin
git checkout preprod
git pull origin preprod
export GIT_COMMIT=$(git rev-parse --short HEAD)
docker compose build app
docker compose up -d
if [ -f /root/.cloudflare_token ]; then
  curl -s -X POST "https://api.cloudflare.com/client/v4/zones/$ZONE/purge_cache" \
    -H "Authorization: Bearer $(cat /root/.cloudflare_token)" \
    -H "Content-Type: application/json" \
    --data '{"hosts":["preprod-live2mp3.naerod.com"]}' >/dev/null && echo "  cache purgé"
fi
echo "[deploy-preprod-live2mp3] ✓ preprod à jour (commit $GIT_COMMIT)"
