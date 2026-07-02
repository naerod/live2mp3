# PLAN — live2mp3

Application web auto-hébergée (Docker sur CT110, accès Tailscale only) qui transforme la captation d'un concert en livrable complet : MP3 tagués, clips MP4, pochettes PDF, image disque, bundle ZIP.

## Architecture

```
┌─────────────┐   POST /jobs    ┌──────────────┐   enqueue   ┌────────────┐
│  Frontend    │ ──────────────▶ │  FastAPI      │ ──────────▶ │  Worker RQ  │
│  SPA vanilla │ ◀── SSE state ──│  (app)        │ ◀── Redis ──│  (pipeline) │
│  + Peaks.js  │                 └──────────────┘             └────────────┘
└─────────────┘                        │                            │
                                       └──────── volume projects/ ──┘
```

- **3 conteneurs** (docker-compose) : `app` (FastAPI + frontend statique), `worker` (RQ), `redis`.
- **Manifest = source unique de vérité** : `projects/<slug>/manifest.yaml`, schéma du brief. Chaque stage lit/écrit le manifest ; `pipeline_state` persiste l'avancement ; règle anti-écrasement `locked: true` respectée.
- **Idempotence** : chaque stage est une fonction Python rejouable (`python -m backend.pipeline.<stage> <slug>`), skippée si `done` sauf `--force`.
- **Progression** : SSE (`/api/jobs/{id}/events`) — plus simple que WebSocket, unidirectionnel suffit.

## Arborescence repo

```
live2mp3/
├── backend/
│   ├── main.py              # FastAPI : routes jobs, manifest, SSE, fichiers
│   ├── manifest.py          # load/save/validate manifest.yaml + slugify
│   ├── pipeline/
│   │   ├── download.py      # Stage 1 — yt-dlp + extraction WAV (CBR pour waveform)
│   │   ├── preanalyze.py    # Stage 2 — audiowaveform + whisper + silencedetect + DeepSeek
│   │   ├── render.py        # Stage 4 — ffmpeg MP3 (-q:a 0) + MP4 (libx264 crf 18)
│   │   ├── tags.py          # Stage 5 — mutagen ID3 + APIC ; ffmpeg -metadata MP4
│   │   ├── artwork.py       # Stage 6 — templates HTML → Chromium headless → PDF
│   │   ├── disc.py          # Stage 7 — genisoimage (data_disc) / CUE+WAV (audio_cd)
│   │   └── bundle.py        # Stage 8 — ZIP final
│   └── llm.py               # client DeepSeek (format OpenAI, deepseek-v4-flash)
├── frontend/                # SPA : formulaire, page progression SSE, écran Peaks.js, récap
├── templates/               # front_insert.html + tray_card.html (fournis par toi — point d'arrêt A)
├── tests/                   # T1–T10 (voir Vérification)
├── docker-compose.yml       # app + worker + redis, volumes projects/ et templates/
├── Dockerfile               # multi-stage : audiowaveform compilé + python slim + ffmpeg/yt-dlp/chromium/genisoimage
├── Makefile                 # up, verify, export-image, import-image
├── .env.example
├── README.md / PLAN.md / STATUS.md / DECISIONS.md
```

## Lots (ordre du brief)

**Lot 1 — Socle : manifest + render + tags**
`manifest.py` (schéma, validation, slugify, nettoyage caractères spéciaux dans noms de fichiers), `render.py`, `tags.py`, structure repo, Makefile, Dockerfile de base, tests T1–T3 sur fixture audio générée localement. → commit + push.

**Lot 2 — Pochettes**
`artwork.py` : injection variables manifest dans les templates HTML fournis, rendu Chromium `--headless=new --print-to-pdf`, fallback cover typographique si `album.cover` null. Front insert 120×120 mm, tray card A4 avec J-card 150×118, tracklist 2 colonnes. → **point d'arrêt A : je te demande les templates avant ce lot.**

**Lot 3 — Assist IA**
`preanalyze.py` : audiowaveform `-b 8`, faster-whisper local (détection GPU `nvidia-smi` → **point d'arrêt C : proposition de modèle, attente OK**), silencedetect, appel DeepSeek JSON-only, écriture timecodes `locked: false`. UI Peaks.js : segments draggables, modes Express/Précision, validation → `locked: true`.

**Lot 4 — Images disque**
`disc.py` : ISO via genisoimage, CUE+WAV Red Book (gap 2 s configurable), placeholder `dvd_video` (hors V1, signalé UI + doc). → **point d'arrêt B : data_disc ou audio_cd par défaut** (les deux sont implémentés et testés ; la question porte sur le défaut UI).

**Lot 5 — Intégration**
FastAPI complet (jobs RQ, SSE), formulaire web (Google Sans, fond `#0d0f13`, accent `#8893f2`, dark/light + FR/EN + Material Symbols), bundle ZIP, écran récap, docker-compose final, README (install, secrets, export/import image, déploiement CT110, Nginx allowlist `100.64.0.0/10`).

## Vérification — `make verify` (tests T1–T10)

Le brief référence des tests d'acceptation T1–T10 ; je les implémente dans `tests/` comme suit (mappage soumis à ta validation) :

| Test | Vérifie |
|---|---|
| T1 | Manifest : load/save/validation, slugify, règle `locked` anti-écrasement |
| T2 | Render : coupes ffmpeg exactes (durées MP3/MP4 vs manifest, fixture synthétique) |
| T3 | Tags : ID3 complets + APIC présents (relecture mutagen) |
| T4 | Artwork : PDF générés, dimensions exactes (120×120 mm / A4), fallback typo |
| T5 | Silencedetect + waveform.dat générés et cohérents |
| T6 | DeepSeek : parsing réponse JSON, timecodes plausibles, respect `locked` (mock + appel réel) |
| T7 | data_disc : ISO montable, contenu conforme |
| T8 | audio_cd : CUE valide (parsing), WAV concaténé Red Book 44.1 kHz 16 bit |
| T9 | API : création job → pipeline complet sur fixture → états SSE |
| T10 | Bundle : ZIP complet (audio, vidéo, PDF, image disque, manifest) |

Tests sur **fixture synthétique** (audio+vidéo générés par ffmpeg avec bips espacés) — pas de téléchargement réel (point d'arrêt D : URL réelle fournie par toi uniquement).

## Sécurité (déjà appliqué)

- Clé DeepSeek lue depuis le partage → `.env` (chmod 600), jamais affichée ni loggée.
- `.gitignore` en place avant tout commit : `.env`, `*.txt`, `projects/`, `build/`, `source/`.

## Décisions à consigner dans DECISIONS.md dès validation

- SSE plutôt que WebSocket (unidirectionnel suffit, plus simple derrière Nginx).
- SPA vanilla JS (pas de Vue) : une seule page complexe (Peaks.js), pas besoin de framework.
- audiowaveform compilé en stage Docker séparé (pas de paquet Debian officiel fiable).
- Tests sur fixtures synthétiques ffmpeg (reproductible, pas de dépendance réseau).

## Points d'arrêt humain planifiés

- **A** (avant Lot 2) : templates pochette — je te les demande.
- **B** (Lot 4) : choix disque par défaut.
- **C** (Lot 3) : détection GPU + proposition modèle Whisper, attente OK.
- **D** : URL source réelle fournie par toi ; aucun téléchargement de ma propre initiative.
