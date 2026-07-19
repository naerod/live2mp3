/* social.js — briques réutilisables : identité, avatars, likes, commentaires.
   Utilisé par la vitrine (modale album) et la page profil. Vanilla JS, aucun
   framework. Thème/langue pilotés par localStorage (l2m-theme / l2m-lang). */
const L2M = (function () {
  const TXT = {
    fr: {
      comments: "Commentaires", write_ph: "Partagez votre avis sur ce concert, les morceaux…",
      publish: "Publier", reply: "Répondre", reply_ph: "Votre réponse…",
      edit: "Modifier", del: "Supprimer", save: "Enregistrer", cancel: "Annuler",
      sort_top: "Top", sort_new: "Récents",
      login_comment: "Connectez-vous pour commenter", login: "Connexion",
      edited: "modifié", deleted_c: "[commentaire supprimé]",
      more_comments: "Voir plus de commentaires", more_replies: "Voir plus de réponses",
      no_comments: "Aucun commentaire pour l'instant. Soyez le premier !",
      confirm_del: "Supprimer ce commentaire ?", like: "J'aime", liked: "Aimé",
      now: "à l'instant", min: "min", h: "h", d: "j", login_like: "Connectez-vous pour aimer",
      view_profile: "Voir le profil", language: "Langue", logout: "Déconnexion",
      theme_dark: "Mode sombre", theme_light: "Mode clair", back: "Retour",
      published_by: "Publié par",
      follow: "Suivre", following: "Suivi", unfollow: "Ne plus suivre",
      login_follow: "Connectez-vous pour suivre",
      notify_on: "Notifications activées", notify_off: "Notifications coupées",
      role_user: "Utilisateur", role_gestionnaire: "Gestionnaire", role_admin: "Admin",
      followers: "abonnés", posts: "posts",
    },
    en: {
      comments: "Comments", write_ph: "Share your thoughts on this show, the tracks…",
      publish: "Post", reply: "Reply", reply_ph: "Your reply…",
      edit: "Edit", del: "Delete", save: "Save", cancel: "Cancel",
      sort_top: "Top", sort_new: "New",
      login_comment: "Log in to comment", login: "Log in",
      edited: "edited", deleted_c: "[comment deleted]",
      more_comments: "Show more comments", more_replies: "Show more replies",
      no_comments: "No comments yet. Be the first!",
      confirm_del: "Delete this comment?", like: "Like", liked: "Liked",
      now: "just now", min: "min", h: "h", d: "d", login_like: "Log in to like",
      view_profile: "View profile", language: "Language", logout: "Log out",
      theme_dark: "Dark mode", theme_light: "Light mode", back: "Back",
      published_by: "Published by",
      follow: "Follow", following: "Following", unfollow: "Unfollow",
      login_follow: "Log in to follow",
      notify_on: "Notifications on", notify_off: "Notifications off",
      role_user: "Member", role_gestionnaire: "Manager", role_admin: "Admin",
      followers: "followers", posts: "posts",
    },
  };
  const LANG = () => localStorage.getItem("l2m-lang") || "fr";
  const t = (k) => (TXT[LANG()] || TXT.fr)[k] || k;

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  function hue(s) { let h = 0; for (const c of String(s || "?")) h = (h * 31 + c.charCodeAt(0)) % 360; return h; }

  function avatar(user, link) {
    const name = (user.display_name || user.username || "?").trim();
    let inner, style = "";
    if (user.avatar) {
      inner = `<img src="/avatar/${encodeURIComponent(user.username)}" alt="" loading="lazy">`;
    } else {
      // Avatar par défaut : pastel, teinte déterministe (initiale du pseudo).
      inner = esc(name.slice(0, 1).toUpperCase() || "?");
      const h = hue(user.username || name);
      style = ` style="background:hsl(${h},32%,74%);color:#2c2e36"`;
    }
    const av = `<span class="soc-avatar"${style}>${inner}</span>`;
    return (link !== false && user.username) ? `<a class="soc-avlink" href="/u/${encodeURIComponent(user.username)}">${av}</a>` : av;
  }

  // Avatar + pseudo cliquables (utilisé pour le posteur d'un album).
  function poster(user, prefix) {
    const name = esc(user.display_name || user.username || "?");
    const inner = `${avatar(user, false)}<span class="soc-poster-name">${name}</span>`;
    const wrap = user.username
      ? `<a class="soc-poster" href="/u/${encodeURIComponent(user.username)}">${inner}</a>`
      : `<span class="soc-poster">${inner}</span>`;
    return prefix ? `<span class="soc-poster-pre">${esc(prefix)}</span>${wrap}` : wrap;
  }

  // Récupère en un appel {username: {display_name, avatar}} pour une liste d'users.
  async function profiles(usernames) {
    const uniq = [...new Set((usernames || []).filter(Boolean))];
    if (!uniq.length) return {};
    try {
      return await api("GET", "/api/social/profiles?u=" + encodeURIComponent(uniq.join(",")));
    } catch (e) { return {}; }
  }

  /* ---------------- Thème & langue (partagés) ---------------- */
  function getTheme() { return document.documentElement.dataset.theme || localStorage.getItem("l2m-theme") || "dark"; }
  function applyTheme(x) { document.documentElement.dataset.theme = x; localStorage.setItem("l2m-theme", x); }
  function getLang() { return localStorage.getItem("l2m-lang") || "fr"; }
  function applyLang(l) { localStorage.setItem("l2m-lang", l); document.documentElement.lang = l; }

  /* ---------------- Fenêtre de choix d'import ---------------- */
  // Visible par tous : les visiteurs et les utilisateurs simples y voient le
  // rôle requis plutôt qu'un bouton absent (les droits réels restent côté API).
  function openImportChoice(rights, tx, opts) {
    const gest = !!(rights.is_gestionnaire || rights.is_admin);
    const anon = !rights.authenticated;
    document.getElementById("imp-choice-ovl")?.remove();
    const ovl = document.createElement("div");
    ovl.id = "imp-choice-ovl";
    ovl.className = "imp-choice-ovl";
    const card = (act, icon, t, d) => `
      <button class="imp-card${gest ? "" : " locked"}" data-act="${act}"${gest ? "" : " disabled"}>
        <span class="material-symbols-outlined imp-card-ic">${icon}</span>
        <span class="imp-card-t">${t}</span>
        <span class="imp-card-d">${d}</span>
        ${gest ? "" : `<span class="imp-card-lock"><span class="material-symbols-outlined">lock</span>${tx.need_role}</span>`}
      </button>`;
    ovl.innerHTML = `
      <div class="imp-choice" role="dialog" aria-modal="true">
        <button class="imp-choice-x icon-btn icon-only" title="${tx.close}"><span class="material-symbols-outlined">close</span></button>
        <h2 class="imp-choice-h">${tx.title}</h2>
        <div class="imp-choice-grid">
          ${card("manual", "upload_file", tx.manual_t, tx.manual_d)}
          ${card("tool", "auto_fix_high", tx.tool_t, tx.tool_d)}
        </div>
        ${gest ? "" : `<div class="imp-choice-note">
          <span class="material-symbols-outlined">info</span>
          <span>${anon ? tx.note_anon : tx.note_user}</span>
          ${anon ? `<a class="primary imp-choice-login" href="/outpost.goauthentik.io/start?rd=${encodeURIComponent(location.pathname)}">${tx.login}</a>` : ""}
        </div>`}
      </div>`;
    document.body.appendChild(ovl);
    requestAnimationFrame(() => ovl.classList.add("show"));
    const close = () => { ovl.classList.remove("show"); setTimeout(() => ovl.remove(), 180); };
    ovl.querySelector(".imp-choice-x").onclick = close;
    ovl.onclick = e => { if (e.target === ovl) close(); };
    document.addEventListener("keydown", function esc(e) {
      if (e.key === "Escape") { close(); document.removeEventListener("keydown", esc); }
    });
    if (gest) ovl.querySelectorAll(".imp-card").forEach(b => {
      b.onclick = () => {
        close();
        if (b.dataset.act === "tool") location.href = "/app";
        else (opts.onImport || (() => { location.href = "/?import=1"; }))();
      };
    });
  }

  /* ---------------- Header unifié ---------------- */
  async function initHeader(opts) {
    opts = opts || {};
    const header = document.querySelector("header");
    if (!header) return {};
    header.innerHTML = `
      <a class="brand" href="/"><img class="brand-icon" src="/static/favicon.svg" alt="" width="24" height="24"> live2mp3</a>
      <div class="tools">
        <button id="import-btn" class="icon-btn"><span class="material-symbols-outlined">library_add</span><span data-hk="import"></span></button>
        <button id="lang" class="icon-btn"><span class="material-symbols-outlined">translate</span><span id="lang-label"></span></button>
        <button id="theme" class="icon-btn icon-only"><span class="material-symbols-outlined"></span></button>
        <a id="login" class="icon-btn" style="display:none"><span class="material-symbols-outlined">login</span><span data-hk="login"></span></a>
        <div id="usermenu" class="usermenu" style="display:none"></div>
      </div>`;

    const HT = {
      fr: { tool: "Outil", import: "Importer", login: "Connexion" },
      en: { tool: "Tool", import: "Import", login: "Log in" },
    };
    // Textes de la fenêtre de choix d'import (partagée par toutes les pages).
    const IT = {
      fr: {
        title: "Importer un album", close: "Fermer",
        manual_t: "Importer manuellement",
        manual_d: "Déposez des MP3 déjà découpés (ou un ZIP) et complétez les informations.",
        tool_t: "Depuis un lien (assisté par IA)",
        tool_d: "Collez le lien d'une captation : découpe, titres et métadonnées proposés automatiquement.",
        need_role: "Réservé aux gestionnaires",
        note_user: "L'import d'albums est réservé aux comptes gestionnaires. Demandez cet accès à un administrateur.",
        note_anon: "L'import d'albums est réservé aux comptes gestionnaires. Connectez-vous avec un compte disposant de ce rôle.",
        login: "Se connecter",
      },
      en: {
        title: "Import an album", close: "Close",
        manual_t: "Import manually",
        manual_d: "Upload already-split MP3s (or a ZIP) and fill in the details.",
        tool_t: "From a link (AI-assisted)",
        tool_d: "Paste a concert link: splitting, titles and metadata are proposed automatically.",
        need_role: "Managers only",
        note_user: "Importing albums is restricted to manager accounts. Ask an administrator for this access.",
        note_anon: "Importing albums is restricted to manager accounts. Log in with an account holding that role.",
        login: "Log in",
      },
    };
    function refreshHeaderTexts() {
      const lang = getLang();
      const tx = HT[lang] || HT.fr;
      header.querySelectorAll("[data-hk]").forEach(el => { el.textContent = tx[el.dataset.hk] || ""; });
      document.getElementById("lang-label").textContent = lang.toUpperCase();
      const themeIcon = document.querySelector("#theme .material-symbols-outlined");
      themeIcon.textContent = getTheme() === "dark" ? "dark_mode" : "light_mode";
    }

    document.getElementById("theme").onclick = () => {
      applyTheme(getTheme() === "dark" ? "light" : "dark");
      refreshHeaderTexts();
    };
    document.getElementById("lang").onclick = () => {
      applyLang(getLang() === "fr" ? "en" : "fr");
      refreshHeaderTexts();
      if (opts.onLangChange) opts.onLangChange(getLang());
    };

    refreshHeaderTexts();

    let meData = { authenticated: false, is_gestionnaire: false };
    try { meData = await fetch("/api/me").then(r => r.json()); } catch (e) {}

    // Le bouton est visible par tous : la fenêtre de choix explique le rôle
    // requis aux visiteurs et aux utilisateurs simples.
    document.getElementById("import-btn").onclick = () => {
      // `opts.rights` permet à la vitrine de refléter le mode « Voir en tant que ».
      const r = (opts.rights ? opts.rights() : null) || meData;
      openImportChoice(r, IT[getLang()] || IT.fr, opts);
    };

    const um = document.getElementById("usermenu");
    if (meData.authenticated) {
      ["login", "lang", "theme"].forEach(id => document.getElementById(id).style.display = "none");
      um.style.display = "";
      const sm = await me();
      const extraItems = opts.extraItems ? (typeof opts.extraItems === "function" ? opts.extraItems(meData) : opts.extraItems) : [];
      userMenu(um, { username: sm.username || meData.username, display_name: sm.display_name, avatar: sm.avatar },
        { extraItems, onChange: k => {
          if (k === "lang") { refreshHeaderTexts(); if (opts.onLangChange) opts.onLangChange(getLang()); }
          if (opts.onMenuChange) opts.onMenuChange(k);
        }});
    } else {
      um.style.display = "none";
      const loginEl = document.getElementById("login");
      loginEl.href = loginUrl();
      loginEl.style.display = "";
    }

    return meData;
  }

  /* ---------------- Menu utilisateur (avatar déroulant) ---------------- */
  // mountEl doit avoir la classe .usermenu. opts.onChange(kind) après thème/langue.
  function userMenu(mountEl, meData, opts) {
    opts = opts || {};
    const uname = meData.username;
    mountEl.innerHTML = `
      <button class="um-trigger" aria-label="menu" aria-haspopup="true">${avatar(meData, false)}</button>
      <div class="um-pop" role="menu">
        <a class="um-head" href="/u/${encodeURIComponent(uname)}">${avatar(meData, false)}
          <div class="um-id"><div class="um-name">${esc(meData.display_name || uname)}</div>
            <div class="um-handle">@${esc(uname)}</div></div></a>
        <a class="um-item" href="/u/${encodeURIComponent(uname)}"><span class="material-symbols-outlined">account_circle</span><span data-k="profile"></span></a>
        <button class="um-item" data-act="lang"><span class="material-symbols-outlined">translate</span><span data-k="lang"></span><span class="um-val" data-k="langval"></span></button>
        <button class="um-item" data-act="theme"><span class="material-symbols-outlined" data-k="themeic"></span><span data-k="theme"></span></button>
        ${(opts.extraItems||[]).map((it,i)=>`<button class="um-item" data-extra="${i}"><span class="material-symbols-outlined">${it.icon}</span><span data-k="extra${i}">${it.label}</span></button>`).join("")}
        <div class="um-sep"></div>
        <a class="um-item danger" href="/api/logout"><span class="material-symbols-outlined">logout</span><span data-k="logout"></span></a>
      </div>`;
    const trigger = mountEl.querySelector(".um-trigger");
    const set = (k, v) => { const el = mountEl.querySelector(`[data-k="${k}"]`); if (el) el.textContent = v; };
    function refresh() {
      const dark = getTheme() === "dark";
      set("profile", t("view_profile")); set("lang", t("language")); set("langval", getLang().toUpperCase());
      set("logout", t("logout")); set("themeic", dark ? "light_mode" : "dark_mode");
      set("theme", dark ? t("theme_light") : t("theme_dark"));
    }
    refresh();
    trigger.onclick = (e) => { e.stopPropagation(); mountEl.classList.toggle("open"); };
    document.addEventListener("click", (e) => { if (!mountEl.contains(e.target)) mountEl.classList.remove("open"); });
    mountEl.querySelector('[data-act="lang"]').onclick = () => {
      applyLang(getLang() === "fr" ? "en" : "fr"); refresh(); if (opts.onChange) opts.onChange("lang");
    };
    mountEl.querySelector('[data-act="theme"]').onclick = () => {
      applyTheme(getTheme() === "dark" ? "light" : "dark"); refresh(); if (opts.onChange) opts.onChange("theme");
    };
    (opts.extraItems||[]).forEach((it,i)=>{
      const btn=mountEl.querySelector(`[data-extra="${i}"]`);
      if(btn) btn.onclick=()=>{ if(it.onclick) it.onclick(); mountEl.classList.remove("open"); };
    });
    mountEl._refreshExtra=function(items){
      items.forEach((it,i)=>{ set("extra"+i, it.label); });
    };
  }

  function userLink(user) {
    const name = esc(user.display_name || user.username);
    return user.username ? `<a class="soc-user" href="/u/${encodeURIComponent(user.username)}">${name}</a>`
      : `<span class="soc-user">${name}</span>`;
  }

  function timeAgo(iso) {
    if (!iso) return "";
    const then = new Date(iso).getTime(); if (isNaN(then)) return "";
    const s = Math.floor((Date.now() - then) / 1000);
    if (s < 60) return t("now");
    if (s < 3600) return `${Math.floor(s / 60)} ${t("min")}`;
    if (s < 86400) return `${Math.floor(s / 3600)} ${t("h")}`;
    if (s < 2592000) return `${Math.floor(s / 86400)} ${t("d")}`;
    return new Date(iso).toLocaleDateString(LANG());
  }

  const loginUrl = () => "/outpost.goauthentik.io/start?rd=" + encodeURIComponent(location.pathname + location.search);

  let _me = null;
  function me() {
    if (!_me) _me = fetch("/api/social/me").then((r) => r.json()).catch(() => ({ authenticated: false }));
    return _me;
  }

  async function api(method, url, body) {
    const opt = { method, headers: {} };
    if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
    const r = await fetch(url, opt);
    if (!r.ok) throw Object.assign(new Error("http " + r.status), { status: r.status });
    return r.json();
  }

  /* ---------------- Bouton like ---------------- */
  async function likeButton(el, slug) {
    const meData = await me();
    const s = await fetch(`/api/social/albums/${slug}`).then((r) => r.json());
    let liked = s.liked, count = s.likes;
    function paint() {
      el.className = "soc-like" + (liked ? " on" : "");
      el.innerHTML = `<span class="material-symbols-outlined">favorite</span>
        <span class="n">${count}</span>`;
      el.title = meData.authenticated ? (liked ? t("liked") : t("like")) : t("login_like");
    }
    paint();
    el.onclick = async () => {
      if (!meData.authenticated) { location.href = loginUrl(); return; }
      el.disabled = true;
      try {
        const r = await api("POST", `/api/social/albums/${slug}/like`);
        liked = r.liked; count = r.likes; paint();
      } catch (e) { /* silencieux */ } finally { el.disabled = false; }
    };
  }

  /* ---------------- Bouton de suivi + cloche (façon X / YouTube) ----------------
     followButton(el, {type, id, label, state, onChange})
     - `state` (optionnel) : {following, notify, followers} déjà connu de la page,
       sinon récupéré via /api/social/follow-state ;
     - le bouton bascule Suivre → Suivi (+/✓) ; une fois suivi, une cloche
       apparaît (notifications on/off) — activer la cloche implique de suivre.
     - `onChange(state)` rappelé après chaque changement (compteur d'abonnés…). */
  async function followButton(el, opts) {
    const { type, id } = opts;
    const label = opts.label || "";
    const meData = await me();
    let st = opts.state;
    if (!st) {
      try {
        st = await api("GET", `/api/social/follow-state?target_type=${encodeURIComponent(type)}&target_id=${encodeURIComponent(id)}`);
      } catch (e) { st = { following: false, notify: false, followers: 0 }; }
    }
    const body = () => ({ target_type: type, target_id: id, target_label: label });

    function paint() {
      const bell = st.following ? `
        <button class="bell-btn${st.notify ? " on" : ""}" data-act="bell"
                title="${esc(st.notify ? t("notify_on") : t("notify_off"))}"
                aria-pressed="${st.notify}">
          <span class="material-symbols-outlined">${st.notify ? "notifications_active" : "notifications_off"}</span>
        </button>` : "";
      el.innerHTML = `
        <button class="follow-btn${st.following ? " following" : ""}" data-act="follow"
                title="${esc(st.following ? t("unfollow") : (meData.authenticated ? t("follow") : t("login_follow")))}">
          <span class="material-symbols-outlined">${st.following ? "check" : "add"}</span>
          <span class="follow-lbl">${st.following ? t("following") : t("follow")}</span>
        </button>${bell}`;
      wire();
    }
    function wire() {
      const fb = el.querySelector('[data-act="follow"]');
      const bb = el.querySelector('[data-act="bell"]');
      fb.onclick = async () => {
        if (!meData.authenticated) { location.href = loginUrl(); return; }
        fb.disabled = true;
        try { st = await api("POST", "/api/social/follow", body()); }
        catch (e) { if (e.status === 401) return (location.href = loginUrl()); }
        paint(); if (opts.onChange) opts.onChange(st);
      };
      if (bb) bb.onclick = async () => {
        bb.disabled = true;
        try { st = await api("PATCH", "/api/social/follow/notify", { ...body(), notify: !st.notify }); }
        catch (e) { bb.disabled = false; return; }
        paint(); if (opts.onChange) opts.onChange(st);
      };
    }
    paint();
    return { get: () => st };
  }

  // Badge de rôle (utilisateur / gestionnaire / admin) — affiché sur les profils.
  function roleBadge(role) {
    if (!role) return "";
    const icon = role === "admin" ? "shield_person"
      : role === "gestionnaire" ? "manage_accounts" : "person";
    return `<span class="role-badge role-${role}"><span class="material-symbols-outlined">${icon}</span>${t("role_" + role)}</span>`;
  }

  const entityHref = (type, id) => `/${type}/${encodeURIComponent(id)}`;

  /* ---------------- Widget commentaires ---------------- */
  /* `opts.coverId` bascule le widget sur le fil d'une pochette au lieu de celui
     de l'album. Les deux peuvent coexister (fil d'album + modale pochette), donc
     rien ici ne doit s'appuyer sur un id global. */
  async function comments(root, slug, opts) {
    const coverId = (opts || {}).coverId || null;
    const scope = coverId ? `&cover_id=${coverId}` : "";
    const meData = await me();
    let sort = "top", offset = 0, total = 0;

    root.classList.add("soc-wrap");
    root.innerHTML = `
      <div class="soc-head">
        <h3>${t("comments")}</h3>
        <div class="soc-sort">
          <button data-sort="top" class="active">${t("sort_top")}</button>
          <button data-sort="new">${t("sort_new")}</button>
        </div>
      </div>
      <div class="soc-compose-slot"></div>
      <div class="soc-list"></div>
      <div class="soc-more-slot"></div>`;

    const list = root.querySelector(".soc-list");
    const moreSlot = root.querySelector(".soc-more-slot");
    const composeSlot = root.querySelector(".soc-compose-slot");

    // Composer racine ou invite de connexion
    if (meData.authenticated) {
      composeSlot.innerHTML = composerHtml(t("write_ph"));
      wireComposer(composeSlot.firstElementChild, async (body) => {
        const c = await api("POST", `/api/social/albums/${slug}/comments`,
                            coverId ? { body, cover_id: coverId } : { body });
        c.reply_count = 0; c.replies = [];
        const emptyEl = list.querySelector(".soc-empty");
        if (emptyEl) emptyEl.remove();
        list.insertAdjacentHTML("afterbegin", commentHtml(c, c.id));
        total++;
      });
    } else {
      composeSlot.innerHTML = `<div class="soc-login-note">
        <span class="material-symbols-outlined">lock</span>${t("login_comment")}
        <a class="icon-btn" href="${loginUrl()}"><span class="material-symbols-outlined">login</span> ${t("login")}</a>
      </div>`;
    }

    root.querySelectorAll(".soc-sort button").forEach((b) => b.onclick = () => {
      if (b.dataset.sort === sort) return;
      sort = b.dataset.sort;
      root.querySelectorAll(".soc-sort button").forEach((x) => x.classList.toggle("active", x === b));
      offset = 0; list.innerHTML = ""; loadPage();
    });

    async function loadPage() {
      const d = await api("GET", `/api/social/albums/${slug}/comments?sort=${sort}&offset=${offset}&limit=20${scope}`);
      total = d.total;
      if (!d.comments.length && offset === 0) {
        list.innerHTML = `<div class="soc-empty">${t("no_comments")}</div>`;
      } else {
        list.insertAdjacentHTML("beforeend", d.comments.map((c) => commentHtml(c, c.id)).join(""));
      }
      offset += d.comments.length;
      moreSlot.innerHTML = offset < total
        ? `<button class="soc-more"><span class="material-symbols-outlined">expand_more</span> ${t("more_comments")}</button>` : "";
      const mt = moreSlot.querySelector(".soc-more");
      if (mt) mt.onclick = loadPage;
    }
    loadPage();

    /* --- rendu --- */
    function commentLikeHtml(c) {
      if (c.deleted) return `<div class="soc-clikes"></div>`;
      const likers = c.likers || [];
      const count = c.likes || 0;
      const liked = c.liked || false;
      const pills = likers.map((u) => {
        if (u.avatar) {
          return `<a class="soc-clika soc-avatar" href="/u/${encodeURIComponent(u.username)}" title="${esc(u.display_name)}"><img src="/avatar/${encodeURIComponent(u.username)}" alt="" loading="lazy"></a>`;
        }
        const h = hue(u.username);
        return `<a class="soc-clika soc-avatar" href="/u/${encodeURIComponent(u.username)}" title="${esc(u.display_name)}" style="background:hsl(${h},32%,74%);color:#2c2e36">${esc((u.display_name || u.username || "?").slice(0, 1).toUpperCase())}</a>`;
      }).join("");
      const extra = count > likers.length ? `<span class="soc-clikes-more">+${count - likers.length}</span>` : "";
      const likerRow = count > 0 ? `<div class="soc-cliker-row">${pills}${extra}</div>` : "";
      const title = meData.authenticated ? (liked ? t("liked") : t("like")) : t("login_like");
      return `<div class="soc-clikes" data-cid="${c.id}">
        ${likerRow}
        <button class="soc-clike-btn${liked ? " on" : ""}" data-act="clike" title="${esc(title)}">
          <span class="material-symbols-outlined">favorite</span>
        </button>
      </div>`;
    }
    function bodyHtml(c) {
      if (c.deleted) return `<div class="soc-body deleted">${t("deleted_c")}</div>`;
      const mention = c.reply_to ? `<span class="soc-mention">@${esc(c.reply_to)}</span> ` : "";
      return `<div class="soc-body">${mention}${esc(c.body)}</div>`;
    }
    function actionsHtml(c) {
      if (c.deleted) return "";
      const parts = [];
      if (meData.authenticated) parts.push(`<button data-act="reply"><span class="material-symbols-outlined">reply</span>${t("reply")}</button>`);
      if (meData.authenticated && c.username === meData.username)
        parts.push(`<button data-act="edit"><span class="material-symbols-outlined">edit</span>${t("edit")}</button>`);
      if (meData.authenticated && (c.username === meData.username || meData.is_moderator))
        parts.push(`<button data-act="del"><span class="material-symbols-outlined">delete</span>${t("del")}</button>`);
      return `<div class="soc-actions">${parts.join("")}</div>`;
    }
    function commentHtml(c, topId, isReply) {
      const byline = c.deleted
        ? `<div class="soc-byline"><span class="soc-dim">${t("deleted_c")}</span></div>`
        : `<div class="soc-byline">${avatar(c)} ${userLink(c)}
             <span class="soc-dim">· ${timeAgo(c.created_at)}${c.edited_at ? " · " + t("edited") : ""}</span></div>`;
      const repliesBlock = !isReply ? `
        <div class="soc-replies" data-top="${c.id}" data-shown="${(c.replies || []).length}" data-count="${c.reply_count || 0}">
          ${(c.replies || []).map((r) => commentHtml(r, c.id, true)).join("")}
        </div>
        <div class="soc-morereplies-slot" data-top="${c.id}">${
          (c.reply_count || 0) > (c.replies || []).length
            ? `<button class="soc-more" data-act="morereplies"><span class="material-symbols-outlined">expand_more</span> ${t("more_replies")}</button>` : ""
        }</div>` : "";
      return `<div class="soc-comment" data-cid="${c.id}" data-top="${topId}" data-user="${esc(c.username || "")}">
        <div class="soc-main">
          ${byline}
          ${bodyHtml(c)}
          <div class="soc-footer">
            ${actionsHtml(c)}
            ${commentLikeHtml(c)}
          </div>
          <div class="soc-sub"></div>
          ${repliesBlock}
        </div>
      </div>`;
    }

    /* --- délégation d'événements --- */
    list.addEventListener("click", async (e) => {
      const btn = e.target.closest("button"); if (!btn) return;
      const node = btn.closest(".soc-comment"); if (!node) return;
      const cid = node.dataset.cid;

      const act = btn.dataset.act;

      // Like commentaire
      if (act === "clike") {
        if (!meData.authenticated) { location.href = loginUrl(); return; }
        const likesEl = btn.closest(".soc-clikes");
        btn.disabled = true;
        try {
          const r = await api("POST", `/api/social/comments/${cid}/like`);
          const tmp = document.createElement("div");
          tmp.innerHTML = commentLikeHtml({ id: parseInt(cid, 10), deleted: false, likes: r.likes, liked: r.liked, likers: r.likers });
          likesEl.replaceWith(tmp.firstElementChild);
        } catch (err) {
          btn.disabled = false;
          if (err.status === 401) location.href = loginUrl();
        }
        return;
      }
      if (act === "reply") return openReply(node);
      if (act === "edit") return openEdit(node);
      if (act === "del") return doDelete(node);
      if (act === "morereplies") return loadMoreReplies(node.querySelector(".soc-replies") || node.closest(".soc-main").querySelector(".soc-replies"), btn);
    });

    function openReply(node) {
      const sub = node.querySelector(".soc-sub");
      if (sub.firstChild) { sub.innerHTML = ""; return; }
      sub.innerHTML = composerHtml(t("reply_ph"), true);
      const form = sub.firstElementChild;
      wireComposer(form, async (body) => {
        const r = await api("POST", `/api/social/albums/${slug}/comments`, { body, parent_id: parseInt(node.dataset.cid, 10) });
        const topId = node.dataset.top;
        const topNode = list.querySelector(`.soc-comment[data-cid="${topId}"]`);
        const rep = topNode.querySelector(".soc-replies");
        rep.insertAdjacentHTML("beforeend", commentHtml(r, topId, true));
        rep.dataset.shown = (parseInt(rep.dataset.shown, 10) + 1);
        sub.innerHTML = "";
      }, () => sub.innerHTML = "");
      form.querySelector("textarea").focus();
    }

    function openEdit(node) {
      const bodyEl = node.querySelector(".soc-body");
      const cur = bodyEl.querySelector(".soc-mention") ? bodyEl.textContent.replace(/^@\S+\s/, "") : bodyEl.textContent;
      const sub = node.querySelector(".soc-sub");
      sub.innerHTML = composerHtml("", true, cur.trim());
      const form = sub.firstElementChild;
      wireComposer(form, async (body) => {
        const r = await api("PATCH", `/api/social/comments/${node.dataset.cid}`, { body });
        const mention = r.reply_to ? `<span class="soc-mention">@${esc(r.reply_to)}</span> ` : "";
        bodyEl.innerHTML = mention + esc(r.body);
        const dim = node.querySelector(".soc-byline .soc-dim");
        if (dim && !dim.textContent.includes(t("edited"))) dim.textContent += " · " + t("edited");
        sub.innerHTML = "";
      }, () => sub.innerHTML = "");
      form.querySelector("textarea").focus();
    }

    async function doDelete(node) {
      if (!confirm(t("confirm_del"))) return;
      try {
        await api("DELETE", `/api/social/comments/${node.dataset.cid}`);
        node.querySelector(".soc-main .soc-byline").innerHTML = `<span class="soc-dim">${t("deleted_c")}</span>`;
        node.querySelector(".soc-body").outerHTML = `<div class="soc-body deleted">${t("deleted_c")}</div>`;
        const f = node.querySelector(".soc-footer"); if (f) f.remove();
      } catch (e) { /* silencieux */ }
    }

    async function loadMoreReplies(rep, btn) {
      const top = rep.dataset.top, shown = parseInt(rep.dataset.shown, 10);
      const d = await api("GET", `/api/social/comments/${top}/replies?offset=${shown}&limit=20`);
      rep.insertAdjacentHTML("beforeend", d.replies.map((r) => commentHtml(r, top, true)).join(""));
      rep.dataset.shown = shown + d.replies.length;
      if (parseInt(rep.dataset.shown, 10) >= parseInt(rep.dataset.count, 10)) btn.remove();
    }
  }

  /* --- composer réutilisable --- */
  function composerHtml(ph, small, value) {
    return `<form class="soc-composer${small ? " small" : ""}">
      <textarea placeholder="${esc(ph)}" maxlength="4000" required>${esc(value || "")}</textarea>
      <div class="row">
        <button type="submit" class="primary">${t(small && value !== undefined && value !== "" ? "save" : (small ? "publish" : "publish"))}</button>
        ${small ? `<button type="button" class="cancel">${t("cancel")}</button>` : ""}
      </div>
    </form>`;
  }
  function wireComposer(form, onSubmit, onCancel) {
    const ta = form.querySelector("textarea");
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const body = ta.value.trim(); if (!body) return;
      const btn = form.querySelector('button[type="submit"]'); btn.disabled = true;
      try { await onSubmit(body); if (!form.classList.contains("small")) ta.value = ""; }
      catch (err) { btn.disabled = false; return; }
      btn.disabled = false;
    });
    const cancel = form.querySelector(".cancel");
    if (cancel && onCancel) cancel.onclick = onCancel;
  }

  /* ---------------- Autocomplétion à valeur canonique ----------------
     Le champ texte n'est qu'une aide à la saisie : la valeur retenue est
     l'**id** de l'entrée choisie dans la liste. Taper « dijon » sans choisir
     ne vaut rien — c'est ce qui force un formalisme unique. Le champ est donc
     remis à l'état choisi (ou vidé) dès qu'il perd le focus.

     mount(el, {endpoint, value:{id,label}, placeholder, icon, onPick})
     → { get() → {id,label}, clear() } */
  function autocomplete(el, opts) {
    const { endpoint, placeholder = "", icon = "", onPick } = opts;
    // `allowFree` : autorise une valeur saisie qui ne figure pas dans la liste
    // (id vide, libellé = texte tapé). Sert aux festivals, dont la liste
    // s'auto-construit — un festival inédit se crée en texte libre.
    const allowFree = !!opts.allowFree;
    let picked = opts.value && (opts.value.id || (allowFree && opts.value.label))
      ? { ...opts.value } : { id: "", label: "" };
    let items = [], active = -1, seq = 0, timer = null;

    el.classList.add("ac");
    el.innerHTML = `
      <div class="ac-field">
        ${icon ? `<span class="material-symbols-outlined ac-ic">${icon}</span>` : ""}
        <input type="text" autocomplete="off" role="combobox" aria-expanded="false"
               aria-autocomplete="list" placeholder="${esc(placeholder)}">
        <button type="button" class="ac-clear" tabindex="-1" aria-label="${esc(t("cancel"))}"
                style="display:${picked.id ? "flex" : "none"}">
          <span class="material-symbols-outlined">close</span></button>
      </div>
      <ul class="ac-list" role="listbox" hidden></ul>`;
    const input = el.querySelector("input");
    const list = el.querySelector(".ac-list");
    const clearBtn = el.querySelector(".ac-clear");
    input.value = picked.label || "";

    const close = () => { list.hidden = true; input.setAttribute("aria-expanded", "false"); active = -1; };
    function commit(entry) {
      picked = entry ? { id: entry.id, label: entry.label } : { id: "", label: "" };
      input.value = picked.label;
      clearBtn.style.display = (picked.id || picked.label) ? "flex" : "none";
      close();
      if (onPick) onPick(picked);
    }
    // Valeur libre (allowFree) : conserve le texte tapé, id vide.
    function commitFree() {
      const v = input.value.trim();
      picked = { id: "", label: v };
      clearBtn.style.display = v ? "flex" : "none";
      close();
      if (onPick) onPick(picked);
    }
    function render() {
      if (!items.length) return close();
      list.innerHTML = items.map((it, i) => `
        <li role="option" aria-selected="${i === active}" class="${i === active ? "on" : ""}" data-i="${i}">
          ${it.picture ? `<img src="${esc(it.picture)}" alt="" loading="lazy">`
                       : `<span class="material-symbols-outlined">${icon || "search"}</span>`}
          <span class="ac-l">${esc(it.label)}</span>
          ${it.hint ? `<span class="ac-h">${esc(it.hint)}</span>` : ""}
        </li>`).join("");
      list.hidden = false;
      input.setAttribute("aria-expanded", "true");
      list.querySelectorAll("li").forEach((li) => {
        // mousedown : précède le blur, qui sinon annulerait la sélection.
        li.addEventListener("mousedown", (e) => { e.preventDefault(); commit(items[+li.dataset.i]); });
      });
    }
    async function search(q) {
      const my = ++seq;
      try {
        const r = await fetch(`${endpoint}?q=${encodeURIComponent(q)}`);
        const data = r.ok ? await r.json() : [];
        if (my !== seq) return;            // réponse d'une frappe périmée
        items = data; active = data.length ? 0 : -1; render();
      } catch (e) { if (my === seq) { items = []; close(); } }
    }

    input.addEventListener("input", () => {
      // Canonique : éditer une valeur choisie l'annule. Libre : on ne touche pas
      // au texte tapé (il devient la valeur au blur).
      if (picked.id && input.value !== picked.label && !allowFree) commit(null);
      const q = input.value.trim();
      clearTimeout(timer);
      if (q.length < 2) { items = []; return close(); }
      timer = setTimeout(() => search(q), 250);
    });
    input.addEventListener("keydown", (e) => {
      if (list.hidden || !items.length) {
        // Entrée sur un champ libre sans liste ouverte = valider le texte tapé.
        if (e.key === "Enter" && allowFree) { e.preventDefault(); commitFree(); }
        return;
      }
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        active = (active + (e.key === "ArrowDown" ? 1 : items.length - 1)) % items.length;
        render();
      } else if (e.key === "Enter") { e.preventDefault(); commit(items[active]); }
      else if (e.key === "Escape") close();
    });
    // Sans allowFree : texte non validé = pas de valeur, on restaure l'état
    // canonique. Avec allowFree : on garde le texte tapé comme valeur libre.
    input.addEventListener("blur", () => setTimeout(() => {
      if (allowFree) {
        const v = input.value.trim();
        if (v !== picked.label) commitFree(); else close();
      } else { input.value = picked.label; close(); }
    }, 120));
    clearBtn.onclick = () => { commit(null); input.focus(); };

    return { get: () => ({ ...picked }), clear: () => commit(null) };
  }

  return {
    t, esc, avatar, userLink, poster, profiles, timeAgo, loginUrl, me,
    likeButton, followButton, roleBadge, entityHref, comments, userMenu,
    autocomplete, LANG,
    getTheme, applyTheme, getLang, applyLang, initHeader,
  };
})();
