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
