/* albums.js — fiches d'albums, grille et regroupement, partagés par la vitrine (/) et la
   page profil (/u/…). Source unique du rendu d'une fiche : extrait de vitrine.html
   (2026-09-30) pour que le profil ait exactement les mêmes cartes, la même densité
   (<nrd-density>) et le même regroupement. Style : albums.css. Dépend de social.js (L2M).

   API (objet global L2MAlbums) :
     init({me})                       me() → identité réelle (pour « j'aime »)
     card(a, o)                       HTML d'une fiche. o : { user, pick, picked, poster }
                                        user   profil affiché (authenticated, is_gestionnaire)
                                        pick   true = case de sélection (mode sélection de la vitrine)
                                        poster HTML du posteur (facultatif)
     render(wrap, albums, {sort, card})   grille à plat, ou groupes (artiste / année) selon `sort`
     loadSocial(authenticated)        charge compteurs + « j'aime » puis remplit les rangées
     patchSocial()                    (re)remplit les rangées sociales du DOM
     year(date)                       année (« 2024 ») d'une date libre, "" si absente
     mode()                           densité mémorisée (cozy | compact | tiny | list)
*/
(function () {
  const S = {
    fr: {
      badge_new: "Nouveau", badge_updated: "Mis à jour",
      date_unknown: "Date inconnue", venue_unknown: "Lieu inconnu",
      quickdl: "Téléchargement rapide", lockdl: "Se connecter pour télécharger",
      status_draft: "Brouillon", status_unpublished: "Non publié",
      pick: "Sélectionner", video_full: "Concert complet en vidéo",
      like: "J'aime", comments: "Commentaires",
      nrd_density_label: "Taille d'affichage", nrd_density_cozy: "Grandes vignettes",
      nrd_density_compact: "Vignettes moyennes", nrd_density_tiny: "Miniatures",
      nrd_density_list: "Liste",
    },
    en: {
      badge_new: "New", badge_updated: "Updated",
      date_unknown: "Unknown date", venue_unknown: "Unknown venue",
      quickdl: "Quick download", lockdl: "Log in to download",
      status_draft: "Draft", status_unpublished: "Unpublished",
      pick: "Select", video_full: "Full concert video",
      like: "Like", comments: "Comments",
      nrd_density_label: "Display size", nrd_density_cozy: "Large thumbnails",
      nrd_density_compact: "Medium thumbnails", nrd_density_tiny: "Small thumbnails",
      nrd_density_list: "List",
    },
  };
  const lang = () => localStorage.getItem("l2m-lang") || "fr";
  const s = (k) => (S[lang()] || S.fr)[k] || S.fr[k] || k;
  const esc = (v) => L2M.esc(v);

  let getMe = () => ({ authenticated: false });
  function init(o) { if (o && o.me) getMe = o.me; }

  // Les composants naerod-ui (<nrd-density>) demandent d'abord au site ses libellés.
  document.addEventListener("naerod:i18n", (e) => {
    const v = (S[lang()] || {})[e.detail.key];
    if (typeof v === "string") e.detail.text = v;
  });

  const MODES = ["cozy", "compact", "tiny", "list"];
  function mode() {
    let m = null;
    try { m = localStorage.getItem("l2m-density"); } catch (e) { /* stockage indisponible */ }
    return MODES.indexOf(m) > -1 ? m : "cozy";
  }

  // ── Dates ─────────────────────────────────────────────────────────────────
  function year(date) {
    if (!date) return "";
    const m = String(date).match(/\d{4}/);
    return m ? m[0] : "";
  }
  const NEW_DAYS = 21, UPDATED_DAYS = 30;
  function daysSince(ref) {
    if (!ref) return Infinity;
    const t = new Date(ref).getTime();
    if (isNaN(t)) return Infinity;
    return (Date.now() - t) / 864e5;
  }
  // « Nouveau » = mise en ligne récente, pas import récent : un album importé il y
  // a des mois mais publié aujourd'hui est une nouveauté. Repli sur l'import pour
  // les albums sans date de publication (brouillons, vue gestionnaire).
  function isNew(a) { return daysSince(a.first_published_at || a.imported_at) <= NEW_DAYS; }
  // « Mis à jour » = manifest réécrit récemment (pistes, titres, pochette…) sur un
  // album qui n'est plus une nouveauté — les deux badges ne coexistent jamais.
  function isUpdated(a) { return !isNew(a) && daysSince(a.updated_at) <= UPDATED_DAYS; }

  // ── Pochette ──────────────────────────────────────────────────────────────
  const GENERIC_COVER = `<div class="cover-generic">
  <svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">
    <circle cx="50" cy="50" r="44" fill="#2a2d38"/>
    <circle cx="50" cy="50" r="43" fill="none" stroke="#4a4d5a" stroke-width="1.5" opacity=".5"/>
    <circle cx="50" cy="50" r="38" fill="none" stroke="#4a4d5a" stroke-width="0.8" opacity=".4"/>
    <circle cx="50" cy="50" r="33" fill="none" stroke="#4a4d5a" stroke-width="0.8" opacity=".3"/>
    <circle cx="50" cy="50" r="26" fill="none" stroke="#4a4d5a" stroke-width="0.8" opacity=".25"/>
    <circle cx="50" cy="50" r="20" fill="#1a1c22"/>
    <circle cx="50" cy="50" r="19" fill="none" stroke="#4a4d5a" stroke-width="1" opacity=".4"/>
    <circle cx="50" cy="50" r="5" fill="#0a0a0a"/>
  </svg>
</div>`;

  function coverHtml(a) {
    if (!a.has_cover) return GENERIC_COVER;
    // ?v=<mtime> : dès qu'un gestionnaire remplace la pochette, l'URL change et
    // le navigateur refetch — sans ce param, la vitrine sert l'ancienne image
    // depuis son cache disque (le nom de fichier ne change pas).
    const v = a.cover_v || 0;
    // w=320 : vignette WebP dérivée (~20 Ko) au lieu de la pochette d'origine
    // (jusqu'à 5 Mo). La pleine résolution reste servie sans `w` (fiche album,
    // lightbox, téléchargement). srcset 1x/2x : un écran haute densité prend la
    // version 640 sans imposer son poids aux autres.
    const u = (n) => `/cover/${a.slug}?v=${v}&w=${n}`;
    return `<img class="cover-img" src="${u(320)}" srcset="${u(320)} 1x, ${u(640)} 2x" alt="" loading="lazy" decoding="async"
      onload="this.classList.add('rdy');this.parentNode.classList.remove('is-load')"
      onerror="this.classList.add('rdy');this.parentNode.classList.remove('is-load')">`;
  }

  // Nom d'entité cliquable vers sa page auto (artiste/lieu/festival), comme sur la
  // fiche album. L'id exact vient de a.entities ; à défaut (ex. artiste sans id
  // Deezer, donc sans page), texte simple plutôt qu'un lien mort.
  function entLink(a, type, label) {
    if (!label) return "";
    const e = (a.entities || []).find((x) => x.type === type && x.label === label);
    return e
      ? `<a class="entity-link" href="/${type}/${encodeURIComponent(e.id)}">${esc(label)}</a>`
      : esc(label);
  }

  // ── Fiche ─────────────────────────────────────────────────────────────────
  function card(a, o) {
    o = o || {};
    const user = o.user || getMe();
    const hasDownloadable = a.has_mp3 || a.has_mp4 || a.has_cover || a.has_traycard;
    let dlBtn = "";
    if (hasDownloadable) {
      if (user.authenticated) {
        const dlHref = a.has_mp3 ? `/download/${a.slug}/mp3` : `/download/${a.slug}/mp4`;
        dlBtn = `<a class="primary" href="${dlHref}"><span class="material-symbols-outlined" aria-hidden="true">download</span>${s("quickdl")}</a>`;
      } else {
        dlBtn = `<a class="icon-btn" href="/outpost.goauthentik.io/start?rd=${location.pathname}"><span class="material-symbols-outlined" aria-hidden="true">lock</span>${s("lockdl")}</a>`;
      }
    }
    const newBadge = isNew(a)
      ? `<span class="new-badge" title="${s("badge_new")}"><span class="material-symbols-outlined" aria-hidden="true">auto_awesome</span>${s("badge_new")}</span>`
      : (isUpdated(a)
        ? `<span class="new-badge upd-badge" title="${s("badge_updated")}"><span class="material-symbols-outlined" aria-hidden="true">update</span>${s("badge_updated")}</span>` : "");
    // Badge de statut — gestionnaire seulement, draft/non-publié uniquement (alertes).
    const ST_IC = { draft: "draft", unpublished: "visibility_off" };
    const hasVideoBadge = !!a.has_video_full;
    const statusBadge = user.is_gestionnaire && a.status && ST_IC[a.status]
      ? `<span class="status-badge st-${a.status}${hasVideoBadge ? " has-video" : ""}"><span class="material-symbols-outlined" aria-hidden="true">${ST_IC[a.status]}</span>${s("status_" + a.status)}</span>` : "";
    // Tout le catalogue est en MP3 (c'est l'objet du site) : seule la vidéo est une
    // information — même icône pour tout le monde, gestionnaire compris.
    const videoBadge = hasVideoBadge
      ? `<span class="video-badge" title="${s("video_full")}"><span class="material-symbols-outlined" aria-hidden="true">movie</span></span>` : "";
    const cardTitle = esc([a.artist, a.title || a.slug].filter(Boolean).join(" — "));
    const pick = o.pick
      ? `<button type="button" class="pick" role="checkbox" aria-checked="${!!o.picked}" aria-label="${s("pick")}"><span class="material-symbols-outlined" aria-hidden="true">check</span></button>` : "";
    // Artiste : toujours affiché (ligne dédiée, cliquable), y compris sous un
    // séparateur d'artiste — sans lui la fiche perdait son sujet (2026-09-30).
    const artistLine = a.artist
      ? `<div class="al-artist"><span class="material-symbols-outlined" aria-hidden="true">artist</span><span class="al-artist-name">${entLink(a, "artist", a.artist)}</span></div>` : "";
    return `<div class="album${o.pick ? " selectable" : ""}${a.published === false ? " unpublished" : ""}${o.picked ? " picked" : ""}" data-slug="${esc(a.slug)}" title="${cardTitle}">
    <a class="album-open" draggable="false" href="/album/${encodeURIComponent(a.slug)}" aria-label="${cardTitle}"></a>
    ${pick}
    <div class="badges">${statusBadge}${newBadge}${videoBadge}</div>
    <div class="cover${a.has_cover ? " is-load" : ""}">${coverHtml(a)}</div>
    <div class="body">
      ${artistLine}
      <div class="al-title">${esc(a.title || a.slug)}</div>
      <div class="al-line${a.date ? "" : " muted-placeholder"}"><span class="material-symbols-outlined" aria-hidden="true">calendar_month</span>${a.date ? esc(a.date) : s("date_unknown")}</div>
      <div class="al-line${a.venue ? "" : " muted-placeholder"}"><span class="material-symbols-outlined" aria-hidden="true">location_on</span>${a.venue ? entLink(a, "venue", a.venue) : s("venue_unknown")}</div>
      ${a.festival ? `<div class="al-line"><span class="material-symbols-outlined" aria-hidden="true">festival</span>${entLink(a, "festival", a.festival)}</div>` : ""}
      ${o.poster ? `<div class="al-poster">${o.poster}</div>` : ""}
    </div>
    <div class="al-social"></div>
    ${dlBtn ? `<div class="btns">${dlBtn}</div>` : ""}
  </div>`;
  }

  // ── Regroupement ──────────────────────────────────────────────────────────
  // Tri par artiste → un groupe par artiste (ordre alphabétique) ; tri par date →
  // un groupe par année (la plus récente d'abord, « Date inconnue » en dernier).
  // Dans chaque groupe, l'ordre reçu est conservé. Autres tris : pas de groupe.
  function groups(list, sort) {
    if (sort === "artist") {
      const g = new Map();
      list.forEach((a) => {
        const k = a.artist || "—";
        if (!g.has(k)) g.set(k, []);
        g.get(k).push(a);
      });
      return [...g.entries()].sort(([a], [b]) => a.localeCompare(b, undefined, { sensitivity: "base" }));
    }
    if (sort === "date_concert") {
      const g = new Map();
      list.forEach((a) => {
        const k = year(a.date);
        if (!g.has(k)) g.set(k, []);
        g.get(k).push(a);
      });
      return [...g.entries()]
        .sort(([a], [b]) => (a === "" ? 1 : b === "" ? -1 : b.localeCompare(a)))
        .map(([k, items]) => [k || s("date_unknown"), items]);
    }
    return null;
  }

  function render(wrap, albums, o) {
    const cardFn = (o && o.card) || card;
    const g = groups(albums, o && o.sort);
    if (!g) {
      wrap.className = "albums";
      wrap.innerHTML = albums.map(cardFn).join("");
    } else {
      wrap.className = "";
      wrap.innerHTML = g.map(([label, items]) => `
      <div class="artist-group" role="group" aria-label="${esc(label)}">
        <div class="artist-sep">
          <span class="artist-bar"></span>
          <span class="artist-name">${esc(label)}</span>
          <span class="artist-bar"></span>
        </div>
        <div class="albums">${items.map(cardFn).join("")}</div>
      </div>`).join("");
    }
    patchSocial();
  }

  // ── Rangée sociale (j'aime / commentaires) ────────────────────────────────
  let SOC_CACHE = {}, MY_LIKES = new Set();
  function patchSocial() {
    document.querySelectorAll(".al-social").forEach((el) => {
      const slug = el.closest(".album")?.dataset.slug;
      if (!slug) return;
      const c = SOC_CACHE[slug] || { likes: 0, comments: 0 };
      const liked = MY_LIKES.has(slug);
      el.innerHTML =
        `<button class="soc-btn is-like${liked ? " liked" : ""}" aria-label="${s("like")}" aria-pressed="${liked}" onclick="L2MAlbums.like(event,'${slug}')"><span class="material-symbols-outlined" aria-hidden="true">favorite</span>${c.likes}</button>` +
        `<button class="soc-btn is-comment" aria-label="${s("comments")}" onclick="L2MAlbums.comment(event,'${slug}')"><span class="material-symbols-outlined" aria-hidden="true">chat_bubble</span>${c.comments}</button>`;
    });
  }
  async function like(e, slug) {
    e.stopPropagation();
    if (!getMe().authenticated) { location.href = "/outpost.goauthentik.io/start?rd=" + location.pathname; return; }
    try {
      const res = await (await fetch(`/api/social/albums/${slug}/like`, { method: "POST" })).json();
      SOC_CACHE[slug] = { likes: res.likes, comments: res.comments };
      if (res.liked) MY_LIKES.add(slug); else MY_LIKES.delete(slug);
      patchSocial();
    } catch (err) { /* réseau : le compteur reste inchangé */ }
  }
  function comment(e, slug) {
    e.stopPropagation();
    location.href = `/album/${slug}#comments`;
  }
  async function loadSocial(authenticated) {
    try {
      const [counts, liked] = await Promise.all([
        fetch("/api/social/counts").then((r) => r.json()),
        authenticated ? fetch("/api/social/my-likes").then((r) => r.json()) : Promise.resolve([]),
      ]);
      SOC_CACHE = counts;
      MY_LIKES = new Set(liked);
      patchSocial();
    } catch (err) { /* compteurs indisponibles : rangées à zéro */ }
  }

  window.L2MAlbums = { init, card, render, loadSocial, patchSocial, like, comment, year, mode, groups };
})();
