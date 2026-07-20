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
  let DATA = null, ME = { authenticated: false }, activeTab = "publications", pubSort = "date";

  function albumCard(a) {
    const cover = a.has_cover
      ? `<div class="cv" style="background-image:url('/cover/${a.slug}')"></div>`
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
    const media = isUser
      ? L2M.avatar({ username: it.id, display_name: it.label, avatar: it.avatar }, false)
      : `<span class="fr-ic"><span class="material-symbols-outlined">${FOLLOW_ICON[it.type] || "tag"}</span></span>`;
    const role = isUser && it.role ? " " + L2M.roleBadge(it.role) : "";
    return `<div class="follow-row">
      <a class="fr-main" href="${href}">
        ${media}
        <span class="fr-txt"><span class="fr-name">${esc(it.label)}${role}</span></span>
      </a>
      <div class="follow-wrap fr-act" data-type="${esc(it.type)}" data-id="${esc(it.id)}"
           data-label="${esc(it.label)}" data-following="${it.viewer_following ? 1 : 0}"
           data-notify="${it.viewer_notify ? 1 : 0}"></div>
    </div>`;
  }

  function followingContent() {
    const groups = DATA.following_list || {};
    const blocks = GRP_ORDER
      .filter((tt) => (groups[tt] || []).length)
      .map((tt) => `<div class="follow-grp"><h3 class="follow-grp-h">${t("grp_" + tt)}
        <span class="cnt">${groups[tt].length}</span></h3>
        <div class="follow-list">${groups[tt].map(followRow).join("")}</div></div>`);
    return blocks.length ? blocks.join("") : `<div class="soc-empty">${t("no_following")}</div>`;
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

  function renderProfile() {
    const p = DATA.profile, c = DATA.counts;
    const since = p.created_at ? new Date(p.created_at).toLocaleDateString(LANG(), { year: "numeric", month: "long" }) : "";
    const editBtn = DATA.is_self
      ? `<button class="icon-btn" id="edit-btn"><span class="material-symbols-outlined">edit</span> ${t("edit")}</button>` : "";
    document.getElementById("content").innerHTML = `
      <div class="card">
        <div class="prof-hero">
          <div class="prof-avatar-edit">${L2M.avatar({ username: p.username, display_name: p.display_name, avatar: p.avatar }, false)}
            ${DATA.is_self ? `<label class="cam" title="${t("change_photo")}"><span class="material-symbols-outlined">photo_camera</span>
              <input type="file" accept="image/*" id="avatar-input" hidden></label>` : ""}</div>
          <div class="prof-id">
            <h1>${esc(p.display_name)} ${L2M.roleBadge(p.role)}</h1>
            <div class="handle">@${esc(p.username)}${since ? " · " + t("member_since") + " " + since : ""}</div>
            ${p.bio ? `<div class="bio">${esc(p.bio)}</div>` : ""}
            ${chips(p)}
          </div>
          ${DATA.is_self ? editBtn : `<div class="follow-wrap" id="prof-follow"></div>`}
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
        <button data-tab="following" class="${activeTab === "following" ? "active" : ""}">${t("following")}<span class="cnt">${c.following}</span></button>
        <button data-tab="followers" class="${activeTab === "followers" ? "active" : ""}">${t("followers")}<span class="cnt">${c.followers}</span></button>
      </div>
      <div id="tab-content">${tabContent()}</div>`;

    document.querySelectorAll(".prof-tabs button").forEach((b) => b.onclick = () => {
      activeTab = b.dataset.tab;
      document.querySelectorAll(".prof-tabs button").forEach((x) => x.classList.toggle("active", x === b));
      document.getElementById("tab-content").innerHTML = tabContent();
      wirePubSort();
      wireFollowRows();
    });
    wirePubSort();
    wireFollowRows();

    if (DATA.is_self) wireEdit();
    else {
      const slot = document.getElementById("prof-follow");
      const f = DATA.follow || { followers: 0, following: false, notify: false };
      if (slot) L2M.followButton(slot, {
        type: "user", id: p.username, label: p.display_name, state: f,
      });
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
