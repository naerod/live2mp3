# DECISIONS — live2mp3

Chaque choix technique non couvert explicitement par le brief.

## D1 — SSE plutôt que WebSocket
Progression unidirectionnelle serveur→client : SSE suffit et passe mieux
derrière Nginx sans config upgrade. WebSocket surdimensionné.

## D2 — SPA vanilla JS (pas de Vue)
Une seule page réellement complexe (éditeur Peaks.js). Pas de framework
pour limiter la dette et le poids de l'image.

## D3 — audiowaveform : binaire BBC dans l'image Docker, fallback Python en test
Pas de paquet Debian fiable pour bbc/audiowaveform → compilé dans un stage
Docker dédié. En environnement de test sans le binaire, un générateur
Python produit un .dat au même format binaire (header v2 + int16), pour
que T5 reste vert hors Docker.

## D4 — Tests sur fixtures synthétiques ffmpeg
Reproductibles, sans réseau. Master audio = 4 tonalités de 3 s ; master
vidéo = mire testsrc. Respecte le point d'arrêt D (aucun téléchargement
sans URL fournie par l'utilisateur).

## D5 — Templates pochette : versions fonctionnelles par défaut
Le brief prévoit que l'utilisateur fournit les templates (point d'arrêt A).
En mode autonome demandé, j'ai créé des templates conformes au thème décrit
(fond noir mat + grain, rouge sang #d81f18, Anton/Pirata One/Oswald,
front 120×120 mm, tray card A4 J-card 150×118, tracklist 2 colonnes).
**À remplacer** par les templates définitifs de l'utilisateur quand fournis :
il suffit d'écraser templates/front_insert.html et templates/tray_card.html
(mêmes variables Jinja2).

## D6 — Whisper : détection GPU auto (point d'arrêt C géré en autonomie)
`nvidia-smi` détecté → large-v3 sur GPU ; sinon fallback CPU `medium`.
Choix logué, pas de blocage en mode autonome.

## D7 — Bascule accès public + SSO Authentik (2026-07-03)
Changement de cap assumé par le propriétaire vs brief initial ("Tailscale only,
pas d'exposition") : l'app est désormais **publique** sur `live2mp3.naerod.com`
(Cloudflare Tunnel), protégée par **Authentik** (SSO OIDC/forward-auth).
- Moteur d'auth centralisé = **Authentik** (server+worker+postgres+redis, CT110).
- `auth.naerod.com` (login public), `admin.naerod.com` (admin, superuser only),
  gérés par l'UI native Authentik — pas de hub custom (bouton depuis hub/admin).
- Rôles = groupes Authentik : `live2mp3-user` (download), `live2mp3-gestionnaire`
  (outil + IA), superuser (tout). Vitrine `/` publique.
- nginx forward-auth : `/` public ; `/app`, `/api/jobs`, `/download` derrière
  `auth_request` vers l'outpost embarqué. L'app applique le rôle fin via les
  en-têtes `X-authentik-groups` (non spoofables, posés par nginx depuis la
  sous-requête d'auth ; neutralisés sur les routes publiques).
- Ajout d'un site futur = 1 application/provider Authentik + ~5 lignes nginx.

## Slug d'album : identifiant stable + correction manuelle
- Le slug (`artiste-date`) est fixé **une fois** à la création et sert d'ID :
  dossier projet + clé des tables sociales (favoris, commentaires, pochettes,
  pochettes par piste, notifications, annonces).
- **Pas de renommage automatique** à chaque édition d'info : une URL doit rester
  stable (liens partagés, favoris, référencement). Renommer casse ces liens.
- À la place : action **manuelle** « Corriger l'URL » (page de gestion) qui
  renomme le dossier, migre les 6 tables et pose un **alias → redirection 301**
  de l'ancienne URL (table `slug_aliases`, chaînes compressées). Réservée au
  gestionnaire. Cf. `backend/slugrename.py`.
- Le nom de fichier des **téléchargements** (`AAAA-MM-JJ_artiste_titre_type.zip`)
  est, lui, calculé à la volée depuis le manifeste → suit les infos sans toucher
  au slug. Cf. `manifest.download_stem`.
