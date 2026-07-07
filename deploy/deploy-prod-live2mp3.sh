#!/bin/bash
# Promotion preprod -> prod : merge, bump semver, tag git, déploiement. (CT110:/opt/apps/)
# Usage : deploy-prod-live2mp3.sh [patch|minor|major]   (défaut: patch)
set -e
ZONE=a4fee77bc4ab0e55fe65b5190a64104f
BUMP="${1:-patch}"
echo "[deploy-prod-live2mp3] Release ($BUMP)..."
cd /opt/apps/live2mp3
git fetch origin
git checkout main
git pull origin main
git merge --no-edit origin/preprod
CUR=$(cat VERSION); IFS=. read -r MA MI PA <<< "$CUR"
case "$BUMP" in
  major) MA=$((MA+1)); MI=0; PA=0 ;;
  minor) MI=$((MI+1)); PA=0 ;;
  patch|*) PA=$((PA+1)) ;;
esac
NEW="$MA.$MI.$PA"; echo "$NEW" > VERSION
git add VERSION; git commit -m "Release v$NEW"; git tag "v$NEW"
git push origin main --tags
export GIT_COMMIT=$(git rev-parse --short HEAD)
docker compose build app
docker compose up -d
if [ -f /root/.cloudflare_token ]; then
  curl -s -X POST "https://api.cloudflare.com/client/v4/zones/$ZONE/purge_cache" \
    -H "Authorization: Bearer $(cat /root/.cloudflare_token)" \
    -H "Content-Type: application/json" \
    --data '{"hosts":["live2mp3.naerod.com"]}' >/dev/null && echo "  cache purgé"
fi
echo "[deploy-prod-live2mp3] ✓ prod v$NEW déployée (commit $GIT_COMMIT)"
