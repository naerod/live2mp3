2026-08-03 (suite) : **Ajout d'une piste à un album existant depuis un lien**
(preprod v1.20.4). Les deux parcours de création produisaient un album entier ;
la compilation enrichie au fil de l'eau (« Live Crossovers ») n'était couverte
par rien.
- Backend `backend/addtrack.py` : `POST /api/tool/probe-track` (sonde seule, ni
  LLM ni setlist.fm — un morceau unique ne les justifie pas) puis
  `POST /api/albums/{slug}/tracks/from-url` (téléchargement, rognage, encodage
  `libmp3lame -q:a 0`, tags, miniature en pochette de piste, refresh Jellyfin).
  Exécution en thread du process API + suivi par sondage (`GET .../{token}`) :
  yt-dlp dépasse la minute, une requête synchrone se ferait couper par
  Cloudflare (100 s).
- **Modèle** : la piste porte sa propre source (`track.source` : url, rognage,
  auteur, date) au lieu de dépendre du master de l'album. D'où deux garde-fous,
  sans lesquels la fonctionnalité détruisait du travail existant :
  (1) `render._expected_filenames` ignorait les pistes sans timecodes → la purge
  des orphelins aurait effacé le MP3 de la piste externe **à chaque re-rendu** ;
  (2) `PUT /jobs/{slug}/setlist` remplace la liste ENTIÈRE par ce que l'éditeur
  a produit — or l'éditeur ne connaît que le master : `preserve_external_tracks`
  les recolle en fin de liste (avec renumérotation des pochettes de piste, index
  unique (slug, track_n) → passage par des numéros négatifs).
- Un ajout en échec retire son entrée du manifeste (pas de piste fantôme, qui
  décalerait la numérotation ID3 de toutes les suivantes).
