# Notes
2026-07-18: Responsive mobile complet — preprod v1.4.30 (commit 188a769).
- **Constat clé** : le site s'affichait "comme sur PC" sur le Pixel de
  l'utilisateur alors que la balise viewport est servie partout (vérifié par
  curl sur prod ET sur Authentik). Cause côté téléphone : mode « Version pour
  ordinateur » de Chrome Android. Rien à corriger côté serveur pour ça.
- app.css : bloc mobile global ≤768px — header (brand 21px, .icon-btn 46px,
  avatar 46px, items menu 45px+), `input/select/textarea` 16px (anti-zoom iOS),
  header `flex-wrap` (repli 2 lignes ≤360px), `#lang` icône seule ≤480px.
  `@media(hover:none)` : `.gear` des cartes toujours visible.
- **Fusion Outil+Importer dans le menu avatar, mobile uniquement** :
  `social.js` initHeader injecte 2 `.um-item.um-mobile` avant le séparateur
  (cachés desktop par app.css, le header cache ses boutons en ≤768px).
  Textes via data-hk → refreshHeaderTexts (rafraîchi au changement de langue).
  `applyViewToHeader` (vitrine) applique les droits simulés aux .um-mobile.
- vitrine : grille **2 colonnes** ≤700px, recherche 48px plein écran au-dessus
  du tri, chips filtres 42px, panneau filtres empilé, bouton téléchargement
  des cartes en 2 lignes autorisées. Sous-menu « Voir en tant que » en
  accordéon (position:static) — il débordait à gauche de l'écran.
- album_detail : pochette centrée max 330px, soc-btn 40px, dl 40px,
  flèches carrousel toujours visibles en `hover:none` (invisibles au tactile
  sinon !), idem overlay « Proposer une pochette ».
- Vérifié E2E via webshot (nouvelles options `--mobile` et `--click`) en
  412×915 et 360×800 : vitrine visiteur/gestionnaire, menu, viewas, filtres,
  album, profil, thème clair. Piège corrigé dans measure.mjs : les en-têtes
  --as étaient envoyés aux domaines Google Fonts → CORS bloquait les polices.
- **Git remis d'équerre** : le clone CT102 poussait encore `preprod` sur le
  repo public (ancien schéma) — branche supprimée du public, remote
  `preprod-origin` (naerod/preprod-live2mp3, privé) ajouté, tracking corrigé
  (voir DEPLOY.md). WIP orphelin des sessions du matin sur CT110 (pochette
  vinyle générique + suppression cover différée) commité en d286048.
- v1.4.31 : `.album .body{flex:1}` — rangées sociales + téléchargement
  épinglées en bas des cartes, alignées entre cartes d'une même rangée quel
  que soit le nombre de lignes de texte (retour utilisateur après validation
  mobile réussie sur son Pixel).
- Validation utilisateur OK sur son Pixel → **prod v1.5.0 promue** (6dc94fe,
  tag v1.5.0). Le script deploy-prod a dû être réécrit : il mergait
  `origin/preprod` qui n'existe plus sur le repo public → merge depuis
  `preprod-origin` (repo privé), exclusion de `claude/` du repo public
  (modify/delete attendu), résolution auto du conflit `VERSION` (systématique :
  bump patch preprod vs minor prod), garde-fou qui stoppe sur tout autre
  conflit. Conflit one-shot vitrine.html (fixes du matin commités en double
  main/preprod) résolu en faveur preprod (sur-ensemble vérifié par diff,
  précédent 206f50b). Backup ancien script : /opt/apps/*.bak.20260718 +
  workspace/backups/.

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

2026-07-15: Profil — ville + artiste favori (preprod cbf4baf).
- **Formalisme garanti par construction** : le client envoie l'**id** de
  l'entrée choisie (code INSEE / id Deezer), jamais un libellé ; le serveur
  reconstruit le texte depuis la source. Taper « dijon » sans choisir ne vaut
  rien (le champ se restaure au blur).
- Sources publiques sans clé : `geo.api.gouv.fr` (villes) + `api.deezer.com`
  (artistes, triés par popularité). Cache TTL mémoire, timeout 4 s.
- Paris/Lyon/Marseille : code générique 75000/69000/13000 (les codes de la
  source sont ceux des arrondissements). Seul cas particulier, liste fermée.
- Artistes dédoublonnés par nom (le plus suivi gagne) : Deezer laisse
  coexister le vrai « Coldplay » et un squatteur vide du même nom.
- Résolution seulement si l'id change → éditer sa bio ne dépend d'aucune API.
  Source en panne : suggestions vides, 503 sur un changement invérifiable.
- `L2M.autocomplete()` réutilisable (debounce 250 ms, clavier, ARIA combobox).
- ⚠️ **Villes = France uniquement** (geo.api.gouv.fr). Le format « Dijon, 21000 »
  est intrinsèquement français ; ouvrir à l'international demanderait une autre
  source (Nominatim/Google Places) et un autre formalisme.
- 118 tests verts, vérifié E2E sur preprod. Aucune étape nginx pour la prod
  (routes sous `/api/social/`, déjà couvertes).

2026-07-16: Réparation post-refactor header + release v1.4.0 en prod.
- L'unification du header (a23b256) avait cassé vitrine / fiche album / profil
  (code legacy thème/langue sur des éléments supprimés) et le menu « Voir en
  tant que… » n'était plus monté. Voir
  workspace/debugging/2026-07-16_preprod-cassee-refactor-header-a-moitie-committe.md
- Fixes : 32d698a (3 pages + injectViewAsMenu + thème dans <head>),
  e77d103 (tri profil 10px/10px réels, fusion de marges neutralisée),
  eb3c58f (.hidden!important — /app restait verrouillé pour l'admin),
  21ac841 (tests alignés sur 26edc0c/4bb250b) → 118/118 verts.
- E2E : proxy local injectant X-authentik-username/groups (4 rôles) via tunnel
  SSH vers le conteneur + Chromium headless/CDP. 16 combinaisons page×rôle OK.
- Release v1.4.0 (d16cdb2) : header unifié, menu Voir-en-tant-que 4 vues,
  tri Publications profil, covers PNG/PDF imprimable, gating /app client.
- Bugs connus relevés en review (non corrigés, en attente d'arbitrage) :
  « ▲ undefined » onglet Commentaires du profil (profile.js:71, c.score
  n'existe plus depuis les likes) ; « Trier par » et boutons header
  Outil/Importer non retraduits au changement de langue ; redondance
  « Importé par nathan 🅝 nathan » sur les fiches ; badge version absent hors
  vitrine ; profil inconnu répond HTTP 200.
