#!/bin/bash
# Déploie la preprod live2mp3 depuis la branche preprod + bump patch version.
set -e
ZONE=a4fee77bc4ab0e55fe65b5190a64104f
echo "[deploy-preprod-live2mp3] Démarrage..."
cd /opt/apps/live2mp3-preprod

# Stash les modifications locales (ex: bind-mount dans docker-compose.yml)
STASHED=0
if ! git diff --quiet || ! git diff --cached --quiet; then
  git stash
  STASHED=1
fi

git fetch origin
git checkout preprod
PREV_REV=$(git rev-parse HEAD)   # ce qui tourne en preprod avant mise à jour
git pull origin preprod

# Bump patch version
CUR=$(cat VERSION)
IFS=. read -r MA MI PA <<< "$CUR"
PA=$((PA+1))
NEW="$MA.$MI.$PA"
# Garde-fou changelog (2026-09-10) : chaque déploiement preprod doit apporter
# son entrée de changelog (FR+EN) ; l'entrée de tête peut viser la prochaine
# version (contrôle souple). Pas de contournement.
if ! changelog-guard check --repo . --base "$PREV_REV" --mode preprod --version "$NEW"; then
  git reset -q --hard "$PREV_REV"
  [ "$STASHED" = "1" ] && git stash pop
  echo "[deploy-preprod] ✗ annulé, preprod inchangée"
  exit 1
fi
echo "$NEW" > VERSION
git add VERSION
git commit -m "Release v$NEW"
git push origin preprod

[ "$STASHED" = "1" ] && git stash pop

export GIT_COMMIT=$(git rev-parse --short HEAD)
docker compose build app
docker compose up -d
docker exec nginx nginx -s reload 2>/dev/null || true
if [ -f /root/.cloudflare_token ]; then
  curl -s -X POST "https://api.cloudflare.com/client/v4/zones/$ZONE/purge_cache" \
    -H "Authorization: Bearer $(cat /root/.cloudflare_token)" \
    -H "Content-Type: application/json" \
    --data '{"hosts":["preprod-live2mp3.naerod.com"]}' >/dev/null && echo "  cache purgé"
fi
echo "[deploy-preprod-live2mp3] ✓ preprod v$NEW déployée (commit $GIT_COMMIT)"
