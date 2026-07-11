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
    },
  };
  const LANG = () => localStorage.getItem("l2m-lang") || "fr";
  const t = (k) => (T[LANG()] || T.fr)[k] || k;
  const esc = L2M.esc;

  // ── Thème ──
  const themeBtn = document.getElementById("theme");
  function setTheme(x) {
    document.documentElement.dataset.theme = x;
    localStorage.setItem("l2m-theme", x);
    themeBtn.querySelector(".material-symbols-outlined").textContent = x === "dark" ? "dark_mode" : "light_mode";
  }
  setTheme(localStorage.getItem("l2m-theme") || "dark");
  themeBtn.onclick = () => setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");

  // ── Langue ──
  function applyStaticI18n() {
    document.documentElement.lang = LANG();
    document.getElementById("lang-label").textContent = LANG().toUpperCase();
    document.getElementById("t-back").textContent = t("back");
    document.getElementById("t-tool").textContent = t("tool");
    document.getElementById("t-login").textContent = t("login");
  }
  document.getElementById("lang").onclick = () => {
    localStorage.setItem("l2m-lang", LANG() === "fr" ? "en" : "fr");
    applyStaticI18n(); if (DATA) renderProfile();
  };
  function onLangChanged() { applyStaticI18n(); if (DATA) renderProfile(); }

  const USERNAME = decodeURIComponent((location.pathname.split("/u/")[1] || "").replace(/\/$/, ""));
  let DATA = null, ME = { authenticated: false }, activeTab = "publications";

  function albumCard(a) {
    const cover = a.has_cover
      ? `<div class="cv" style="background-image:url('/cover/${a.slug}')"></div>`
      : `<div class="cv"><span class="material-symbols-outlined">album</span></div>`;
    return `<a class="prof-alb" href="/?album=${encodeURIComponent(a.slug)}">
      ${cover}
      <div class="b"><div class="t">${esc(a.title || a.slug)}</div>
        <div class="a">${esc(a.artist || "")}</div></div>
    </a>`;
  }

  function commentItem(c) {
    return `<a class="prof-cmt" href="/?album=${encodeURIComponent(c.slug)}" style="display:block">
      <div class="lnk"><span class="material-symbols-outlined" style="font-size:15px">comment</span>
        ${t("on_album")} <b>${esc(c.album_title)}</b>${c.album_artist ? " · " + esc(c.album_artist) : ""}
        <span class="soc-dim">· ${L2M.timeAgo(c.created_at)}${c.edited_at ? " · " + t("edited") : ""}</span>
        <span class="soc-dim" style="margin-left:auto">▲ ${c.score}</span></div>
      <div class="txt">${esc(c.body)}</div>
    </a>`;
  }

  function tabContent() {
    if (activeTab === "publications") {
      return DATA.publications.length
        ? `<div class="prof-albums">${DATA.publications.map(albumCard).join("")}</div>`
        : `<div class="soc-empty">${t("no_pubs")}</div>`;
    }
    if (activeTab === "comments") {
      return DATA.comments.length
        ? DATA.comments.map(commentItem).join("")
        : `<div class="soc-empty">${t("no_comments")}</div>`;
    }
    return DATA.likes.length
      ? `<div class="prof-albums">${DATA.likes.map(albumCard).join("")}</div>`
      : `<div class="soc-empty">${t("no_likes")}</div>`;
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
            <h1>${esc(p.display_name)}</h1>
            <div class="handle">@${esc(p.username)}${since ? " · " + t("member_since") + " " + since : ""}</div>
            ${p.bio ? `<div class="bio">${esc(p.bio)}</div>` : ""}
          </div>
          ${editBtn}
        </div>
        <div class="prof-edit" id="edit-panel">
          <div class="row" style="gap:14px">
            <label style="flex:1;min-width:180px">${t("pseudo")}
              <input id="e-pseudo" maxlength="40" value="${esc(p.display_name)}"></label>
          </div>
          <label>${t("bio")}
            <textarea id="e-bio" maxlength="500" rows="3">${esc(p.bio)}</textarea></label>
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

    document.querySelectorAll(".prof-tabs button").forEach((b) => b.onclick = () => {
      activeTab = b.dataset.tab;
      document.querySelectorAll(".prof-tabs button").forEach((x) => x.classList.toggle("active", x === b));
      document.getElementById("tab-content").innerHTML = tabContent();
    });

    if (DATA.is_self) wireEdit();
  }

  function wireEdit() {
    const panel = document.getElementById("edit-panel");
    document.getElementById("edit-btn").onclick = () => panel.classList.toggle("open");
    document.getElementById("e-cancel").onclick = () => panel.classList.remove("open");
    document.getElementById("e-save").onclick = async () => {
      const display_name = document.getElementById("e-pseudo").value.trim();
      const bio = document.getElementById("e-bio").value.trim();
      if (!display_name) return;
      const btn = document.getElementById("e-save"); btn.disabled = true;
      try {
        await fetch("/api/social/profile", {
          method: "PUT", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ display_name, bio }),
        }).then((r) => { if (!r.ok) throw 0; });
        DATA.profile.display_name = display_name; DATA.profile.bio = bio;
        renderProfile();
      } catch (e) { btn.disabled = false; }
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
    ME = await L2M.me();
    if (ME.is_moderator) document.getElementById("tool-link").style.display = "";
    // Connecté : menu avatar. Anonyme : boutons langue/thème/connexion.
    const um = document.getElementById("usermenu");
    if (ME.authenticated) {
      ["login", "lang", "theme"].forEach((id) => document.getElementById(id).style.display = "none");
      um.style.display = "";
      L2M.userMenu(um, { username: ME.username, display_name: ME.display_name, avatar: ME.avatar },
        { onChange: (k) => { if (k === "lang") onLangChanged(); } });
    } else {
      um.style.display = "none";
      document.getElementById("login").style.display = "";
    }
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
