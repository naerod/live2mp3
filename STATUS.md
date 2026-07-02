# STATUS — live2mp3

Avancement réel (preuve = sortie `make verify`).

## `make verify` : 47 passed, 0 failed (24 s)

| Lot | Description | Etat | Tests |
|-----|-------------|------|-------|
| 1 | Manifest + render ffmpeg + tags mutagen | OK | T1 (9), T2 (5), T3 (4) |
| 2 | Visuels pochette (HTML -> Chromium -> PDF) | OK | T4 (5) |
| 3 | Assist IA (Whisper + DeepSeek + Peaks.js) | OK | T5 (4), T6 (6, dont appel reel DeepSeek) |
| 4 | Images disque (data_disc + audio_cd) | OK | T7 (3), T8 (4) |
| 5 | Integration (API SSE, formulaire, bundle) | OK | T9 (3), T10 (3) |

## Mapping T1-T10

| Test | Fichier | Verifie |
|------|---------|---------|
| T1 | test_t1_manifest.py | load/save/validation, slugify, clean_filename, regle locked |
| T2 | test_t2_render.py | durees MP3/MP4 = manifest, idempotence, etat |
| T3 | test_t3_tags.py | ID3 complets, APIC embarque, metadonnees MP4, fallback sans cover |
| T4 | test_t4_artwork.py | PDF valides, 120x120 mm / A4 exacts, fallback typo |
| T5 | test_t5_preanalyze.py | waveform.dat format binaire v2, silencedetect, choix modele GPU |
| T6 | test_t6_llm.py | parsing JSON (plain/fence/prose), respect locked, APPEL REEL DeepSeek |
| T7 | test_t7_data_disc.py | ISO generee + isoinfo liste >=4 MP3 |
| T8 | test_t8_audio_cd.py | WAV Red Book 44.1k/16/stereo, CUE TOC, offsets gap, duree totale |
| T9 | test_t9_api.py | POST job -> render pipeline -> etats SSE complets, verrou markers |
| T10 | test_t10_bundle.py | ZIP complet (mp3+mp4+pdf+iso+manifest / cue+wav) |

## Points d'arret (geres en autonomie sur demande explicite)

- A templates : versions fonctionnelles conformes au theme creees (DECISIONS D5), a ecraser par les definitives.
- B disque : les deux cibles implementees et testees ; defaut UI data_disc.
- C GPU Whisper : detection auto nvidia-smi -> large-v3 / fallback CPU medium.
- D URL source : aucun telechargement effectue ; pipeline teste sur fixtures synthetiques.

## Hors perimetre V1

- dvd_video : placeholder signale (UI + DvdVideoNotSupported).
- Deploiement Docker reel sur CT110 : non execute ici (docker absent de CT102) ; artefacts prets (Dockerfile, compose, Makefile export/import).
