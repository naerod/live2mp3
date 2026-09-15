/* profile.js — page profil publique /u/{username}.
   Sections : Publications (albums publiés), Commentaires, Likes (albums aimés).
   Édition du pseudo / bio / avatar réservée au propriétaire du profil. */
(function () {
  const T = {
    fr: {
      albums: "Albums", tool: "Outil", login: "Connexion", logout: "Déconnexion",
      publications: "Publications", comments: "Commentaires", likes: "Likes",
      edit: "Modifier le profil", save: "Enregistrer", cancel: "Annuler",
      pseudo: "Pseudo", bio: "Bio", change_photo: "Changer la photo", remove_photo: "Retirer",
      member_since: "Membre depuis", no_pubs: "Aucune publication.",
      no_comments: "Aucun commentaire.", no_likes: "Aucun album aimé.",
      not_found: "Utilisateur introuvable.", on_album: "sur", edited: "modifié",
      titres: "titres", back: "Retour",
      following: "Abonnements", followers: "Abonnés",
      grp_artist: "Artistes", grp_user: "Utilisateurs",
      grp_festival: "Festivals", grp_venue: "Lieux",
      no_following: "Aucun abonnement.", no_followers: "Aucun abonné.",
      its_you: "C'est vous",
      change_role: "Changer le rôle", role_none: "Sans rôle",
      role_picker_title: "Attribuer un rôle", role_current: "Actuel",
      r_user: "Utilisateur", r_gestionnaire: "Gestionnaire", r_admin: "Administrateur",
      rd_user: "Accès de base (téléchargement).",
      rd_gestionnaire: "Peut importer et gérer les albums.",
      rd_admin: "Gestionnaire + gestion des rôles.",
      confirm_role: "Attention, êtes-vous sûr de vouloir attribuer le rôle « {r} » à cet utilisateur ?",
      yes: "Oui", no: "Non", role_err: "Action impossible. Réessayez.",
      city: "Ville", artist: "Artiste/groupe favori",
      city_ph: "Commencez à taper : Dijon…", artist_ph: "Commencez à taper : Coldplay…",
      pick_hint: "Choisissez une entrée dans la liste",
      save_err: "Enregistrement impossible. Réessayez.",
      sort_by: "Trier", sort_date: "Date du concert", sort_new: "Publication",
    },
    en: {
      albums: "Albums", tool: "Tool", login: "Log in", logout: "Log out",
      publications: "Publications", comments: "Comments", likes: "Likes",
      edit: "Edit profile", save: "Save", cancel: "Cancel",
      pseudo: "Nickname", bio: "Bio", change_photo: "Change photo", remove_photo: "Remove",
      member_since: "Member since", no_pubs: "No publications yet.",
      no_comments: "No comments yet.", no_likes: "No liked albums yet.",
      not_found: "User not found.", on_album: "on", edited: "edited",
      titres: "tracks", back: "Back",
      following: "Following", followers: "Followers",
      grp_artist: "Artists", grp_user: "Users",
      grp_festival: "Festivals", grp_venue: "Venues",
      no_following: "Not following anyone yet.", no_followers: "No followers yet.",
      its_you: "That's you",
      change_role: "Change role", role_none: "No role",
      role_picker_title: "Assign a role", role_current: "Current",
      r_user: "Member", r_gestionnaire: "Manager", r_admin: "Administrator",
      rd_user: "Basic access (downloads).",
      rd_gestionnaire: "Can import and manage albums.",
      rd_admin: "Manager + role management.",
      confirm_role: "Warning: are you sure you want to assign the role “{r}” to this user?",
      yes: "Yes", no: "No", role_err: "Action failed. Please try again.",
      city: "City", artist: "Favourite artist/band",
      sort_by: "Sort", sort_date: "Concert date", sort_new: "Published",
      city_ph: "Start typing: Dijon…", artist_ph: "Start typing: Coldplay…",
      pick_hint: "Pick an entry from the list",
      save_err: "Could not save. Please try again.",
    },
  };
  const LANG = () => localStorage.getItem("l2m-lang") || "fr";
  const t = (k) => (T[LANG()] || T.fr)[k] || k;
  const esc = L2M.esc;

  // Thème appliqué dans le <head> ; toggles gérés par le header unifié (L2M.initHeader).

  // ── Langue ──
  function applyStaticI18n() {
    document.documentElement.lang = LANG();
    const ll = document.getElementById("lang-label");
    if (ll) ll.textContent = LANG().toUpperCase();
    document.getElementById("t-back").textContent = t("back");
  }
  function onLangChanged() { applyStaticI18n(); if (DATA) renderProfile(); }

  const USERNAME = decodeURIComponent((location.pathname.split("/u/")[1] || "").replace(/\/$/, ""));
  let DATA = null, ME = { authenticated: false }, activeTab = "publications", pubSort = "imported";

  function albumCard(a) {
    const cover = a.has_cover
      ? `<div class="cv is-load"><img class="cv-img" loading="lazy" decoding="async" alt=""
           src="/cover/${a.slug}?v=${a.cover_v||0}&w=320"
           srcset="/cover/${a.slug}?v=${a.cover_v||0}&w=320 1x, /cover/${a.slug}?v=${a.cover_v||0}&w=640 2x"
           onload="this.parentNode.classList.remove('is-load');this.classList.add('rdy')"></div>`
      : `<div class="cv"><span class="material-symbols-outlined">album</span></div>`;
    return `<a class="prof-alb" href="/album/${encodeURIComponent(a.slug)}">
      ${cover}
      <div class="b"><div class="t">${esc(a.title || a.slug)}</div>
        <div class="a">${esc(a.artist || "")}</div></div>
    </a>`;
  }

  function commentItem(c) {
    return `<a class="prof-cmt" href="/album/${encodeURIComponent(c.slug)}#comments" style="display:block">
      <div class="lnk"><span class="material-symbols-outlined" style="font-size:15px">comment</span>
        ${t("on_album")} <b>${esc(c.album_title)}</b>${c.album_artist ? " · " + esc(c.album_artist) : ""}
        <span class="soc-dim">· ${L2M.timeAgo(c.created_at)}${c.edited_at ? " · " + t("edited") : ""}</span>
        <span class="soc-dim" style="margin-left:auto">▲ ${c.score}</span></div>
      <div class="txt">${esc(c.body)}</div>
    </a>`;
  }

  // ── Abonnements / Abonnés ──
  const FOLLOW_ICON = { artist: "artist", festival: "festival", venue: "location_on", user: "person" };
  const GRP_ORDER = ["artist", "user", "festival", "venue"];

  // Une ligne : media + libellé (+ badge rôle) cliquable, puis slot bouton Suivre/cloche.
  function followRow(it) {
    const isUser = it.type === "user";
    const href = isUser ? `/u/${encodeURIComponent(it.id)}` : L2M.entityHref(it.type, it.id);
    let media;
    if (isUser) {
      media = L2M.avatar({ username: it.id, display_name: it.label, avatar: it.avatar }, false);
    } else {
      // Artiste : icône de repli + `data-artist-id` pour hydrater la photo Deezer.
      const attr = it.type === "artist" ? ` data-artist-id="${esc(it.id)}"` : "";
      media = `<span class="fr-ic"${attr}><span class="material-symbols-outlined">${FOLLOW_ICON[it.type] || "tag"}</span></span>`;
    }
    return `<div class="follow-row">
      <a class="fr-main" href="${href}">
        ${media}
        <span class="fr-txt"><span class="fr-name">${esc(it.label)}</span></span>
      </a>
      <div class="follow-wrap fr-act" data-type="${esc(it.type)}" data-id="${esc(it.id)}"
           data-label="${esc(it.label)}" data-following="${it.viewer_following ? 1 : 0}"
           data-notify="${it.viewer_notify ? 1 : 0}"></div>
    </div>`;
  }

  // Groupes repliés (persisté entre visites) : Set des types repliés.
  const COLLAPSE_KEY = "l2m-prof-collapsed";
  function loadCollapsed() {
    try { return new Set(JSON.parse(localStorage.getItem(COLLAPSE_KEY) || "[]")); }
    catch (e) { return new Set(); }
  }
  let collapsedGrps = loadCollapsed();
  function saveCollapsed() {
    try { localStorage.setItem(COLLAPSE_KEY, JSON.stringify([...collapsedGrps])); } catch (e) {}
  }

  function followingContent() {
    const groups = DATA.following_list || {};
    const blocks = GRP_ORDER
      .filter((tt) => (groups[tt] || []).length)
      .map((tt) => {
        const collapsed = collapsedGrps.has(tt);
        return `<div class="follow-grp${collapsed ? " collapsed" : ""}" data-grp="${tt}">
          <button type="button" class="follow-grp-h" aria-expanded="${!collapsed}">
            <span class="material-symbols-outlined grp-chevron">expand_more</span>
            <span class="grp-title">${t("grp_" + tt)}</span>
            <span class="cnt">${groups[tt].length}</span>
          </button>
          <div class="follow-list">${groups[tt].map(followRow).join("")}</div></div>`;
      });
    return blocks.length ? blocks.join("") : `<div class="soc-empty">${t("no_following")}</div>`;
  }

  // Récupère les photos d'artistes (Deezer) et remplace les icônes de repli.
  async function hydrateArtistPics() {
    const els = [...document.querySelectorAll(".fr-ic[data-artist-id]")];
    const ids = [...new Set(els.map((e) => e.dataset.artistId))];
    if (!ids.length) return;
    let pics = {};
    try {
      pics = await (await fetch("/api/social/artist-pics?ids=" + encodeURIComponent(ids.join(",")))).json();
    } catch (e) { return; }
    els.forEach((e) => {
      const p = pics[e.dataset.artistId];
      if (p) { e.classList.add("has-pic"); e.innerHTML = `<img src="${esc(p)}" alt="" loading="lazy">`; }
    });
  }

  // Pli/dépli des groupes d'abonnements au clic sur l'en-tête.
  function wireFollowGroups() {
    document.querySelectorAll(".follow-grp .follow-grp-h").forEach((h) => {
      h.onclick = () => {
        const grp = h.closest(".follow-grp");
        const tt = grp.dataset.grp;
        const collapsed = grp.classList.toggle("collapsed");
        h.setAttribute("aria-expanded", String(!collapsed));
        if (collapsed) collapsedGrps.add(tt); else collapsedGrps.delete(tt);
        saveCollapsed();
      };
    });
  }

  function followersContent() {
    const list = DATA.followers_list || [];
    return list.length
      ? `<div class="follow-list">${list.map(followRow).join("")}</div>`
      : `<div class="soc-empty">${t("no_followers")}</div>`;
  }

  // Monte le bouton Suivre/cloche sur chaque ligne (état = relation du visiteur).
  function wireFollowRows() {
    document.querySelectorAll(".fr-act").forEach((slot) => {
      const type = slot.dataset.type, id = slot.dataset.id;
      // On ne se suit pas soi-même : pas de bouton sur sa propre ligne.
      if (type === "user" && ME.authenticated && id === ME.username) {
        slot.outerHTML = `<span class="fr-you">${t("its_you")}</span>`;
        return;
      }
      L2M.followButton(slot, {
        type, id, label: slot.dataset.label,
        state: {
          following: slot.dataset.following === "1",
          notify: slot.dataset.notify === "1",
          followers: 0,
        },
      });
    });
  }

  function sortedPubs() {
    const arr = [...DATA.publications];
    if (pubSort === "imported") arr.sort((a, b) => (b.imported_at || "").localeCompare(a.imported_at || ""));
    else arr.sort((a, b) => (b.date || "").localeCompare(a.date || ""));
    return arr;
  }

  function tabContent() {
    if (activeTab === "publications") {
      const sort = `<div class="prof-sort"><span class="material-symbols-outlined" style="font-size:14px;color:var(--muted)">sort</span><select id="pub-sort"><option value="date"${pubSort==="date"?' selected':''}>${t("sort_date")}</option><option value="imported"${pubSort==="imported"?' selected':''}>${t("sort_new")}</option></select></div>`;
      return DATA.publications.length
        ? sort + `<div class="prof-albums">${sortedPubs().map(albumCard).join("")}</div>`
        : `<div class="soc-empty">${t("no_pubs")}</div>`;
    }
    if (activeTab === "comments") {
      return DATA.comments.length
        ? DATA.comments.map(commentItem).join("")
        : `<div class="soc-empty">${t("no_comments")}</div>`;
    }
    if (activeTab === "following") return followingContent();
    if (activeTab === "followers") return followersContent();
    return DATA.likes.length
      ? `<div class="prof-albums">${DATA.likes.map(albumCard).join("")}</div>`
      : `<div class="soc-empty">${t("no_likes")}</div>`;
  }

  function chips(p) {
    const out = [];
    if (p.city) out.push(`<span class="prof-chip"><span class="material-symbols-outlined">location_on</span>${esc(p.city)}</span>`);
    if (p.artist) out.push(`<span class="prof-chip"><span class="material-symbols-outlined">music_note</span>${esc(p.artist)}</span>`);
    return out.length ? `<div class="prof-chips">${out.join("")}</div>` : "";
  }

  function wirePubSort() {
    const sel = document.getElementById("pub-sort");
    if (sel) sel.onchange = () => {
      pubSort = sel.value;
      document.getElementById("tab-content").innerHTML = tabContent();
      wirePubSort();
    };
  }

  // Rôles gérables et leur icône Material (mêmes que le badge de rôle).
  const ROLE_ICON = { user: "person", gestionnaire: "manage_accounts", admin: "shield_person" };
  // Rôles proposés dans le sélecteur : le rôle « admin » (applicatif) n'est
  // gérable que par un super-administrateur.
  function pickableRoles() {
    return ME.is_superadmin ? ["user", "gestionnaire", "admin"] : ["user", "gestionnaire"];
  }
  // Peut-on gérer le rôle de ce profil ? (gestionnaire des rôles, pas soi-même,
  // et — sauf superadmin — pas un profil déjà admin.)
  function canManageRole(p) {
    return ME.is_admin && !DATA.is_self && (ME.is_superadmin || p.role !== "admin");
  }

  // Badge de rôle : simple pour tous, cliquable (ouvre le sélecteur) si gérable.
  function roleControl(p) {
    const badge = L2M.roleBadge(p.role);
    if (!canManageRole(p)) return badge;
    const inner = badge || `<span class="role-badge role-user"><span class="material-symbols-outlined">person</span>${t("role_none")}</span>`;
    return `<button type="button" class="role-badge-btn" id="role-badge-btn" title="${esc(t("change_role"))}">
      ${inner}<span class="material-symbols-outlined role-caret">expand_more</span></button>`;
  }

  // Sélecteur de rôle (modale) : les 3 rôles, l'actuel marqué. Résout le rôle
  // choisi (différent de l'actuel), ou null si annulé.
  function openRolePicker(current) {
    return new Promise((resolve) => {
      const ov = document.createElement("div");
      ov.className = "modal-ov";
      const opt = (r) => {
        const isCur = r === current;
        return `<button type="button" class="role-opt${isCur ? " current" : ""}" data-role="${r}"${isCur ? " disabled" : ""}>
          <span class="role-opt-ic material-symbols-outlined">${ROLE_ICON[r]}</span>
          <span class="role-opt-txt"><span class="role-opt-name">${t("r_" + r)}</span>
            <span class="role-opt-desc">${t("rd_" + r)}</span></span>
          ${isCur ? `<span class="role-opt-cur">${t("role_current")}</span>` : `<span class="material-symbols-outlined role-opt-go">chevron_right</span>`}
        </button>`;
      };
      ov.innerHTML = `<div class="modal-box role-picker" role="dialog" aria-modal="true">
        <h3 class="modal-title">${t("role_picker_title")}</h3>
        <div class="role-opts">${pickableRoles().map(opt).join("")}</div>
        <div class="modal-actions"><button class="icon-btn" data-a="close">${t("cancel")}</button></div>
      </div>`;
      const close = (v) => { ov.remove(); document.removeEventListener("keydown", onKey); resolve(v); };
      const onKey = (e) => { if (e.key === "Escape") close(null); };
      ov.addEventListener("click", (e) => { if (e.target === ov) close(null); });
      ov.querySelector('[data-a="close"]').onclick = () => close(null);
      ov.querySelectorAll(".role-opt:not(.current)").forEach((b) =>
        b.onclick = () => close(b.dataset.role));
      document.addEventListener("keydown", onKey);
      document.body.appendChild(ov);
    });
  }

  function wireRoleBadge() {
    const btn = document.getElementById("role-badge-btn");
    if (!btn) return;
    btn.onclick = async () => {
      const chosen = await openRolePicker(DATA.profile.role || "user");
      if (!chosen) return;
      const ok = await confirmDialog(t("confirm_role").replace("{r}", t("r_" + chosen)));
      if (!ok) return;
      try {
        const r = await fetch(`/api/social/users/${encodeURIComponent(DATA.profile.username)}/role`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ role: chosen }),
        });
        if (!r.ok) throw new Error();
        DATA.profile.role = (await r.json()).role;
        renderProfile();
      } catch (e) { alert(t("role_err")); }
    };
  }

  // Modale de confirmation Oui/Non (promise<bool>), stylée (pas de confirm() natif).
  function confirmDialog(message) {
    return new Promise((resolve) => {
      const ov = document.createElement("div");
      ov.className = "modal-ov";
      ov.innerHTML = `<div class="modal-box" role="alertdialog" aria-modal="true">
        <div class="modal-ic"><span class="material-symbols-outlined">warning</span></div>
        <p class="modal-msg">${esc(message)}</p>
        <div class="modal-actions">
          <button class="icon-btn" data-a="no">${t("no")}</button>
          <button class="primary" data-a="yes">${t("yes")}</button>
        </div></div>`;
      const close = (v) => { ov.remove(); document.removeEventListener("keydown", onKey); resolve(v); };
      const onKey = (e) => { if (e.key === "Escape") close(false); };
      ov.addEventListener("click", (e) => { if (e.target === ov) close(false); });
      ov.querySelector('[data-a="no"]').onclick = () => close(false);
      ov.querySelector('[data-a="yes"]').onclick = () => close(true);
      document.addEventListener("keydown", onKey);
      document.body.appendChild(ov);
      ov.querySelector('[data-a="yes"]').focus();
    });
  }

  function renderProfile() {
    const p = DATA.profile, c = DATA.counts;
    const since = p.created_at ? new Date(p.created_at).toLocaleDateString(LANG(), { year: "numeric", month: "long" }) : "";
    // Bouton « Modifier le profil » dans la barre haute (comme « Gérer » sur un album).
    const topEdit = document.getElementById("edit-btn");
    if (topEdit) {
      topEdit.style.display = DATA.is_self ? "" : "none";
      const lbl = topEdit.querySelector("#t-edit"); if (lbl) lbl.textContent = t("edit");
    }
    document.getElementById("content").innerHTML = `
      <div class="card">
        <div class="prof-hero">
          <div class="prof-avatar-edit">${L2M.avatar({ username: p.username, display_name: p.display_name, avatar: p.avatar }, false)}
            ${DATA.is_self ? `<label class="cam" title="${t("change_photo")}"><span class="material-symbols-outlined">photo_camera</span>
              <input type="file" accept="image/*" id="avatar-input" hidden></label>` : ""}</div>
          <div class="prof-id">
            <h1>${esc(p.display_name)} ${roleControl(p)}</h1>
            <div class="handle">@${esc(p.username)}${since ? " · " + t("member_since") + " " + since : ""}</div>
            ${p.bio ? `<div class="bio">${esc(p.bio)}</div>` : ""}
            ${chips(p)}
            <div class="prof-stats">
              <button type="button" class="prof-stat${activeTab === "following" ? " active" : ""}" data-go="following"><b>${c.following}</b> ${t("following").toLowerCase()}</button>
              <button type="button" class="prof-stat${activeTab === "followers" ? " active" : ""}" data-go="followers"><b>${c.followers}</b> ${t("followers").toLowerCase()}</button>
            </div>
          </div>
          ${DATA.is_self ? "" : `<div class="follow-wrap" id="prof-follow"></div>`}
        </div>
        <div class="prof-edit" id="edit-panel">
          <div class="row" style="gap:14px">
            <label style="flex:1;min-width:180px">${t("pseudo")}
              <input id="e-pseudo" maxlength="40" value="${esc(p.display_name)}"></label>
          </div>
          <label>${t("bio")}
            <textarea id="e-bio" maxlength="500" rows="3">${esc(p.bio)}</textarea></label>
          <div class="row" style="gap:14px;align-items:flex-start">
            <label style="flex:1;min-width:200px">${t("city")}
              <div id="e-city"></div></label>
            <label style="flex:1;min-width:200px">${t("artist")}
              <div id="e-artist"></div></label>
          </div>
          <div class="row" id="e-err" style="display:none;color:var(--like);font-size:13px"></div>
          <div class="row">
            <button class="primary" id="e-save">${t("save")}</button>
            <button class="icon-btn" id="e-cancel">${t("cancel")}</button>
            ${p.avatar ? `<button class="icon-btn" id="e-rmavatar" style="margin-left:auto"><span class="material-symbols-outlined">no_photography</span> ${t("remove_photo")}</button>` : ""}
          </div>
        </div>
      </div>

      <div class="prof-tabs">
        <button data-tab="publications" class="${activeTab === "publications" ? "active" : ""}">${t("publications")}<span class="cnt">${c.publications}</span></button>
        <button data-tab="comments" class="${activeTab === "comments" ? "active" : ""}">${t("comments")}<span class="cnt">${c.comments}</span></button>
        <button data-tab="likes" class="${activeTab === "likes" ? "active" : ""}">${t("likes")}<span class="cnt">${c.likes}</span></button>
      </div>
      <div id="tab-content">${tabContent()}</div>`;

    // Bascule de section, partagée par les onglets et la ligne de stats (Twitter-like).
    const switchTab = (name) => {
      activeTab = name;
      document.querySelectorAll(".prof-tabs button").forEach((x) => x.classList.toggle("active", x.dataset.tab === name));
      document.querySelectorAll(".prof-stat").forEach((s) => s.classList.toggle("active", s.dataset.go === name));
      document.getElementById("tab-content").innerHTML = tabContent();
      wirePubSort();
      wireFollowRows();
      wireFollowGroups();
      hydrateArtistPics();
    };
    document.querySelectorAll(".prof-tabs button").forEach((b) => b.onclick = () => switchTab(b.dataset.tab));
    document.querySelectorAll(".prof-stat").forEach((s) => s.onclick = () => switchTab(s.dataset.go));
    wirePubSort();
    wireFollowRows();
    wireFollowGroups();
    hydrateArtistPics();

    if (DATA.is_self) wireEdit();
    else {
      const slot = document.getElementById("prof-follow");
      const f = DATA.follow || { followers: 0, following: false, notify: false };
      if (slot) L2M.followButton(slot, {
        type: "user", id: p.username, label: p.display_name, state: f,
      });
      wireRoleBadge();
    }
  }

  function wireEdit() {
    const panel = document.getElementById("edit-panel");
    const p = DATA.profile;
    const err = document.getElementById("e-err");
    document.getElementById("edit-btn").onclick = () => panel.classList.toggle("open");
    document.getElementById("e-cancel").onclick = () => panel.classList.remove("open");

    const city = L2M.autocomplete(document.getElementById("e-city"), {
      endpoint: "/api/social/suggest/cities", icon: "location_on",
      placeholder: t("city_ph"), value: { id: p.city_id, label: p.city },
    });
    const artist = L2M.autocomplete(document.getElementById("e-artist"), {
      endpoint: "/api/social/suggest/artists", icon: "music_note",
      placeholder: t("artist_ph"), value: { id: p.artist_id, label: p.artist },
    });

    document.getElementById("e-save").onclick = async () => {
      const display_name = document.getElementById("e-pseudo").value.trim();
      const bio = document.getElementById("e-bio").value.trim();
      if (!display_name) return;
      const btn = document.getElementById("e-save"); btn.disabled = true;
      err.style.display = "none";
      try {
        const r = await fetch("/api/social/profile", {
          method: "PUT", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            display_name, bio,
            city_id: city.get().id, artist_id: artist.get().id,
          }),
        });
        if (!r.ok) throw new Error(r.status === 422 ? t("pick_hint") : t("save_err"));
        const saved = await r.json();
        Object.assign(DATA.profile, {
          display_name, bio,
          city_id: saved.city_id, city: saved.city,
          artist_id: saved.artist_id, artist: saved.artist,
        });
        renderProfile();
      } catch (e) {
        btn.disabled = false;
        err.textContent = e.message || t("save_err");
        err.style.display = "";
      }
    };
    const input = document.getElementById("avatar-input");
    if (input) input.onchange = async () => {
      if (!input.files[0]) return;
      const fd = new FormData(); fd.append("file", input.files[0]);
      const r = await fetch("/api/social/profile/avatar", { method: "POST", body: fd });
      if (r.ok) { DATA.profile.avatar = true; renderProfile(); }
      else alert("Upload impossible (format/taille).");
    };
    const rm = document.getElementById("e-rmavatar");
    if (rm) rm.onclick = async () => {
      const r = await fetch("/api/social/profile/avatar", { method: "DELETE" });
      if (r.ok) { DATA.profile.avatar = false; renderProfile(); }
    };
  }

  async function load() {
    applyStaticI18n();
    await L2M.initHeader({onLangChange:()=>onLangChanged()});
    ME = await L2M.me();
    const r = await fetch(`/api/social/users/${encodeURIComponent(USERNAME)}`);
    if (!r.ok) {
      document.getElementById("content").innerHTML =
        `<div class="notfound"><span class="material-symbols-outlined">person_off</span>${t("not_found")}</div>`;
      return;
    }
    DATA = await r.json();
    document.title = `live2mp3 — ${DATA.profile.display_name}`;
    renderProfile();
  }
  load();
})();
