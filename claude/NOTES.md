# Notes
2026-07-02: PLAN.md rédigé, en attente de validation utilisateur avant Lot 1.

2026-07-10: Système social (profils / favoris / commentaires) développé et
déployé en **preprod** (commit 254200b, v1.1.0).
- Backend : `backend/db.py` (SQLite) + `backend/social.py` (router `/api/social/*`,
  pages `/u/{username}` et `/avatar/{username}`). Auth via forward-auth Authentik.
- Front : `social.js` (widgets like + commentaires), `profile.html`/`profile.js`,
  intégration modale vitrine + lien profil header. i18n FR/EN + thème.
- DB : `projects/.l2m-social/<APP_ENV>/live2mp3.db` (pas de modif compose).
- Tests : `tests/test_social.py` (8) — suite complète 68 verts.
- Vérifié bout-en-bout sur preprod (like, commentaire, réponse, vote, profil,
  avatar). **Reste** : validation visuelle preprod par l'utilisateur, puis
  promotion prod avec l'étape nginx `/api/social/` (voir DEPLOY.md).

2026-07-11: Refonte UI + données (preprod commit 6aef939).
- Header : menu avatar déroulant (profil/langue/thème/déconnexion) ; avatar
  pastel + initiale pseudo par défaut ; bouton Retour sur page profil ;
  avatars posteurs sur cartes/fiches ; fiche album refondue (artiste>titre>
  date 📅 > lieu 📍). Endpoint groupé /api/social/profiles.
- imported_by corrigés (volume PARTAGÉ prod+preprod) : nathan 12, louis 2, naerod 4.
- 2 compilations importées (naerod, 17/07/2025) : live-crossovers (5),
  halftime-shows (4). Pochette IA par piste (APIC) + cover album. Catalogue = 19.
- ⚠️ Ghost Stories Live 2014 (coldplay-2014, owner louis) SANS MP3 → hors catalogue.
- ⚠️ TOP : seulement 2 albums en base (pas 3), mis sur nathan ; dates d'import
  non lisibles sur captures → inchangées.
- Prod v1.2.0 déployée (commit b1f3e1f) + bloc nginx soft-auth /api/social/ ajouté. Terminé.
- coldplay-2014 (Ghost Stories Live 2014, owner louis) retiré du volume : fichier MP4 uniquement,
  en attente feature support MP4 (voir BACKLOG). Idem Breach Digital Remains = PDF, hors scope.

2026-07-15: Pochettes multiples créditées (backend/covers.py).
- Un album porte une **collection** de pochettes, chacune créditée à son auteur.
  Tout utilisateur connecté peut proposer (pas seulement les gestionnaires).
- **Gagnante résolue à la lecture** (`rank_covers`) : épinglée > plus likée >
  plus ancienne. Jamais stockée : un like ne réécrit rien, il reclasse.
  Épinglage réservé aux gestionnaires, 1 max/album (index partiel SQLite).
- **Tray card = colonne de la cover** (`traycard_ext`), pas une entité. Une tray
  card orpheline est donc impossible, et supprimer une cover emporte la sienne.
- Commentaires : colonne `cover_id` sur la table `comments` existante (NULL =
  fil d'album). Réutilise replies/likes/édition/modération sans duplication.
- **`file_key` décorrélée de l'`id`** : prod et preprod partagent le volume
  albums mais ont chacune leur base — deux `id` autoincrémentés indépendants
  désigneraient le même fichier. Clé = `legacy` (migration) ou uuid (upload).
- APIC MP3 **paresseuse** : recalculée au build du ZIP, pas à chaque like (sinon
  20 fichiers réécrits par clic). `_write_album_cover` est no-op si l'image
  embarquée est déjà la bonne, sans quoi le cache du ZIP boucle.
- ZIP : `artwork/<rang>-<auteur>_cover.ext` / `_traycard.ext` → le tri
  alphabétique reproduit l'ordre de popularité et colle chaque paire.
  Cache invalidé par une **signature du classement** gravée dans le commentaire
  du zip : un reclassement renomme les entrées sans toucher aucun mtime.
- Migration : `python -m backend.migrate_covers [--dry-run]`, idempotente,
  **copie** (ne déplace pas) `artwork/cover.*` + `tray_card.pdf` sous la clé
  `legacy`, créditée à `meta.imported_by`. À jouer une fois par base.
- Front : carrousel (flèches/points/clavier) + crédit avatar→profil + modale par
  cover (likes, commentaires, tray card, import). Tests : `tests/test_covers.py`.
- **Le carrousel défile `SLIDES`, pas `COVERS`** : chaque pochette occupe une
  face, suivie de sa tray card quand elle en a une (`buildSlides`). Cohérent
  avec le modèle backend — la tray card est une *colonne* de sa cover, jamais
  une entrée autonome : elle ne peut donc pas apparaître sans sa pochette.
  Le badge « Tray card » n'étiquette plus une présence, il **bascule** vers la
  face. Puces creuses (`.cr-dot.tray`) pour distinguer une face tray de sa cover.
- **Recalage par paire `id`+`kind`, jamais par index** (`focusSlide`) : un like
  ou un épinglage fait renvoyer par le serveur une liste **reclassée**, un index
  n'y survit pas. Vaut pour like / pin / delete / upload.
- Face tray card en `contain` (format libre) et non `cover` : une tray card est
  large, la rogner au carré la décapite. **Seule tray card du catalogue = PDF**
  (twenty-one-pilots-2026-04-03) → rendue en `<iframe>`. L'iframe capte le clic,
  donc une face PDF n'ouvre pas la modale (flèches/puces/badge restent le chemin).
- `traycard_ext` vaut **`''`, pas `NULL`**, quand il n'y a pas de tray card :
  filtrer sur la vérité (`bool`) ou `COALESCE(...) != ''`, pas sur `IS NOT NULL`.
- `/traycard-img/{id}` sert le PDF **sans `Content-Disposition`** → inline dans
  l'iframe. Ne pas viser `/download/{slug}/traycard` (attachment) : c'est le bug
  du 2026-07-15 (« Enregistrer sous » au lieu de l'aperçu).
- **Prod : aucune étape nginx** (contrairement au social) — `^~ /api/social/`
  couvre les routes covers, `~ ^/(app|api/jobs|api/albums|download)(/|$)` couvre
  `/download/cover|traycard/{id}`, `/cover-img|traycard-img/{id}` sont publics.

## Modèle commentaires
Aplati à 1 niveau : racine (`parent_id` NULL) + réponses rattachées à la racine
avec mention `@auteur` ; « voir plus » pour dérouler. Votes ▲/▼, tri Top/Récents,
édition + suppression douce (auteur ou modérateur).
