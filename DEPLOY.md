# Déploiement & versionning — live2mp3

## Environnements
| Env | URL | Branche | Dossier CT110 | Accès |
|-----|-----|---------|---------------|-------|
| prod | https://live2mp3.naerod.com | `main` | /opt/apps/live2mp3 | vitrine publique + auth (user/gestionnaire) |
| preprod | https://preprod-live2mp3.naerod.com | `preprod` | /opt/apps/live2mp3-preprod | Authentik **gestionnaire/admin** (tout le domaine) |

- 2 stacks Docker isolées (app+worker+redis), volumes séparés
  (`live2mp3_projects` / `live2mp3-preprod_projects`).
- Compose paramétré par `ENV_SUFFIX` (.env) : noms de conteneurs/images/volumes.
- GitHub = source de vérité. On ne modifie jamais directement dans le conteneur.

## Workflow
1. Développement → commits sur `preprod`, `git push origin preprod`.
2. Déployer la preprod : `ssh root@192.168.1.110 /opt/apps/deploy-preprod-live2mp3.sh`
3. Validation sur https://preprod-live2mp3.naerod.com
4. Promotion en prod (merge preprod→main + tag) :
   `ssh root@192.168.1.110 /opt/apps/deploy-prod-live2mp3.sh [patch|minor|major]`

## Versionning
- `VERSION` (semver) à la racine ; `GET /api/version` → `{version, env, commit}`.
- Badge « vX.Y.Z · env » dans le footer de la vitrine.
- `deploy-prod` bump le `VERSION`, crée un tag git `vX.Y.Z` et pousse.

## Système social (profils / favoris / commentaires)
- Base **SQLite** dédiée : `projects/.l2m-social/<APP_ENV>/live2mp3.db` (+ `avatars/`).
  Stockée sous le volume `projects` déjà monté → **aucune modif de docker-compose**.
  Scoppée par environnement (prod ≠ preprod), ignorée par le scan du catalogue.
- Routes : `/api/social/*` (API), `/u/{username}` (page profil), `/avatar/{username}`.
  Lectures publiques (identité optionnelle), écritures = `require_user` (401 si anonyme).

### ⚠️ Promotion en prod — étape nginx OBLIGATOIRE (one-shot)
Sur la prod, tout chemin non listé tombe dans `location /` qui **supprime** les
en-têtes d'identité. Il faut donc ajouter un bloc **soft-auth** pour `/api/social/`
dans le `server { server_name live2mp3.naerod.com; }` de `/opt/apps/nginx/nginx.conf`
(avant de recharger nginx). `/u/` et `/avatar/` restent servis par `location /` (public).

```nginx
    # Social : soft-auth (lecture publique, l'app exige l'auth sur les écritures)
    location ^~ /api/social/ {
      auth_request /outpost.goauthentik.io/auth/nginx;
      error_page 401 403 = @social_anon;
      auth_request_set $auth_cookie $upstream_http_set_cookie;
      add_header Set-Cookie $auth_cookie;
      auth_request_set $ak_user   $upstream_http_x_authentik_username;
      auth_request_set $ak_groups $upstream_http_x_authentik_groups;
      proxy_pass http://live2mp3-app:8000;
      proxy_set_header Host $host;
      proxy_set_header X-Real-IP $remote_addr;
      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
      proxy_set_header X-Forwarded-Proto https;
      proxy_set_header X-authentik-username $ak_user;
      proxy_set_header X-authentik-groups $ak_groups;
    }
    location @social_anon {
      proxy_pass http://live2mp3-app:8000;
      proxy_set_header Host $host;
      proxy_set_header X-Forwarded-Proto https;
      proxy_set_header X-authentik-username "";
      proxy_set_header X-authentik-groups "";
    }
```
Puis `docker exec <nginx> nginx -t && ... -s reload`. (Sur preprod, rien à faire :
tout le domaine est déjà en hard-auth avec identité injectée.)

## Note d'exploitation — docker-compose.yml patché localement
Sur CT110, `docker-compose.yml` est **modifié localement** (non commité) dans chaque
checkout pour bind-monter `projects` sur `/opt/data/live2mp3/projects` (partage
prod↔preprod). Toute modif versionnée de ce fichier casse le `git pull` du déploiement.
Réconciliation ponctuelle : `git stash && git pull && git stash pop`.
→ Correctif propre à prévoir (BACKLOG) : déplacer ce bind-mount dans un
`docker-compose.override.yml` gitignoré par hôte.
