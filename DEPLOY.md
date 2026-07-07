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
