# Déploiement & versionning — live2mp3

## Environnements
| Env | URL | Branche | Dossier CT110 | Accès |
|-----|-----|---------|---------------|-------|
| prod | https://live2mp3.naerod.com | `main` | /opt/apps/live2mp3 | vitrine publique + auth (user/gestionnaire) |
| preprod | https://preprod-live2mp3.naerod.com | `preprod` | /opt/apps/live2mp3-preprod | Authentik **gestionnaire/admin** (tout le domaine) |

- 2 stacks Docker isolées (app+worker+redis), redis séparés.
- **Stockage des albums partagé** entre prod et preprod : bind-mount
  `PROJECTS_DIR` (.env) → `/opt/data/live2mp3/projects` sur CT110.
  Les données sociales restent scindées par env (`.l2m-social/<APP_ENV>/`).
  `PROJECTS_DIR` est obligatoire (`${PROJECTS_DIR:?}`) : un .env incomplet
  fait échouer le déploiement au lieu de servir un stockage vide.
  Les anciens volumes nommés `live2mp3*_projects` ne sont plus utilisés.
- Compose paramétré par `ENV_SUFFIX` (.env) : noms de conteneurs/images/volumes.
- **Ne jamais patcher `docker-compose.yml` à la main sur CT110** : les scripts de
  déploiement stashent les modifications locales avant `docker compose up -d`,
  donc le patch est ignoré par les conteneurs tout en restant visible sur le
  disque. Toute config d'infra doit être commitée dans le repo.
- GitHub = source de vérité. On ne modifie jamais directement dans le conteneur.
- **Deux repos GitHub** (schéma commun aux sites) : `naerod/live2mp3` (public,
  branche `main` = prod) et `naerod/preprod-live2mp3` (**privé**, branche
  `preprod`). Ne jamais pousser `preprod` sur le repo public.
  Sur CT102, remotes du clone `workspace/live2mp3` : `origin` = repo public,
  `preprod-origin` = repo privé (la branche locale `preprod` tracke
  `preprod-origin/preprod`).

## Workflow
1. Développement → commits sur `preprod`, `git push origin preprod`.
2. Déployer la preprod : `ssh root@192.168.1.110 /opt/apps/deploy-preprod-live2mp3.sh`
   → bumpe automatiquement le **patch** (ex: v1.4.6 → v1.4.7), commit + push.
3. Validation sur https://preprod-live2mp3.naerod.com
4. Promotion en prod (merge preprod→main + tag) :
   `ssh root@192.168.1.110 /opt/apps/deploy-prod-live2mp3.sh`
   → bumpe automatiquement le **minor** (ex: v1.4.x → v1.5.0).

## Versionning
- `VERSION` (semver) à la racine ; `GET /api/version` → `{version, env, commit}`.
- Badge « vX.Y.Z · env » dans le footer de la vitrine.
- **Convention :**
  - Chaque déploiement sur **preprod** → bump **patch** : `v1.4.6 → v1.4.7 → 1.4.8 …`
  - **Promotion en prod** → bump **minor**, patch remis à 0 : `v1.4.x → v1.5.0`
  - `deploy-prod [major]` si rupture de compatibilité majeure.
- `deploy-preprod` bump patch, commit `Release vX.Y.Z`, pousse sur `preprod`.
- `deploy-prod` merge `preprod→main`, bump minor, crée tag git `vX.Y.Z`, pousse.

### ⚠️ Réaligner la preprod après chaque promotion
La promotion bump le **minor** côté prod sans toucher au `VERSION` de la branche
`preprod` : les deux lignes divergent d'une version mineure à chaque fois. Non
corrigée, la dérive s'accumule — constaté le 2026-08-03, prod en **v1.19.0**
face à une preprod encore en **v1.15.41** après une dizaine de promotions, au
point qu'on ne pouvait plus dire au badge près si la preprod était en avance ou
en retard sur la prod.

**Juste après un `deploy-prod`**, replacer la preprod sur la nouvelle ligne :

```bash
cd ~/workspace/live2mp3 && git checkout preprod && echo "<version prod>" > VERSION
```

puis commiter, pousser et redéployer la preprod (le script portera le patch à
`.1`). La preprod doit toujours se lire comme « la prod, plus les patches en
cours de validation ».

Le script `deploy-prod` retient déjà `max(VERSION prod, VERSION preprod)` avant
de bumper : une preprod en avance ne casse donc jamais la numérotation prod.

## Système social (profils / favoris / commentaires)
- Base **SQLite** dédiée : `projects/.l2m-social/<APP_ENV>/live2mp3.db` (+ `avatars/`).
  Stockée sous le volume `projects` déjà monté → **aucune modif de docker-compose**.
  Scoppée par environnement (prod ≠ preprod), ignorée par le scan du catalogue.
- Routes : `/api/social/*` (API), `/u/{username}` (page profil), `/avatar/{username}`.
  Lectures publiques (identité optionnelle), écritures = `require_user` (401 si anonyme).

### ✅ Bloc nginx soft-auth `/api/social/` — DÉJÀ APPLIQUÉ (vérifié 2026-08-02)
Le bloc est en place dans le `server { server_name live2mp3.naerod.com; }` de
`/opt/apps/nginx/nginx.conf`. **Rien à faire lors des prochaines promotions.**
Cette section était rédigée comme une étape à venir et n'avait jamais été mise à
jour après son application : elle a fait annoncer à tort une action nécessaire.

Vérifier avant d'affirmer quoi que ce soit sur ce point :
```bash
ssh root@192.168.1.110 'awk "/server_name live2mp3.naerod.com/,/^}/" /opt/apps/nginx/nginx.conf | grep -n "location.*social"'
```

La preprod n'a pas besoin de ce bloc : tout le domaine passe par `auth_request`
dans `location /`, les en-têtes d'identité sont donc toujours présents.

Contexte (pourquoi ce bloc existe) : sur la prod, tout chemin non listé tombe
dans `location /` qui **supprime** les en-têtes d'identité. `/u/` et `/avatar/`
restent servis par `location /` (public). Configuration en place :

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
