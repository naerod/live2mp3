# STATUS — live2mp3

## Environnements (prod + preprod)
| Env | URL | Branche | Accès |
|-----|-----|---------|-------|
| prod | https://live2mp3.naerod.com | main | vitrine publique + downloads (user) + outil (gestionnaire) |
| preprod | https://preprod-live2mp3.naerod.com | preprod | Authentik gestionnaire/admin (domaine entier) |

- Versionning : VERSION (semver) + /api/version + badge footer + tags git ; promotion via deploy-prod.
- make verify : 143 tests (T1-T10 + auth/vitrine/labels/détail/version + social/covers/import + outil lien).
- Hub : ligne live2mp3 (prod + preprod, health dots verts) dans hub.naerod.com.
- **Outil « album depuis un lien » fonctionnel (preprod v1.6.x)** : /app = assistant 5 étapes
  (analyse yt-dlp+DeepSeek → formulaire vérifiable → préparation → éditeur de coupes Peaks.js →
  rendu → album au catalogue). E2E validé le 2026-07-18 avec U2 Live in Times Square 2014.

Voir DEPLOY.md pour le workflow complet.