- Front : panneau « Ajouter une piste depuis un lien » sur `/app/album/{slug}`
  (destination sans ambiguïté, contrairement à l'outil de création qui aurait
  demandé un sélecteur d'album). i18n FR/EN, thèmes clair/sombre vérifiés au
  rendu. Repère 🔗 sur les pistes venues d'un lien — a nécessité d'exposer
  `source` dans `GET /api/albums/{slug}` (l'API ne renvoyait que n/titre/artiste).
- ⚠️ **CSS** : `label{flex-direction:column}` est global dans app.css — toute
  case à cocher en ligne doit redéclarer `flex-direction:row`.
- Tests : `tests/test_addtrack.py` (13 cas). Suite complète 243 passés,
  1 échec **préexistant** (`test_t5_preanalyze::test_waveform_dat_format`,
  vérifié identique sur la preprod non modifiée).
- Validé de bout en bout sur un album jetable en preprod avec la vraie vidéo
  Coldplay/Ed Sheeran : MP3 de 307,2 s (= durée exacte de la source), tags et
  pochette de piste corrects. Album de test supprimé depuis.
- ⚠️ **`live-crossovers` n'a pas été touché** : `PROJECTS_DIR` est le **même**
  chemin en prod et en preprod (`/opt/data/live2mp3/projects`) — y ajouter une
  piste depuis la preprod modifie l'album public. En attente d'autorisation.


2026-08-03 (suite) : **L'éditeur n'enregistrait rien du tout** (preprod
v1.15.38). Session complète de découpage perdue sur `linkin-park-2025-11-16`
(32 pistes au dixième). Deuxième perte en deux jours ; le correctif du 08-02
(`n` manquant) n'avait traité qu'une cause sur quatre. Voir
`workspace/debugging/2026-08-03_live2mp3-editeur-brouillon-jamais-enregistre.md`.
- **Trois défauts cumulés** : (1) le garde-fou de `saveDraft` abandonnait **en
  silence** dès qu'une piste était sans titre — blocage portant sur la setlist
  ENTIÈRE ; (2) une piste ajoutée naît `title:""`, créant donc aussitôt cet
  état ; (3) saisir le titre ne planifiait aucun enregistrement (titre/artiste
  ne passent pas par `commitEdit()`, qui reconstruit les lignes et ferait perdre
  le focus) → nommer la piste ne débloquait rien ; (4) aucune purge du débounce
  de 3 s au départ de la page.
- Correctifs : saisie titre/artiste → `scheduleDraftSave()` ; état `blocked`
  visible (`cloud_alert` + cause nommée, FR/EN) ; `flushDraftSave()` sur
  `pagehide`/`visibilitychange` avec `keepalive:true` (sinon le navigateur
  annule la requête en vol pendant la navigation).
- ⚠️ **Piège de reproduction** : `commitEdit()` **trie** `EDIT` par `start` — une
  piste ajoutée lecteur à 0 s remonte en 1re position. Viser « la dernière ligne
  du DOM » écrase le titre d'une autre piste et fait croire que le correctif ne
  marche pas. Cibler la ligne au titre vide.
- Vérifié par reproduction Chromium avant/après (0 PUT → PUT 200 à chaque étape).
- **Données remédiées** : les 32 timecodes de l'utilisateur réinjectés depuis ses
  captures via `PUT /setlist`, chaînage fin→début vérifié sur les 32 pistes,
  2 h 02 min 43,74 s. Album toujours non publié.
- **v1.15.39 — enregistrement à chaque modification** (demande utilisateur) :
  action ponctuelle (timecode, cadenas, ajout, suppression, marqueur glissé) →
  envoi **immédiat** via `commitEdit()`. Exception : la frappe (titre/artiste)
  émet un évènement par lettre → 700 ms, sinon le manifest serait réécrit des
  dizaines de fois pour un seul mot, sur un stockage partagé avec la prod.
  ⚠️ **Verrou `draftInFlight`/`draftDirty`** indispensable : le serveur remplace
  la setlist ENTIÈRE à chaque appel, deux écritures qui se croisent ne se
  fondent pas. Mesuré : 3 modifs = 3 envois ; 18 caractères tapés = 1 envoi.
- `tools/webshot/measure.mjs` : nouvelle option `--lang` (la langue vit dans
  `localStorage`, elle ne peut pas être posée après chargement comme le thème ;
  le sélecteur est dans le menu avatar, cliquable en deux temps et fragile).

2026-08-03 : **PROD v1.20.0** — promotion de toute la ligne preprod v1.15.26 →
v1.15.41 (37 commits, 2823 lignes). Contenu : découplage du rendu MP4, rendu
dans un `.part`, avancement par étape, correctifs d'enregistrement de l'éditeur,
portée d'environnement (`origin_env`), MP4 unique par concert, revue de code.
- Prérequis vérifiés avant promotion (tous présents dans le `.env` prod) :
  `JELLYFIN_API_KEY`, `SETLISTFM_API_KEY`, `AUTHENTIK_URL`,
  `AUTHENTIK_API_TOKEN`, `DEEPSEEK_API_KEY`, `PROJECTS_DIR`.
  `ENV_SUFFIX` vide est **normal** en prod (conteneurs `live2mp3-app`) : le
  compose l'interpole sans `:?`.
- **Aucune étape nginx** : le découplage n'ajoute aucune route
  (`/api/render-queue`, `/api/drafts`, `/app/drafts` existaient déjà).
- Après déploiement : 24 albums toujours visibles (le filtre `hidden_by_env`
  ignore les manifests sans `origin_env`, donc aucun album historique masqué),
  vitrine/preprod/dorianjulien/naerod/hub tous en 200.
- ⚠️ **Fausse alerte de ma part** : `linkin-park-2025-11-16` apparaissait en prod
  avec `origin_env` absent et `published: true` — j'y ai vu une exposition
  accidentelle et j'ai rétabli `published: false` + `origin_env: preprod`.
  C'était en réalité l'utilisateur qui venait de publier **et** de promouvoir
  l'album (`PATCH /published` puis `POST /promote` dans les logs preprod).
  État remis comme il l'avait laissé. **Leçon** : avant de « corriger » un état
  surprenant sur des données partagées, lire les journaux d'accès — une écriture
  légitime concurrente est plus probable qu'un bug, et l'annuler est destructeur.

2026-08-03 (suite 2) : **Avancement par étape + rendu dans un `.part`**
(preprod v1.15.41).
- **v1.15.40** — le job audio ne publiait aucun avancement (seul le job vidéo le
  faisait) : bande indéterminée sans nom d'étape. `renderqueue.set_step/get_step`
  (`{stage,index,total,pct}`) exposé par `/api/render-queue` → « Étape 2/4 ·
  Métadonnées MP3 » + barre + %. Doublon assumé du SSE : la page interroge
  toutes les 5 s sans flux ouvert. **Étapes pondérées** (`render` 0,88 ; les
  3 autres 0,04) — à poids égaux la barre resterait sous 25 % pendant tout le
  rendu réel puis sauterait à 100 %. Corps de ligne cliquable → `/app#slug`
  (pause/arrêt volontairement hors zone cliquable).
- **v1.15.41 — ⚠️ incident : déployer pendant un encodage laissait un fichier
  tronqué sous son NOM DÉFINITIF.** Le déploiement de la v1.15.40 a recréé le
  conteneur du worker en plein encodage : MP4 tronqué de 744 Mo + job fantôme
  figé à 20,7 %. Le stage étant idempotent par nom, une relance l'aurait sauté
  et servi comme vidéo finale. `_render_or_cleanup` nettoyait sur `Cancelled`
  mais un SIGKILL n'en laisse pas l'occasion. → encodage dans `<nom>.mp3.part` /
  `<nom>.mp4.part`, promu par `os.replace` à la réussite.
  - ⚠️ Suffixe en **fin de nom**, pas l'extension : `Path.glob("*.mp4")` **voit
    les fichiers cachés** (vérifié) — un `.x.part.mp4` aurait compté comme piste
    rendue dans `catalogue._has_files` et sorti l'album des brouillons.
  - ⚠️ ffmpeg déduit le conteneur de l'extension → `-f mp3` / `-f mp4` imposés.
  - **Règle d'exploitation** : vérifier `/api/render-queue` avant tout
    déploiement. Le `.part` évite la corruption, pas la perte du travail en
    cours ; le job reste fantôme dans RQ jusqu'à son `job_timeout` (6 h) et doit
    être purgé/relancé à la main.
- 231 tests verts (+6).

2026-08-03 : **Rendu MP4 découplé du rendu MP3** (preprod v1.15.37).
Les 28 MP3 d'un concert sortent en quelques minutes, le MP4 demande 15 à 20 min
de x264 : dans un seul job RQ, l'album audio n'était livré à personne avant la
fin de la vidéo. Voir
`workspace/debugging/2026-08-03_live2mp3-rendu-mp4-bloquant-et-faux-positif-session.md`.
- **Deux natures de job** dans `renderqueue` : `render` (audio + tags + pochettes
  + disque) et `video`. Identifiants RQ distincts et **clés d'état Redis
  indexées par nature** (`l2m:{cancel,pause,job,pct}:<kind>:<slug>`) — sans quoi
  le `finally` du job audio effaçait les métadonnées du job vidéo qu'il venait
  d'enfiler. `enqueue_video` contourne volontairement le garde-fou anti-doublon :
  le job audio est encore `started` quand il enfile sa suite.
- Rendus audio **enfilés en tête** (`at_front`) : worker unique, un MP4 en
  attente ne doit pas faire patienter l'audio d'un import suivant.
  ⚠️ **Limite** : un MP4 *déjà en cours* retarde encore l'audio (pause possible
  depuis la file). Correctif propre = 2e worker sur une file `video`, donc modif
  de `docker-compose.yml` (patché localement sur CT110, cf. DEPLOY.md).
- **Publication toujours manuelle** (arbitrage utilisateur) : stockage partagé
  prod↔preprod, publier automatiquement rendrait public un album jamais relu et
  le pousserait dans Finamp sous 10 min.
- **Progression MP4 réelle** : `ffmpeg -progress pipe:1`, thread lecteur,
  `out_time_ms` — ⚠️ en **microsecondes** malgré son nom. Publiée sur le pub/sub
  *et* dans une clé `l2m:pct:` que lit `/api/render-queue` (la page brouillons
  interroge sans flux ouvert).
- ⚠️ **`render.run(video=False)` ne purge jamais `build/video`** : la phase 1
  tourne ainsi et détruirait sinon le MP4 déjà encodé, sur le volume de prod.
  Verrouillé par `test_audio_only_render_preserves_existing_mp4`.
- `catalogue.list_drafts()` **inchangé** : le sens de « brouillon » (import
  interrompu) est préservé, la phase 2 est portée par la file de rendu. La page
  `/app/drafts` a deux sections : « Rendus en cours » (toujours présente, même
  vide) puis « Brouillons ».
- Notif `video_done` distincte de `render_done` ; entrée silencieuse « rendu
  vidéo en cours » avec %. i18n FR/EN complet — la rubrique file d'attente de la
  page brouillons n'avait **jamais** été traduite.
- **Faux positif « Session expirée » corrigé** : `sessionAlive()` concluait sur
  `authenticated`, faux dès que l'outpost preprod n'envoie pas les groupes
  (défaut connu depuis le 2026-07-20). Il porte désormais sur l'identité, avec
  2 échecs consécutifs avant d'alarmer.
- **Bug latent** : `renderToast()` (social.js) référençait `T[LANG()]` au lieu de
  `TXT` → `ReferenceError`, le toast de fin de rendu ne s'était jamais affiché.
- Vérifié E2E réel sur preprod (audio prêt à 3 s, vidéo séparée finie à 66 s,
  progression 5,3 → 97,5 % monotone, MP4 intact après un rendu audio forcé) +
  contrôles Chromium (brouillons sombre/clair/EN, écran final, notification).
  Projet de test entièrement purgé. 225 tests verts (+13).

2026-07-31 : **Propagation pochette album → Jellyfin/Finamp** (prod v1.19.0,
preprod v1.15.25). Incident : covers mises à jour sur albums publiés
n'apparaissaient pas dans Finamp même en forçant la sync.
- Cause : `_on_covers_changed` ne mettait à jour que le manifest ; `sync-media.sh`
  ne re-scanne Jellyfin que sur apparition/disparition de symlink, jamais sur un
  changement de contenu. Jellyfin servait l'ancienne image en cache → Finamp aussi.
- Fix code : `_on_covers_changed` → `_propagate_cover_to_media` quand la gagnante
  change réellement ET album publié :
  - pochette unique → ré-embarque `album.cover` (APIC) dans toutes les pistes ;
  - `per_track_covers` → dépose `build/audio/cover.jpg` (vignette album) SANS
    toucher aux APIC des pistes ;
  puis `jellyfin.refresh_album(slug)` = refresh **par item** avec
  `ReplaceAllImages=true`+`FullRefresh` (un `Library/Refresh` ordinaire ne
  ré-extrait PAS l'art déjà en cache — point clé).
- **`JELLYFIN_API_KEY` désormais injectée dans le `.env` PROD** (était vide → le
  refresh auto était mort en prod). Le « reste à faire » du 2026-07-25 est levé.
- Remédiation manuelle prod (avant le fix) sur les 4 albums touchés : halftime,
  twenty-one-pilots, falling-in-reverse (nouvel album), live-crossovers. Ce
  dernier est **per-track** : build pas ré-rendu → mapping piste→pochette
  reconstruit via le cache d'images Jellyfin (match perceptuel), pochettes
  individuelles ré-embarquées, cover.jpg dossier ajouté pour la vignette.
- ⚠️ Finamp garde un cache d'images CLIENT qui ne s'invalide pas seul : vider le
  cache d'images de l'app pour voir les nouvelles pochettes.
- 3 tests ajoutés (propagation album / folder-cover per-track / no-op brouillon).
# Notes

2026-07-25 : **Réouverture de l'éditeur audio sur un album déjà publié**
(preprod v1.15.4). Mission : les erreurs de découpage remontées par les
utilisateurs Finamp ne pouvaient être corrigées qu'en repassant par le flow
de création complet — impossible de rouvrir l'éditeur Peaks.js sur un album
déjà publié depuis la fiche de gestion.
- **Limite de portée découverte à l'investigation** : seuls les albums créés
  via l'**outil lien** (depuis v1.6.0) conservent `source/master.wav` — les
  imports manuels (majorité du catalogue historique : Coldplay, Indochine,
  Linkin Park, TØP, U2 2006…) n'ont aucun dossier `source/`. L'éditeur ne peut
  donc rouvrir que les concerts importés via l'outil lien avec master encore
  présent sur disque. Nouveau champ `has_editor_source` sur
  `GET /api/albums/{slug}` (vérifie `source/preview.mp3` ou `source/master.wav`).
- **Bouton « Ouvrir l'éditeur audio »** en haut à gauche de la section Pistes
  (`album.html`), visible gestionnaire, désactivé + info-bulle explicative si
  `has_editor_source` est faux. Ouvre `/app#{slug}` : réutilise **à l'identique**
  la logique de reprise de session déjà présente dans `app.js` (resume IIFE,
  `pipeline_state.download/ai_markers === "done"` → `openEditor()` direct) —
  aucun nouvel endpoint créé, `/api/jobs/{slug}/setlist|markers|render` chargent
  et re-sauvegardent le manifest complet donc ne touchent jamais `published`.
- **Bug critique corrigé dans le pipeline de rendu** (jamais exercé jusqu'ici,
  le flow de création ne rend chaque piste qu'une fois dans un `build/` vide) :
  `render.run()` est idempotent par nom de fichier (`skip si le fichier existe
  déjà`). Sur un album déjà rendu, éditer un timecode sans changer le titre ne
  produisait donc **aucun nouveau rendu** (silencieux), et une piste renommée/
  supprimée laissait un fichier orphelin (visible dans le ZIP et dans Jellyfin).
  Fix : `render.run()` purge désormais les fichiers `build/audio`/`build/video`
  qui ne correspondent plus à aucune piste courante ; `start_render` force un
  re-rendu complet (`force=True`) quand l'album est déjà publié au moment de
  l'appel (= forcément une ré-édition, jamais le flow de création où
  `published` est encore `False` à ce stade).
- **Rafraîchissement Jellyfin** (`backend/jellyfin.py`, nouveau) : le montage
  Jellyfin de CT110 (`sync-media.sh`, cron 10 min) ne détecte que l'apparition/
  disparition d'un symlink d'album (publication/dépublication), jamais un
  changement de contenu à l'intérieur d'un dossier déjà monté. Après un
  re-rendu sur un album publié, appel best-effort à
  `POST http://jellyfin:8096/Library/Refresh` (réseau Docker `apps_web`
  partagé avec live2mp3-app/-preprod-app). Clé `JELLYFIN_API_KEY` = même clé
  que `sync-media.sh` (`/opt/apps/jellyfin/apikey` sur CT110), injectée dans
  le `.env` preprod (pas encore dans le `.env` prod — **à faire avant toute
  promotion**, sinon dégradation silencieuse : pas de refresh Jellyfin après
  re-rendu en prod).
- **Wording adapté côté éditeur pour une ré-édition** (`reedit` en JS, déduit
  de `manifest.published` à l'ouverture) : bouton « Enregistrer et republier »
  au lieu de « Valider et créer l'album », écran final sans la mention
  « créé en brouillon » (fausse pour un album déjà public). Implémenté via
  l'attribut `data-i18n` (pas juste `textContent`) pour survivre à un
  changement de langue en cours de session.
- **Vérifié bout-en-bout sur preprod réel** (pas seulement en tests unitaires) :
  script Python exécuté dans le conteneur `live2mp3-preprod-app` (fixture
  ffmpeg synthétique, comme les tests) — création, publication, reprise via
  manifest (état identique à un vrai album créé), édition (fusion 2 pistes →
  1, nom de fichier inchangé), re-rendu confirmant purge de l'orpheline +
  réencodage forcé (mtime), `published` intact, `Library/Refresh` Jellyfin
  réellement déclenché (2 scans Jellyfin observés dans les logs du conteneur
  aux horodatages du test). Contrôle visuel Chromium headless (tunnel SSH +
  proxy Python injectant les en-têtes gestionnaire, cf.
  [[reference_live2mp3_e2e_roles]]) : bouton bien positionné à gauche de
  « Pistes », désactivé/activé selon `has_editor_source`, éditeur Peaks.js
  rouvert avec la piste existante, bouton « Enregistrer et republier » affiché
  une fois l'album publié. Projet de test entièrement supprimé après coup
  (fichiers + dépublication + `DELETE /api/jobs/{slug}`).
- 10 tests ajoutés (purge/force render, endpoint API bout-en-bout avec mock
  Jellyfin, `has_editor_source`). 199 tests verts au total.
- **Reste à faire avant promotion prod** : ajouter `JELLYFIN_API_KEY` au `.env`
  prod sur CT110 (même clé, cf. ci-dessus) ; valider une fois de plus sur un
  vrai album utilisateur (pas seulement la fixture synthétique) si possible.

2026-07-21 (suite 2) : **Rôle admin applicatif `live2mp3-admin`** (preprod v1.8.33).
- Le rôle « Administrateur » du site n'est plus le superuser global Authentik.
  Nouveau groupe **`live2mp3-admin`** (pk `18fa33c5-…`, non-superuser) =
  gestionnaire + gestion des rôles, sans droits sur le reste de l'infra SSO.
- `authentik.set_role` : admin = {live2mp3-user, live2mp3-gestionnaire,
  live2mp3-admin} — **jamais** `authentik Admins`. Vérifié E2E sur test-user.
- Hiérarchie : superuser global (`authentik Admins`, ex: naerod) ⊃ admin appli
  (`live2mp3-admin`) ⊃ gestionnaire ⊃ user. `_role_of` : super OU app-admin →
  "admin". `is_superadmin` exposé dans /me & /api/social/me.
- Garde-fous endpoint : créer/retirer un admin applicatif = **réservé au
  superadmin** ; un superuser global n'est **pas modifiable** depuis le site.
  Sélecteur front : 3 rôles pour un superadmin, 2 (user/gestionnaire) pour un
  admin applicatif ; badge non cliquable sur un admin si on n'est pas superadmin.
- 190 tests (+1 test app-admin vs superadmin).

2026-07-21 (suite) : **Gestion du rôle via le badge (sélecteur 3 rôles)**
(preprod v1.8.32).
- Le badge de rôle du profil est cliquable pour les admins (chevron) → modale
  sélecteur des 3 rôles (Utilisateur/Gestionnaire/Administrateur), l'actuel
  marqué « Actuel ». Choix → confirmation « êtes-vous sûr… ». Bouton séparé
  supprimé.
- Backend généralisé : `POST /api/social/users/{u}/role {role}` (remplace
  `/gestionnaire {grant}`). `authentik.set_role` réconcilie l'appartenance aux
  groupes `live2mp3-user` / `live2mp3-gestionnaire` / `authentik Admins` (les
  autres groupes de l'user sont préservés — vérifié : catchr-users/_preprod
  intacts). Notif uniquement si le rôle change ; sens promotion/rétrogradation
  déduit du rang (habillage doré si promotion) ; nouveau rôle dans `reason_id`.
- ⚠️ Le rôle **Administrateur** = ajout au groupe superuser `authentik Admins`
  (droits Authentik complets, pas seulement l'app). Assumé (demande explicite).
- Vérifié : 189 tests, E2E réel Authentik sur `test-user` (user↔gestionnaire),
  contrôles visuels (badge cliquable, sélecteur, modale).

2026-07-21 : **Promotion/rétrogradation gestionnaire depuis le site**
(preprod v1.8.31).
- **API Authentik câblée** (`backend/authentik.py`) : add_user/remove_user sur le
  groupe `live2mp3-gestionnaire`. Config `AUTHENTIK_URL`
  (`http://authentik-server:9000`) + `AUTHENTIK_API_TOKEN` injectés dans le
  conteneur (docker-compose `environment` + valeur dans `/opt/apps/*/.env`,
  copiée depuis `/root/.env` sur CT110). Token = `claude-api-token` (60 car.).
  ⚠️ **Backlog** : mettre ce token dans Bitwarden (encore uniquement `/root/.env`).
- **Endpoint** `POST /api/social/users/{u}/gestionnaire {grant}` — `require_admin`
  (autorisation sur groupes **live**, jamais le cache). Refuse soi-même + autre
  admin. Recalcule le rôle depuis les groupes Authentik résultants, écrit le
  cache (contourne le cache monotone = voie de rétrogradation assumée), notifie.
- **Notif non désactivable** (`notify_role`, type `role_grant`/`role_revoke`,
  reason_type=admin) — pas dans la page de préférences. Front : badge « Action
  administrateur » ; promotion en habillage **doré valorisant** (icône trophée,
  contour or, 🎉) ; rétrogradation neutre. Lien vers le profil (`_notif_dict`
  expose `username`).
- Front profil : bouton Promouvoir/Retirer (admins, hors self/admin), **modale
  de confirmation Oui/Non** stylée (pas de `confirm()` natif).
- Vérifié : 189 tests (+1 E2E mocké), **E2E réel contre Authentik** sur
  `test-user` (promote→groupe ajouté+notif, demote→retiré, état restauré),
  contrôles visuels Chromium (notifs dorée/neutre, bouton, modale).

2026-07-20 (suite 3) : **Fiabilisation des badges de rôle** (preprod v1.8.29).
- Symptôme : gestionnaires (nathan, louis) sans badge ; admin (naerod)
  rétrogradé en gestionnaire au rechargement.
- **Plancher gestionnaire** (`_effective_role` + `_publisher_usernames`) :
  publier un album exige les droits gestionnaire → `imported_by` est un plancher
  de rôle fiable, affiché même si la personne ne s'est pas reconnectée depuis la
  capture `/me`. Appliqué au profil (persisté) et aux listes abonnés/abonnements.
- **Cache de rôle monotone** (`_touch_role` + `_ROLE_RANK`) : sur preprod
  l'outpost Authentik ne transmet pas toujours le groupe superuser
  « authentik Admins » → l'admin était rétrogradé. On ne garde que les
  promotions ; rétrogradation réelle = reset explicite du champ `role`.
  naerod restauré en `admin` en base.
- ⚠️ **Reste (backlog)** : câbler l'API Authentik pour résoudre le rôle de
  n'importe qui (y compris simples membres jamais vus) sans dépendre de la
  capture `/me` ni du plancher publications. `AUTHENTIK_API_TOKEN` présent dans
  `.env` mais **vide** + non injecté dans le conteneur + pas d'`AUTHENTIK_URL` →
  API renvoie 403. Nécessite de créer un vrai token de service Authentik.

2026-07-20 (suite 2) : **Photos d'artistes dans les listes d'abonnements**
(preprod v1.8.21).
- Les lignes artiste montraient une icône générique. Endpoint batch
  `GET /api/social/artist-pics?ids=...` (photos Deezer best-effort via le cache
  `suggest`, borne 60 ids) + hydratation lazy côté client (`hydrateArtistPics`
  dans `profile.js`) : l'icône de repli reste si pas de photo / source KO.
  CSS `.fr-ic.has-pic` + `.fr-ic img`. Vérifié E2E (URLs Deezer réelles) +
  rendu Chromium (Coldplay/U2 en vignette). Test `test_artist_pics_batch`.

2026-07-20 (suite) : **Groupes d'abonnements pliables** (preprod v1.8.20).
- Onglet Abonnements : chaque groupe (Artistes / Utilisateurs / Festivals /
  Lieux) a un en-tête cliquable + chevron ; état plié persisté par type dans
  `localStorage` (`l2m-prof-collapsed`). `wireFollowGroups()` dans `profile.js`,
  CSS `.follow-grp.collapsed .follow-list{display:none}` + rotation chevron.
- **Suivi festivals/lieux : déjà fonctionnel** (pas de dev backend). Le bouton
  Suivre + cloche est monté par `entity.js` sur `/festival/{slug}` et
  `/venue/{slug}` ; `FOLLOW_TYPES` inclut festival+venue ; ces pages sont
  atteignables via les liens cliquables festival/lieu de la fiche album.
  Vérifié E2E (POST follow festival OK, rendu Chromium de la page Glastonbury
  avec bouton Suivre).

2026-07-20 : **Listes Abonnements / Abonnés sur les profils** (preprod v1.8.18).
- `GET /api/social/users/{username}` expose `following_list` (abonnements de la
  personne, **groupés par type** : `artist` / `user` / `festival` / `venue`) et
  `followers_list` (ses abonnés — uniquement des utilisateurs). `counts.following`
  / `counts.followers` ajoutés. Helper `_follow_lists()` dans `social.py`.
- **État du visiteur par ligne** : chaque item porte `viewer_following` /
  `viewer_notify` → le bouton Suivre/Suivi + la cloche reflètent la relation de
  *celui qui regarde*, ce qui permet de gérer ses propres abonnements (tri +
  cloches individuelles) directement depuis la liste, y compris sur son profil.
- `has_activity` inclut désormais `follows` : un user qu'on suit ou qui suit
  quelqu'un « existe » (profil accessible même sans publication/commentaire).
- Front `profile.js` : 2 nouveaux onglets (Abonnements / Abonnés), rendu des
  lignes (avatar pour user + badge rôle, icône Material pour artiste/festival/
  lieu), montage de `L2M.followButton` par ligne, i18n FR/EN, pas de bouton sur
  sa propre ligne (« C'est vous »). CSS `.follow-grp` / `.follow-row` / `.fr-*`.
- Vérifié : 185 tests verts (+1 `test_profile_following_and_followers_lists`),
  smoke API dans le conteneur preprod, contrôle visuel Chromium (onglets
  Abonnements + Abonnés, thèmes sombre **et** clair) via harnais stub statique.
- Reste possible : pagination des listes si volumétrie, retrait auto de la ligne
  au dé-suivi (aujourd'hui le bouton repasse à « Suivre » sans enlever la ligne).

2026-07-19 (suivi — Phase 2) : **Notifications in-app** (preprod v1.8.6).
- `backend/notifications.py` : fan-out à la **publication** d'un album
  (`set_published` → `announce_post`). Destinataires = abonnés des entités
  liées (artiste principal + invités + festival + lieu) et de l'auteur, avec la
  cloche active (`follows.notify=1`) et la catégorie non coupée. **Dédup** : une
  seule notif par destinataire même s'il suit plusieurs entités du post ;
  l'auteur ne se notifie pas.
- **Idempotence par environnement** : table `post_announcements` (les manifests
  sont partagés prod/preprod, pas les notifs). `ensure_seeded()` au démarrage
  marque « déjà annoncés » les posts publiés antérieurs (pas de spam
  rétroactif) — 22 posts seedés en preprod.
- **Préférences** (`notif_prefs`, opt-out RGPD) : 4 catégories `new_post:{artist,
  festival,venue,user}` × 2 canaux (`inapp`, `email`). Ligne absente = tout
  activé. L'email est **modélisé mais jamais envoyé** tant que
  `NOTIFY_EMAIL_ENABLED` est faux (Phase 3).
- **UI** : cloche dans le header (`L2M.notifBell`, compteur non-lus + dropdown
  des 8 dernières + « tout marquer lu »), page `/notifications` (liste paginée),
  page `/settings` (rubrique Notifications, interrupteurs par type × canal,
  bandeau « email prochainement » + mention RGPD). Entrées Notifications +
  Paramètres dans le menu utilisateur.
- E2E preprod : fan-out réel vérifié (album synthétique sans média → hors
  catalogue, sans effet de bord ; `announce → 2` dont un abonné réel), captures
  cloche+dropdown / centre / réglages. Données de test purgées de la base
  preprod. 184 tests verts (+6).
- **Reste** : Phase 3 email RGPD — ⚠️ **bloquée** sur le choix du relais
  (expéditeur dédié `noreply@…`, provider transactionnel à créer). Le socle
  email est déjà là (colonne `email` des prefs, master-switch, footer prévu).

2026-07-19 (suivi — Phase 1) : **Socle du système de suivi & pages d'entités**
(preprod v1.8.5). Grosse mission « suivi + notifications » découpée en 3 phases ;
Phase 1 livrée et vérifiée E2E sur preprod.
- **4 types de suivi** : artiste (id Deezer, réutilise `suggest.py`), festival
  (slug, liste auto-construite + texte libre), lieu/salle (slug), utilisateur.
  Table `follows(username,target_type,target_id,target_label,notify,created_at)`.
  `notify` = la cloche (façon X/YouTube) : active par défaut au suivi, se coupe
  sans dé-suivre. Alimentera le futur feed + les notifications (Phase 2).
- **Métadonnées canoniques** : `album.artist_id` (Deezer) + `album.guests`
  (invités, [{id,name}]) + `album.festival_id` (slug). L'éditeur
  (`album.html`) utilise `L2M.autocomplete` : artiste + festival en liste
  déroulante (allowFree pour ne jamais bloquer un artiste hors Deezer / créer un
  festival inédit), invités canoniques seulement (un id est requis pour lier).
  `backend/entities.py` dérive les liens `{type,id,label}` d'un album ; le
  catalogue les expose sur chaque carte.
- **Pages auto** `/artist/{id}`, `/festival/{slug}`, `/venue/{slug}`
  (`entity.html`/`entity.js`) : en-tête (photo Deezer pour l'artiste), compteurs
  abonnés/posts, bouton Suivre/Suivi + cloche, grille de tous les posts liés.
  Namespacées côté API sous `/api/social/` → héritent du soft-auth nginx en
  prod, **aucune modif nginx à la promotion** (les pages HTML tombent dans
  `location /`, coquille publique, état perso via /api/social/*).
- **Badge de rôle** (utilisateur/gestionnaire/admin) : colonne `profiles.role`
  en cache, renseignée à chaque `GET /api/social/me` (appelé par le header) à
  partir des groupes Authentik → affichable sur n'importe quel profil sans API
  Authentik. Bouton Suivre ajouté sur les profils.
- **Liens cliquables** artiste/festival/lieu/invités depuis la fiche album
  (`/api/catalogue/{slug}` expose `entities` pour des ids exacts).
- **Backfill** (`backend/backfill_entities.py`, lancé une fois sur CT110) :
  19 `artist_id` résolus (Coldplay→892, U2→163, Indochine→47, TØP→647650…),
  6 `festival_id`, 4 ignorés (chaînes multi-artistes/typos → à corriger à la
  main via l'éditeur). ⚠️ Écrit dans les manifests **partagés** prod/preprod :
  additif, inoffensif pour le code prod actuel.
- E2E webshot (tunnel + `--as/--groups`) : page Coldplay (11 posts + photo),
  Glastonbury (2 posts), clic Suivre→Suivi+cloche+compteur, éditeur (3
  autocompletes), fiche album (liens), profil (badge Gestionnaire), thème clair.
  178 tests verts (+16 : follows + entities).
- **Reste à faire** : Phase 2 = notifications in-app (table + cloche header +
  centre + fan-out à la publication + réglages profil) ; Phase 3 = email RGPD
  (expéditeur dédié `noreply@…`, master-switch off en preprod, test vers l'email
  perso, footer désabonnement). ⚠️ Phase 3 **bloquée** sur le choix du relais
  d'envoi (domaine dédié : provider transactionnel à créer). Voir BACKLOG.


2026-07-19 (setlist.fm) : **Setlist officielle du concert** (preprod v1.8.4).
Complète la détection audio : celle-ci place les frontières, setlist.fm donne
les *titres* exacts, leur ordre et les invités.
- Clé API dans `/home/claude/.env` (`SETLISTFM_API_KEY`, depuis Bitwarden
  « API KEY setlist.fm ») et dans le `.env` preprod de CT110. **À ajouter au
  .env prod avant toute promotion**, sinon dégradation silencieuse (l'analyse
  continue sans setlist).
- `backend/setlistfm.py` : recherche autonome sur (artiste, date) — deux
  informations que l'IA extrait déjà du titre/description. Cache 24 h, throttle
  local (quotas de l'app : 2 req/s, 1440/jour ; on fait 1 appel par analyse).
- Départage plusieurs setlists d'un même soir : la plus complète gagne (cas
  réel U2 : « U2 » 4 titres vs « U2 with Bruce Springsteen » 2 titres).
  Les invités deviennent l'artiste de piste (« U2 with Chris Martin ») ;
  les morceaux `tape` (diffusés par la sono) sont écartés.
- Fusion prudente (`linktool.apply_setlistfm`) : si des timecodes existent déjà
  (chapitres vidéo) et que le nombre de titres diffère, on ne touche à rien ;
  si le nombre correspond, on garde les timecodes et on prend les titres
  officiels. Lieu/ville/tournée complétés depuis la donnée officielle.
- ⚠️ **Attribution obligatoire** (CGU setlist.fm : « place an attribution link
  each time you use setlist.fm data ») : l'URL suit la donnée de bout en bout —
  bandeau dans l'outil (« Setlist officielle du concert (N titres) — source :
  setlist.fm ») **et** crédit sous la setlist de la fiche album via
  `meta.setlistfm_url`. Ne pas retirer ces liens.
- **E2E réel** : le lien U2 remonte les 4 titres exacts avec Chris Martin ×2 et
  Bruce Springsteen ×2, lieu « Times Square / New York » confirmé.
- 162 tests verts (11 nouveaux, API simulée — aucun appel réseau en test).

2026-07-19 (détection des coupes) : **Frontières par l'énergie du signal**
(preprod v1.6.14). Retour utilisateur : les coupes IA tombaient au milieu des
chansons alors que les transitions se voient à l'œil sur la forme d'onde.
- **Diagnostic mesuré** : dans un live il n'y a jamais de silence réel entre
  deux morceaux (applaudissements, foule, annonces). Sur le concert U2 de
  20 min, `silencedetect` ne trouve **qu'un seul** silence à -30 dB (le réglage
  du pipeline) et **aucun** à -35 dB. L'IA n'avait donc aucun repère temporel
  et plaçait les frontières uniquement d'après les paroles.
- **Nouveau `backend/pipeline/boundaries.py`** : enveloppe RMS (numpy, lecture
  par blocs — le WAV pèse ~200 Mo), passage en dB, puis creux jugés face au
  **niveau ambiant local** (médiane glissante 45 s) et non à un seuil absolu
  (une captation live est très compressée : énergie mesurée entre -16 et
  -11 dB). Écart minimal de 120 s entre deux coupes → écarte les ponts et
  passages calmes internes. Bords ignorés (60 s).
  Résultat sur U2 : **4:41 (13,7 dB) / 10:02 (16,7 dB) / 15:53 (4,1 dB)** —
  exactement les trois transitions visibles, très au-dessus du bruit (1-2 dB).
- **Mélange des deux** (demande utilisateur) : les candidats (instant +
  profondeur) sont donnés à l'IA, qui doit choisir ses frontières **parmi
  eux** ; la transcription sert à savoir quel candidat correspond à quelle
  transition. Consigne ajoutée sur la durée typique d'un titre live (3-8 min).
- **Snap** (`_snap_markers` / `_snap_tracks`) : la réponse de l'IA est recalée
  sur le creux voisin (tolérance 25 s) et les frontières communes recollées.
  Une frontière sans creux à portée est laissée telle quelle (medley,
  enchaînement sans coupure).
- **Fallback sans IA** = les n-1 creux les plus marqués : donne déjà le bon
  découpage sur U2 ; la répartition uniforme n'est plus qu'un dernier recours.
  C'est aussi ce que produit le bouton « passer ».
- `numpy` déclaré dans requirements (dépendance directe désormais, elle
  n'arrivait qu'indirectement via faster-whisper).
- 151 tests verts. Fichier de travail pour l'analyse :
  `projects/.analysis/` sur le volume (audio + enveloppe + PNG annotée) —
  à supprimer quand il ne servira plus.
- **Pas encore fait** : setlist.fm (nécessite une clé API gratuite côté
  utilisateur) pour récupérer la setlist officielle d'un concert.

2026-07-19 (skip détection) : **Bouton « passer » sur la détection des
chansons** (preprod v1.6.13). Constat utilisateur : c'est l'étape la plus longue
(transcription whisper) et elle ne fait qu'*estimer* les coupes, que l'humain
ajuste de toute façon dans l'éditeur.
- `POST /api/jobs/{slug}/skip-detection` (gestionnaire) pose un drapeau dans
  `_skip_detection`. Il est relu **par le callback de progression whisper**
  (appelé à chaque segment) qui lève `_SkipDetection` → abandon en quelques
  secondes sans tuer le thread. Également vérifié avant de lancer whisper et
  après la transcription.
- Coupes de secours appliquées (`_fallback_markers` : frontières sur les
  silences les plus longs, déjà détectés juste avant) pour que l'éditeur
  s'ouvre avec des pistes exploitables plutôt que vide.
- `source="skipped"` distinct de `"fallback"` (échec IA) → libellé dédié.
  Drapeau nettoyé dans un `finally` + au lancement de `prepare`.
- Front : bouton affiché tant que l'étape tourne (drapeau `skippable` porté par
  les events SSE), avec une phrase d'explication ; i18n FR/EN.
- ⚠️ **Limite connue** : si le skip est demandé pendant le *chargement* du
  modèle whisper (avant le 1er segment), il ne prend effet qu'au premier
  segment — quelques secondes si le modèle est en cache disque, mais 1-2 min au
  tout premier usage après un rebuild (téléchargement ~460 Mo, le cache HF
  n'est pas persisté dans un volume). Piste d'amélioration : monter un volume
  sur ~/.cache/huggingface.
- 148 tests verts (skip avant lancement / pendant la transcription / auth+404).
- **preprod uniquement** (demande explicite).

2026-07-19 (header) : **Boutons Outil + Importer fusionnés** (preprod v1.6.12).
Un seul bouton « Importer » dans le header, **visible par tous** (y compris
visiteurs anonymes) ; il ouvre une fenêtre de choix (`openImportChoice` dans
social.js, styles dans app.css partagé) :
- « Importer manuellement » → modale d'import existante (`opts.onImport`, ou
  redirection `/?import=1` hors vitrine)
- « Depuis un lien (assisté par IA) » → `/app`
- **Non-gestionnaires** : les deux cartes sont grisées avec un badge « Réservé
  aux gestionnaires » + bandeau explicatif (« demandez cet accès » pour un user
  connecté, « connectez-vous » + bouton Se connecter pour un anonyme).
  **Aucun changement de droits backend** — `/api/import/*` et `/api/tool/*`
  restent `require_gestionnaire` (choix utilisateur : montrer la fonctionnalité
  sans l'ouvrir).
- Les entrées `.um-mobile` dupliquées dans le menu avatar sont **supprimées**
  (un seul bouton tient dans le header mobile ; icône seule ≤480px). Classe
  `.um-mobile` et règle de masquage retirées d'app.css.
- vitrine : le mode « Voir en tant que » est reflété via `opts.rights()`
  (callback évalué à l'ouverture, pas figé au chargement).
- Validé webshot : gestionnaire (2 cartes actives), user simple (grisé +
  message), anonyme (grisé + Se connecter), mobile 412px (1 colonne), thème
  clair, et chaînage réel Importer → « manuellement » → modale d'import.

2026-07-19 (correction sémantique éditeur) : **La liaison va fin→début suivant**
(preprod v1.6.10). L'entrée précédente décrivait l'inverse (début lié à la fin
précédente) — corrigé après retour utilisateur. Intention réelle : chaque
chanson doit COMMENCER pile sur sa musique (début net « si je lance la piste,
elle démarre direct ») ; les transitions parlées restent à la FIN de la piste
précédente (« ça se skip »). Donc :
- Le **début** de chaque piste est la référence éditable (l'IA l'indique).
- La **fin** suit le début de la piste suivante (liée, grisée) — la transition
  parlée est donc incluse à la fin de la piste. `endRaw` = fin musicale
  détectée, mémorisée ; délier la fin la restaure (couper la transition).
- Cadenas sur la **fin** (sauf la dernière piste). Début de la 1re piste et fin
  de la dernière toujours libres (silence d'intro / crédits de fin).
- Geste principal : « définir le début » d'une piste (l'IA se trompe souvent de
  quelques secondes) → la fin liée de la piste précédente suit. « définir la
  fin » d'une piste liée déplace la même frontière (= début de la suivante).
- Modèle EDIT {title,artist,start,end,endRaw,linked}, `relinkEnds()`.
- **Validé E2E preprod** (webshot+puppeteer) : débuts éditables / fins grisées
  liées au début suivant ; délier la fin piste 1 restaure 4:12.8 (coupe la
  transition, gap jusqu'à 7:46.7) ; définir-début piste 2 à 4:30 → fin piste 1
  suit à 4:30. Marqueur disque + barre progression inchangés. 145 tests verts.

2026-07-19 (suite) : **Éditeur de coupes enrichi** (preprod v1.6.9). Trois
demandes utilisateur, 100 % frontend (backend/API inchangés, 145 tests verts) :
- **Liaison + cadenas par piste** : le début de chaque piste (sauf la 1re) est
  lié par défaut à la fin de la précédente (grisé pointillé, pas de gap — on ne
  coupe pas les transitions parlées). Un cadenas par piste délie pour éditer.
  Début de la 1re piste et fin de la dernière toujours libres. `startRaw` :
  mémorise le début « libre » (détecté par l'IA, puis ajustable) — lier le
  masque, délier le restaure (ne réécrase pas l'ajustement utilisateur).
- **Boutons « définir début / fin »** (icônes |◄ / ►|) : reprennent la position
  exacte du lecteur. Définir la fin d'une piste propage au début lié suivant
  (cascade) — c'est le geste principal : j'écoute, pause au bon endroit, clic.
  set-début désactivé quand le début est lié (cohérent avec le champ grisé).
- **Marqueur « 2e disque » à 88 min cumulées** (durées réelles, pas temps
  source) : point Peaks au début de la piste qui bascule + bandeau, recalculés
  en direct à chaque modif. Ne coupe jamais une piste (frontière = début de
  piste). Multi-disques gérés (176, 264 min…).
- Refonte autour d'un modèle `EDIT` explicite : `commitEdit()` unique normalise
  ordre (tri par start), liaisons, lignes DOM, segments Peaks et marqueur.
  Toast, ligne `.invalid` si fin≤début, i18n FR/EN.
- **Validé E2E preprod** (webshot + puppeteer) : cadenas ferme/ouvre et grise,
  délier restaure le début IA (7:46 vs valeur liée 4:12), définir-la-fin met
  4:20 et le début lié suivant suit à 4:20, bandeau disque « 102 min / 2 disques »
  sur un set fictif long. Projets test purgés.
- **preprod uniquement — en attente signal utilisateur pour promotion prod**
  (avec la barre de progression v1.6.6).

2026-07-19 : **Barre de progression pendant la préparation** (preprod v1.6.6).
Retour utilisateur : à l'étape « Détection des chansons » (whisper CPU, plusieurs
minutes), aucun indicateur — impossible de savoir si ça tourne / où ça en est /
si c'est bloqué. Ajout d'une barre par étape :
- **Déterminée** (remplie au %) pour le téléchargement (yt-dlp `[download] x%`)
  et la **transcription** : `preanalyze.transcribe` reçoit un callback
  `progress(frac)` appelé segment par segment (position = `segment.end /
  info.duration`), le générateur faster-whisper étant paresseux. Callback
  throttlé (≥1 %) publié en SSE `phase=transcription, pct=…`. Avant le 1er
  segment (chargement du modèle) : pas de pct → barre indéterminée.
- **Indéterminée** (bande animée qui balaie) pour les phases non mesurables
  (silences, analyse IA, découpe, tags, artwork, disc, bundle) — signale
  « ça tourne » sans fausse précision. `@media(prefers-reduced-motion)` gère
  l'accessibilité.
- Front : `runProgress` restructure chaque `<li>` en tête (dot+label+pct) +
  barre ; classes `.running/.done/.indet`, `.stage-fill` en `--accent`
  (en cours) ou `--ok` (terminé). i18n `running_word` FR/EN ; « (long) »
  retiré des libellés (la barre le dit mieux).
- Test : `test_prepare_publishes_transcription_progress` (progression monotone
  jusqu'à 100 %). 145 verts. Rendu validé au pixel (webshot, dark+light,
  déterminée 42 % + indéterminée).
- **E2E réel preprod** : préparation U2 complète, transcription émettant
  5,6→21→37→57→73→88→100 % (monotone) puis analyse IA et fin. Projet test purgé.

2026-07-19 : **Prod v1.7.0** — promotion complète de l'outil « album depuis un
lien » (voir entrées 2026-07-18). Merge preprod->main via deploy-prod-live2mp3.sh
(minor : première mise en service réelle de l'outil). Après déploiement Docker,
bloqué en test réel : `/api/tool/analyze` n'était pas dans le bloc nginx
protégé par Authentik (`location ~ ^/(app|api/jobs|api/albums|download)`) —
tombait dans `location /` qui vide les en-têtes d'identité côté public, donc
401 systématique pour un gestionnaire. Ajout de `api/tool` à la regex.
**Piège inode reproduit** ([[feedback_nginx_bind_mount_inode]]) : `sed -i`
sur `/opt/apps/nginx/nginx.conf` recrée le fichier (comportement mv), le
bind-mount du conteneur nginx reste accroché à l'ancien inode -> `nginx -s
reload` ne suffit pas, il faut `docker restart nginx`. Vérifié après coup :
route analyze protégée (302 vers Authentik), vitrine/app/hub/dorianjulien/
naerod tous 200 (aucune régression sur les autres sites servis par ce nginx
partagé). Sauvegarde avant édition : nginx.conf.bak.<timestamp>.

2026-07-18 (fin de journée) : **Publication par défaut = brouillon** (preprod
v1.6.5). Tout album créé par l'outil lien naît `published: false` (volume
partagé prod/preprod : rien ne devient public avant validation). Visibilité
par rôle complète : un album dépublié répond **404 au public et aux users**
sur la fiche (`/api/catalogue/{slug}`), `/cover`, tous les `/download/*` et
les lectures sociales (fiche, commentaires, pochettes) — même réponse qu'un
slug inconnu (`_ensure_album_visible` dans main.py, `_album_visible` dans
social.py). Les gestionnaires voient tout : carte grisée + badge BROUILLON
(vitrine, existant), chip « Dépublié » sur la fiche (nouveau), statut
explicite + bouton Publier/Dépublier dans Gérer (existant, libellé précisé).
Écran final de l'outil : note « créé dépublié » + bouton « Gérer / publier ».
`catalogue_detail` expose `published` et résout labels/has_* aussi pour les
brouillons (gestionnaires). Tests : 144 verts (parcours brouillon→publication ;
helpers _album/_make_album publient désormais explicitement). E2E preprod :
brouillon synthétique invisible anonyme/user (404 partout), visible
gestionnaire, publication → visible, purgé ensuite.

2026-07-18 (après-midi) : **Outil « album depuis un lien » rendu 100 % fonctionnel**
(preprod v1.6.0 → v1.6.4). C'était la raison d'être du site ; le code existant
était inachevé : le formulaire créait un manifest mais aucune route ne lançait
download/preanalyze, `/api/jobs/{slug}/audio` n'existait pas, et le global
Peaks.js était mal référencé (`Peaks` vs `peaks`) — l'éditeur n'avait donc
jamais fonctionné.

Flux complet livré (assistant 5 étapes sur /app, accès gestionnaires) :
1. **Analyse** — POST /api/tool/analyze : sonde `yt-dlp --dump-single-json
   --no-playlist` (⚠ les liens copiés portent souvent `&list=` radio) puis
   DeepSeek (`llm.extract_album_info`) : artiste, titre album, **date du
   concert** (pas de l'upload), lieu/ville, événement, setlist avec artistes
   par piste et timecodes si chapitres/description. Fallback heuristique sans
   LLM (chapitres → setlist). Testé sur U2 Times Square 2014 : 4/4 pistes,
   invités Chris Martin/Springsteen corrects.
2. **Formulaire vérifiable** pré-rempli (carte source + chip « Pré-rempli par
   IA — à vérifier », setlist en lignes éditables, option vidéo MP4).
3. **Préparation** — POST /api/jobs/{slug}/prepare (thread + SSE) :
   download audio seul par défaut (`source.media`, % dans SSE) ou mkv complet,
   preview.mp3 128k (l'éditeur ne charge pas le wav de 200 Mo), waveform.dat,
   pochette = miniature YouTube (convertie jpg si webp, créditée via la
   collection covers → APIC/tray card gratuits), marqueurs : timecodes fournis
   → rien à faire ; sinon silences + faster-whisper (CPU : `small`,
   WHISPER_MODEL_CPU) + DeepSeek ; **setlist vide → l'IA identifie les
   chansons à l'écoute** (`request_auto_setlist`) ; échec LLM → découpe de
   secours sur les n-1 plus longs silences (l'éditeur reste utilisable).
4. **Éditeur de coupes** (vérification humaine obligatoire) : Peaks.js v3.4.2
   (global UMD `peaks` !), segments draggables ⇄ table (titre/artiste/
   start/end/écoute/suppression), ajout de piste au playhead, zoom, couleurs
   thème (défauts Peaks noirs). PUT /api/jobs/{slug}/setlist trie/renumérote/
   verrouille. Reprise de session via `/app#slug`.
5. **Rendu** — render/tags/artwork/disc/bundle en SSE, puis album visible dans
   la bibliothèque (catalogue scanne manifest + build/audio), crédité
   meta.imported_by, source URL affichée.

Pièges corrigés en E2E : (a) écriture périmée du manifest dans le thread de
préparation (recharger après download.run sinon download reste `pending` et la
reprise re-télécharge) ; (b) `new_manifest` perdait `tracks[].artist` ;
(c) SSE derrière nginx/Cloudflare → `X-Accel-Buffering: no` ; (d) tags :
TPE1 = artiste de la piste, TPE2 = artiste album ; (e) `track_filename`
aligné sur la convention bibliothèque « 01. Titre.mp3 » (source unique
`manifest.sanitize_filename`).

E2E réel complet sur preprod avec la vidéo U2 (20 min) : analyse parfaite,
préparation ~6 min (dont whisper small + téléchargement du modèle), coupes IA
plausibles (piste 4 détectée à 984,5 s, l'URL utilisateur pointait t=978s),
rendu ~2 min, 4 MP3 tagués + APIC + PDF + ISO + ZIP 63 Mo, album au catalogue.
**Projet de test supprimé ensuite** (fichiers + lignes covers/likes/comments)
pour laisser l'utilisateur rejouer le test. 143 tests verts.

⚠ Volume partagé prod/preprod : un album créé par l'outil preprod apparaît
aussi en prod. Le rendu est rapide mais whisper CPU est le poste lent ;
premier usage = téléchargement du modèle (~460 Mo) dans le conteneur
(perdu au rebuild).
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

2026-07-23: Actions groupées vitrine — préprod v1.10.6.
- **Sélection multiple** : bouton « Sélectionner » (chip toolbar) → mode
  sélection (body.select-mode). Case à cocher overlay sur chaque carte, clic
  n'importe où sur la carte coche/décoche, liens internes neutralisés
  (pointer-events). État = `picked` (Set de slugs) — NE PAS confondre avec
  `selected` (déjà pris par les filtres étiquettes).
- **Barre d'actions flottante** (#bulkbar) : compteur + Tout sélectionner/
  désélectionner (sur le visible filtré) + Télécharger MP3/MP4 (tous users
  connectés) + Publier/Dépublier (gestionnaires, via meDisplay → respecte
  « Voir en tant que ») + Terminer.
- Backend :
  - `POST /download/bulk` {slugs,kind} (require_user) : un ZIP regroupant le
    zip par album (ZIP_STORED, réutilise `_zip_media` en cache, pas de
    recompression). Albums invisibles/sans média ignorés silencieusement.
    404 si rien de téléchargeable, 400 kind≠mp3|mp4. Streamé.
  - `PATCH /api/albums/bulk-published` {slugs,published} (require_gestionnaire) :
    idempotent (skip si déjà dans l'état), notifie les abonnés pour chaque
    album nouvellement publié, remonte `updated/count/notified/missing`.
- i18n FR/EN complet (clés sel_*). Toast local (#sel-toast, pas de helper L2M).
- Tests : test_bulk_published_permissions_and_idempotency +
  test_bulk_download_auth_and_zip. Suite 195 passed / 1 skipped.
- Idées d'autres actions en masse proposées à l'utilisateur (non implémentées) :
  suppression groupée, étiquetage/désétiquetage en masse, ré-attribution
  artiste/lieu, export pochettes PDF groupé, régénération covers.

2026-07-23 (suite): polish actions groupées + INCIDENT OOM — preprod v1.10.11.
- Bouton « Terminer » → croix seule (icône close, title/aria-label au survol).
- Retrait du bouton « Télécharger MP4 » (vidéo = un album à la fois) ; « Télécharger
  MP3 » renommé « Télécharger », boutons sur une seule ligne.
- Fix centrage vertical des boutons de la barre : `button,.primary` (app.css) porte
  un `margin-top:16px` global (formulaires) → neutralisé par `.bulkbar button{margin:0}`.
- **INCIDENT** : le téléchargement groupé construisait le ZIP entièrement en mémoire
  (io.BytesIO + fetch/blob). « Tout télécharger » sur beaucoup d'albums → uvicorn à
  8,5 Go → CT110 (cap 10 Go) en thrashing (load 237), sites prod KO ~10 min, aggravé
  par 2 builds deploy concurrents. Résolu : kill du process depuis l'hôte Proxmox +
  refonte `/download/bulk` en **GET + fichier temporaire disque** (FileResponse,
  Content-Length, BackgroundTask os.remove) ; le front déclenche un **download natif
  du navigateur** (lien GET, barre de progression, rien en mémoire).
  Voir workspace/debugging/2026-07-23_ct110-oom-download-bulk-memoire.md
- Tests adaptés (GET au lieu de POST). 195 passed. Endpoint vérifié : GET 200,
  application/zip, Content-Length OK, magic PK.

2026-07-30 : revue de code complète — preprod v1.15.26 (commit eecf28e).
- **Suite de tests réparée : 192/203 → 202 passed, 1 skipped.** Les 10 échecs
  n'étaient pas des régressions produit mais de la dette de la migration de la
  progression vers Redis :
  - `tests/test_linktool.py` lisait `main._progress_last`, dictionnaire supprimé
    lors de cette migration → réécrit sur `progress.history()` / `progress.reset()`.
    `_progress_last` n'a délibérément **pas** été ressuscité.
  - `tests/test_t9_api.py` exigeait un Redis réel (absent de CT102) → fixture
    autouse `fakeredis` dans `conftest.py`, un `FakeServer` partagé entre
    `progress._client`, `renderqueue._rq_conn` et `_kv_conn`.
  - **Piège rencontré** : une fois Redis joignable, la suite se bloquait — le
    rendu partait vraiment en file RQ, sans worker pour l'exécuter, et le flux
    SSE attendait un évènement final qui n'arrivait jamais. Résolu par une file
    `is_async=False` (exécution en process). La suite ne dépend plus d'aucun
    service externe.
- `except Exception` muets tracés (`renderqueue` ×7, `social`, `covers`, `main` ×2) :
  `log.warning`/`debug` ajouté, **flux de contrôle inchangé**. Ceux de
  `progress.py` restent volontaires (documentés).
- Docstring de `main.py` corrigé : il décrivait un repli d'exécution inline qui
  n'existe plus depuis le passage au worker.
- Imports morts retirés ; `backend/` est pyflakes-clean.
- **À trancher** : `import_drive.py` (21 Ko, racine, non suivi par git, aucun
  référencement trouvé) — laissé intact, décision utilisateur attendue.
