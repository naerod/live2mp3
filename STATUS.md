# STATUS — live2mp3

## make verify : 50 passed, 0 failed
T1-T10 (manifest, render, tags, artwork, waveform/silence, DeepSeek réel,
data_disc, audio_cd, API+SSE+gating, bundle).

## Déploiement CT110 — EN LIGNE (public + SSO)
- App : `/opt/apps/live2mp3` (app+worker+redis), image live2mp3:latest.
- SSO : `/opt/apps/authentik` (Authentik server+worker+postgres+redis).
- URLs publiques (Cloudflare Tunnel) :
  - https://live2mp3.naerod.com  — vitrine publique + outil/downloads gatés
  - https://auth.naerod.com       — login SSO (portail utilisateur)
  - https://admin.naerod.com      — admin Authentik (superuser)
- Rôles : anonyme (vitrine) / live2mp3-user (download) / live2mp3-gestionnaire
  (outil+IA) / superuser (tout + gestion des accès).
- Vérifs live : vitrine 200 public, /app -> redirection login auth.naerod.com,
  admin API -> 403 sans auth, catalogue public 200.

## Reste (manuel / suite)
- 1er login navigateur : dorian (mdp temporaire Bitwarden) -> changer dans /if/user/.
- Ajouter des utilisateurs via admin.naerod.com (UI Authentik).
- SESSION_SECRET de l'auth-service historique (hub/preprods) : à durcir + migrer
  vers Authentik (Phase ultérieure).
