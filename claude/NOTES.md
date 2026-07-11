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
- Reste : validation visuelle preprod + promo prod (UI) avec bloc nginx /api/social.

## Modèle commentaires
Aplati à 1 niveau : racine (`parent_id` NULL) + réponses rattachées à la racine
avec mention `@auteur` ; « voir plus » pour dérouler. Votes ▲/▼, tri Top/Récents,
édition + suppression douce (auteur ou modérateur).
