# STATUS — live2mp3

## Environnements (prod + preprod)
| Env | URL | Branche | Accès |
|-----|-----|---------|-------|
| prod | https://live2mp3.naerod.com | main | vitrine publique + downloads (user) + outil (gestionnaire) |
| preprod | https://preprod-live2mp3.naerod.com | preprod | Authentik gestionnaire/admin (domaine entier) |

- Versionning : VERSION (semver) + /api/version + badge footer + tags git ; promotion via deploy-prod.
- make verify : 60 tests T1-T10 + auth/vitrine/labels/détail/version.
- Hub : ligne live2mp3 (prod + preprod, health dots verts) dans hub.naerod.com.

Voir DEPLOY.md pour le workflow complet.
