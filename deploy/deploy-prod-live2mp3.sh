#!/bin/bash
# Promotion preprod -> prod : merge, bump de version (semver), tag git, déploiement.
# Usage : deploy-prod-live2mp3.sh [patch|minor|major]   (défaut: minor)
# Schéma 2 repos : preprod vit dans naerod/preprod-live2mp3 (privé),
# main dans naerod/live2mp3 (public). Le dossier claude/ (notes internes)
# ne doit jamais atterrir dans le repo public.
set -e
ZONE=a4fee77bc4ab0e55fe65b5190a64104f
BUMP="${1:-minor}"
echo "[deploy-prod-live2mp3] Release ($BUMP)..."
cd /opt/apps/live2mp3

# Stash les modifications locales (ex: bind-mount dans docker-compose.yml)
STASHED=0
if ! git diff --quiet || ! git diff --cached --quiet; then
  git stash
  STASHED=1
fi

# Remote du repo preprod privé (URL reprise du clone preprod local)
if ! git remote | grep -q "^preprod-origin$"; then
  git remote add preprod-origin "$(git -C /opt/apps/live2mp3-preprod remote get-url origin)"
fi

git fetch origin
git fetch preprod-origin
git checkout main
git pull origin main
PROD_REV=$(git rev-parse HEAD)   # ce qui tourne en prod avant promotion

# Merge sans commit pour pouvoir écarter claude/ (conflits modify/delete attendus)
git merge --no-commit --no-ff preprod-origin/preprod || true
[ -f .git/MERGE_HEAD ] || { echo "ERREUR: le merge n a pas démarré"; exit 1; }
git rm -r -f -q --ignore-unmatch claude 2>/dev/null || true
# VERSION diverge a chaque promotion (patch preprod 1.6.x vs minor prod 1.7.x).
# On garde la version PROD (--ours) comme base du bump : repartir de preprod
# donnerait une version <= prod et collisionnerait avec un tag existant
# (incident 2026-07-19 : preprod 1.6.10 + minor = 1.7.0 deja tague).
git checkout --ours VERSION 2>/dev/null && git add VERSION || true
if git ls-files -u | grep -q .; then
  echo "ERREUR: conflits non résolus :"; git ls-files -u; exit 1
fi
git commit --no-edit

# Base du bump = la version la plus elevee entre prod et preprod. La preprod
# s'incremente a chaque deploiement, la prod seulement aux promotions : partir
# de la seule version prod finirait par publier un numero inferieur au code
# embarque (et a deja provoque une collision de tag le 2026-07-19).
CUR=$(cat VERSION)
PRE=$(git show preprod-origin/preprod:VERSION 2>/dev/null || echo 0.0.0)
CUR=$(printf '%s\n%s\n' "$CUR" "$PRE" | sort -V | tail -1)
IFS=. read -r MA MI PA <<< "$CUR"
case "$BUMP" in
  none) NEW="$CUR" ;;
  major) MA=$((MA+1)); MI=0; PA=0 ;;
  minor) MI=$((MI+1)); PA=0 ;;
  patch|*) PA=$((PA+1)) ;;
esac
[ "$BUMP" != "none" ] && NEW="$MA.$MI.$PA"

# Garde-fou changelog (2026-09-10, oublis v1.24.0/v1.24.1) : refus si le
# changelog n'a pas été modifié depuis la prod actuelle ou si son entrée de
# tête ne couvre pas v$NEW. Pas de contournement — écrire l'entrée en preprod.
if ! changelog-guard check --repo . --base "$PROD_REV" --version "$NEW"; then
  git reset -q --hard "$PROD_REV"
  [ "$STASHED" = "1" ] && git stash pop
  echo "[deploy-prod-live2mp3] ✗ annulé, prod inchangée (v$(cat VERSION))"
  exit 1
fi
echo "$NEW" > VERSION
git add VERSION
git commit -m "Release v$NEW"
git tag "v$NEW"
git push origin main --tags

export GIT_COMMIT=$(git rev-parse --short HEAD)
docker compose build app
docker compose up -d
docker exec nginx nginx -s reload 2>/dev/null || true

# Restaurer les modifications locales
[ "$STASHED" = "1" ] && git stash pop

if [ -f /root/.cloudflare_token ]; then
  curl -s -X POST "https://api.cloudflare.com/client/v4/zones/$ZONE/purge_cache" \
    -H "Authorization: Bearer $(cat /root/.cloudflare_token)" \
    -H "Content-Type: application/json" \
    --data "{\"purge_everything\":true}" >/dev/null && echo "  cache purgé"
fi
echo "[deploy-prod-live2mp3] ✓ prod v$NEW déployée (commit $GIT_COMMIT)"

# --- Réalignement preprod sur la version prod ---------------------------------
# Après promotion, prod et preprod ont le même code : la preprod doit porter le
# même numéro de version (sinon elle affiche une version < prod). Règle générale
# à tous les sites : la preprod SUIT la prod après un push prod.
echo "[deploy-prod-live2mp3] Réalignement preprod -> v$NEW..."
cd /opt/apps/live2mp3-preprod
PSTASH=0
if ! git diff --quiet || ! git diff --cached --quiet; then git stash; PSTASH=1; fi
git fetch origin
git checkout preprod
git pull origin preprod
if [ "$(cat VERSION)" != "$NEW" ]; then
  echo "$NEW" > VERSION
  git add VERSION
  git commit -m "Réaligne preprod sur prod v$NEW"
  git push origin preprod
fi
[ "$PSTASH" = "1" ] && git stash pop
docker compose build app
docker compose up -d
echo "[deploy-prod-live2mp3] ✓ preprod réalignée sur v$NEW"
