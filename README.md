# live2mp3

Application web auto-hébergée qui transforme la captation d'un concert
(livestream ou VOD publique) en livrable complet : **MP3** découpés et
tagués, clips **MP4**, **pochettes de CD imprimables** (PDF), et **image
disque** prête à graver.

> Outil **strictement personnel**, usage privé. Aucune distribution
> publique, aucune exposition Internet. Accès **Tailscale uniquement**.

## Flux utilisateur

1. Formulaire web : lien source, métadonnées album, setlist, cover (optionnel).
2. Page de chargement : progression stage par stage (SSE).
3. Écran waveform (Peaks.js) : coupes IA pré-posées, ajustables.
4. Validation → bundle ZIP (audio + vidéo + PDF pochettes + image disque).

## Pipeline

| Stage | Outil | Sortie |
|-------|-------|--------|
| 1 Download | yt-dlp | `source/master.mkv` + `master.wav` |
| 2 Pré-analyse | audiowaveform + faster-whisper + silencedetect + DeepSeek | `waveform.dat` + timecodes |
| 3 Ajustement | Peaks.js (UI) | timecodes `locked` |
| 4 Render | ffmpeg | `build/audio/*.mp3`, `build/video/*.mp4` |
| 5 Tags | mutagen + ffmpeg | ID3 + APIC + MP4 metadata |
| 6 Pochettes | HTML → Chromium headless | `artwork/*.pdf` |
| 7 Image disque | genisoimage / CUE+WAV | ISO ou CUE+WAV |
| 8 Bundle | zip | `bundle.zip` |

Le **`manifest.yaml`** de chaque projet est la source unique de vérité :
tout en dérive. Un timecode `locked: true` n'est jamais réécrit par l'IA.

## Installation & run

```bash
cp .env.example .env      # renseigner DEEPSEEK_API_KEY
make build                # build image Docker
make up                   # app + worker + redis
```

App servie sur le port interne 8000 (voir docker-compose.yml).

## Tests d'acceptation

```bash
make verify               # T1-T10 sur fixtures synthétiques
```

Les tests n'effectuent **aucun téléchargement réseau** (fixtures ffmpeg).

## Export / import de l'image Docker

```bash
make export-image         # docker save -> live2mp3-image.tar
# copier le .tar sur la cible, puis :
make import-image         # docker load
```

## Déploiement CT110 (Tailscale only)

L'app tourne en Docker sur CT110 (`s-px-dj-docker`), derrière Nginx.
Nginx doit **allowlister `100.64.0.0/10`** et refuser tout le reste
(même schéma que l'app Picsou). Extrait :

```nginx
location / {
    allow 100.64.0.0/10;   # Tailscale
    deny  all;
    proxy_pass http://127.0.0.1:8000;
}
```

**Aucun Cloudflare Tunnel** pour cette app.

## Secrets

Toutes les clés via variables d'environnement (`.env` hors git) ou Docker
secrets. Jamais en clair dans le code, les logs ou les fichiers committés.
`.gitignore` couvre `.env`, `*.txt`, `projects/`, `build/`, `source/`.
