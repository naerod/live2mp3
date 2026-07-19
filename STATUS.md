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

- **Suivi & pages d'entités (preprod v1.8.5)** : suivre artistes (id Deezer),
  festivals, lieux, utilisateurs (table `follows` + cloche). Pages auto
  `/artist/{id}` · `/festival/{slug}` · `/venue/{slug}` regroupant tous les
  posts liés, bouton Suivre/Suivi + cloche façon X/YouTube. Métadonnées
  canoniques (artiste + invités + festival) en liste déroulante dans l'éditeur.
  Badge de rôle (utilisateur/gestionnaire/admin) sur les profils.
- **Notifications in-app (preprod v1.8.6)** : fan-out à la publication d'un album
  vers les abonnés concernés (cloche + préférences par type/canal, dédup,
  idempotent). Cloche dans le header (compteur + dropdown), page `/notifications`,
  page `/settings` (préférences RGPD, email prévu mais coupé). Phase 3 (envoi
  email) à venir — voir NOTES.md/BACKLOG.md.

Voir DEPLOY.md pour le workflow complet.
