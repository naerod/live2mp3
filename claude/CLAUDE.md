# live2mp3 — règles techniques
Voir PLAN.md et le brief source: S:\Dorian\PROJETS\live2mp3\brief-claude-code-live2mp3.md (CT101:/home/dorian/partage/Dorian/PROJETS/live2mp3/). Stack: FastAPI + RQ/Redis + Peaks.js + Docker (CT110, Tailscale only). Manifest = source de vérité. Secrets dans .env local (chmod 600), jamais committés.

## `Manifest.save()` — la règle du `touch`

`save()` horodate `meta.updated_at` par défaut, et ce champ **pilote un badge
public** (« Mis à jour » sur la vitrine, 30 jours). Donc :

> `touch=True` **uniquement** si un humain vient de modifier le contenu de
> l'album (titres, découpage, métadonnées, pochette posée à la main).
> Tout le reste — migration, backfill, repointage automatique, changement de
> visibilité (publication, promotion prod), rattrapage technique — s'écrit avec
> **`touch=False`**.

Corollaire, appris à la dure le 2026-09-21 : **une date métier ne se dérive
jamais du mtime d'un fichier.** Le mtime reflète la dernière écriture
technique, pas une action utilisateur — et toute migration qui tourne au
démarrage le réécrit. Un backfill doit être idempotent **et marqué**
(`meta.backfilled`), sans quoi il se ré-exécute indéfiniment et fait dériver
les dates de tout le catalogue.
Fiche : `workspace/debugging/2026-09-21_badges-nouveau-maj-derives-du-mtime.md`.
