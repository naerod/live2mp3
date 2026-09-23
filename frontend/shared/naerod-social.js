/* ════════════════════════════════════════════════════════════════════════
   naerod-social.js — briques d'identité partagées par l'écosystème naerod.

   NE PAS ÉDITER DANS UN SITE : la source est
   /home/claude/workspace/shared/naerod/naerod-social.js, propagée par sync.sh.

   Le module ne sait rien du site qui l'héberge : routes, textes, thème et
   langue arrivent par `Naerod.init(config)`. Aucune couleur n'est écrite ici
   ni dans la feuille associée — tout passe par les variables --nrd-*.
   ════════════════════════════════════════════════════════════════════════ */
const Naerod = (function () {
  /* ── Textes du module (indépendants de l'i18n du site hôte) ────────── */
  const TXT = {
    fr: {
      view_profile: "Voir le profil", language: "Langue", logout: "Déconnexion",
      theme_dark: "Mode sombre", theme_light: "Mode clair",
      notifications: "Notifications", settings: "Paramètres",
      follow: "Suivre", following: "Suivi", unfollow: "Ne plus suivre",
      login_follow: "Connectez-vous pour suivre",
      notify_on: "Notifications activées", notify_off: "Notifications coupées",
      role_user: "Simple utilisateur", role_gestionnaire: "Gestionnaire", role_admin: "Admin",
      role_supporter: "Soutien",
      role_artiste: "Artiste", role_organisation: "Organisation",
      now: "à l'instant", min: "min", h: "h", d: "j",
      notif_empty: "Aucune notification pour l'instant.",
      notif_new: "Nouveau post de", notif_mark_all: "Tout marquer comme lu",
      notif_see_all: "Voir toutes les notifications",
      notif_like_post: "a aimé votre publication",
      notif_like_comment: "a aimé votre commentaire",
      notif_comment_post: "a commenté votre publication",
      notif_reply_comment: "a répondu à votre commentaire",
      notif_admin_action: "Action administrateur",
      notif_role_now: "Vous êtes désormais",
      notif_role_now_neutral: "Votre rôle est désormais",
      notif_role_by: "par",
      notif_new_date: "annonce une nouvelle date",
      notif_reminder: "c'est bientôt !",
      notif_new_album: "un album live est disponible",
    },
    en: {
      view_profile: "View profile", language: "Language", logout: "Log out",
      theme_dark: "Dark mode", theme_light: "Light mode",
      notifications: "Notifications", settings: "Settings",
      follow: "Follow", following: "Following", unfollow: "Unfollow",
      login_follow: "Log in to follow",
      notify_on: "Notifications on", notify_off: "Notifications off",
      role_user: "User", role_gestionnaire: "Manager", role_admin: "Admin",
      role_supporter: "Supporter",
      role_artiste: "Artist", role_organisation: "Organisation",
      now: "just now", min: "min", h: "h", d: "d",
      notif_empty: "No notifications yet.",
      notif_new: "New post from", notif_mark_all: "Mark all as read",
      notif_see_all: "See all notifications",
      notif_like_post: "liked your post",
      notif_like_comment: "liked your comment",
      notif_comment_post: "commented on your post",
      notif_reply_comment: "replied to your comment",
      notif_admin_action: "Administrator action",
      notif_role_now: "You are now",
      notif_role_now_neutral: "Your role is now",
      notif_role_by: "by",
      notif_new_date: "announces a new date",
      notif_reminder: "coming up soon!",
      notif_new_album: "a live album is available",
    },
  };

  /* ── Configuration ─────────────────────────────────────────────────── */
  const CFG = {
    apiBase: "",
    avatarBase: "/avatar",
    profileHref: (u) => "/u/" + encodeURIComponent(u),
    notificationsHref: "/notifications",
    settingsHref: "/settings",
    logoutHref: "/api/logout",
    loginUrl: () => "/outpost.goauthentik.io/start?rd=" +
      encodeURIComponent(location.pathname + location.search),
    lang: () => document.documentElement.lang || "fr",
    setLang: () => {},
    theme: () => document.documentElement.dataset.theme || "dark",
    setTheme: () => {},
    // Cible d'une notification. Le site hôte décide (album, occurrence, profil…).
    itemHref: (n) => (n.slug ? "/album/" + encodeURIComponent(n.slug) : "/"),
  };
  function init(cfg) { Object.assign(CFG, cfg || {}); _me = null; return Naerod; }

  /* ── Registre des panels flottants (profil, notifs…) ───────────────── */
  // Chaque panel s'enregistre ici à la création. Avant de s'ouvrir, il appelle
  // _closeAll(self) pour fermer les autres — évite les superpositions.
  const _panels = [];
  function _registerPanel(closeFn) { _panels.push(closeFn); }
  function _closeAll(except) { _panels.forEach((fn) => { if (fn !== except) fn(); }); }

  const t = (k) => (TXT[CFG.lang()] || TXT.fr)[k] || (TXT.fr[k] || k);

  /* ── Utilitaires ───────────────────────────────────────────────────── */
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  // Teinte déterministe pour l'avatar par défaut : le même pseudo garde la même
  // couleur d'un site à l'autre — c'est ce qui rend l'identité reconnaissable.
  function hue(s) {
    let h = 0;
    for (const c of String(s || "?")) h = (h * 31 + c.charCodeAt(0)) % 360;
    return h;
  }

  async function api(method, url, body) {
    const opt = { method, headers: {} };
    if (body !== undefined) {
      opt.headers["Content-Type"] = "application/json";
      opt.body = JSON.stringify(body);
    }
    const r = await fetch(CFG.apiBase + url, opt);
    if (!r.ok) throw Object.assign(new Error("http " + r.status), { status: r.status });
    return r.json();
  }

  let _me = null;
  function me() {
    if (!_me) _me = fetch(CFG.apiBase + "/api/social/me")
      .then((r) => r.json()).catch(() => ({ authenticated: false }));
    return _me;
  }

  function timeAgo(iso) {
    if (!iso) return "";
    const then = new Date(iso).getTime();
    if (isNaN(then)) return "";
    const s = Math.floor((Date.now() - then) / 1000);
    if (s < 60) return t("now");
    if (s < 3600) return `${Math.floor(s / 60)} ${t("min")}`;
    if (s < 86400) return `${Math.floor(s / 3600)} ${t("h")}`;
    if (s < 2592000) return `${Math.floor(s / 86400)} ${t("d")}`;
    return new Date(iso).toLocaleDateString(CFG.lang() === "en" ? "en-GB" : "fr-FR");
  }

  /* ── Identité : avatar, lien, posteur ──────────────────────────────── */
  function avatar(user, link) {
    const name = (user.display_name || user.username || "?").trim();
    let inner, style = "";
    if (user.avatar) {
      inner = `<img src="${CFG.avatarBase}/${encodeURIComponent(user.username)}" alt="" loading="lazy">`;
    } else {
      inner = esc(name.slice(0, 1).toUpperCase() || "?");
      const h = hue(user.username || name);
      style = ` style="background:hsl(${h},32%,74%);color:#2c2e36"`;
    }
    const av = `<span class="soc-avatar"${style}>${inner}</span>`;
    return (link !== false && user.username)
      ? `<a class="soc-avlink" href="${CFG.profileHref(user.username)}">${av}</a>` : av;
  }

  function userLink(user) {
    const name = esc(user.display_name || user.username);
    return user.username
      ? `<a class="soc-user" href="${CFG.profileHref(user.username)}">${name}</a>`
      : `<span class="soc-user">${name}</span>`;
  }

  function poster(user, prefix) {
    const name = esc(user.display_name || user.username || "?");
    const inner = `${avatar(user, false)}<span class="soc-poster-name">${name}</span>`;
    const wrap = user.username
      ? `<a class="soc-poster" href="${CFG.profileHref(user.username)}">${inner}</a>`
      : `<span class="soc-poster">${inner}</span>`;
    return prefix ? `<span class="soc-poster-pre">${esc(prefix)}</span>${wrap}` : wrap;
  }

  async function profiles(usernames) {
    const uniq = [...new Set((usernames || []).filter(Boolean))];
    if (!uniq.length) return {};
    try {
      return await api("GET", "/api/social/profiles?u=" + encodeURIComponent(uniq.join(",")));
    } catch (e) { return {}; }
  }

  function roleBadge(role) {
    // Tout le monde porte un badge : l'absence de rôle = simple utilisateur.
    role = role || "user";
    const icon = role === "admin" ? "shield_person"
      : role === "gestionnaire" ? "manage_accounts"
      : role === "supporter" ? "crown"
      : role === "artiste" ? "artist"
      : role === "organisation" ? "festival" : "person";
    return `<span class="role-badge role-${esc(role)}">` +
      `<span class="material-symbols-outlined">${icon}</span>${t("role_" + role)}</span>`;
  }

  /* ── Menu de profil (le bouton d'avatar n'est jamais un simple lien) ──
     mountEl doit porter la classe .usermenu.
     opts.extraItems : [{icon, label, onclick}] — entrées propres au site
     (« Mon journal » sur show-me, par exemple).
     opts.onChange(kind) est rappelé après un changement de thème ou de langue. */
  function userMenu(mountEl, meData, opts) {
    opts = opts || {};
    const uname = meData.username;
    const href = CFG.profileHref(uname);
    mountEl.innerHTML = `
      <button class="um-trigger" aria-label="menu" aria-haspopup="true">${avatar(meData, false)}</button>
      <div class="um-pop" role="menu">
        <a class="um-head" href="${href}">${avatar(meData, false)}
          <div class="um-id"><div class="um-name">${esc(meData.display_name || uname)}</div>
            <div class="um-handle">@${esc(uname)}</div></div></a>
        <a class="um-item" href="${href}"><span class="material-symbols-outlined">account_circle</span><span data-k="profile"></span></a>
        <div class="um-sep"></div>
        <a class="um-item" href="${CFG.settingsHref}"><span class="material-symbols-outlined">settings</span><span data-k="settings"></span></a>
        <button class="um-item" data-act="lang"><span class="material-symbols-outlined">translate</span><span data-k="lang"></span><span class="um-val" data-k="langval"></span></button>
        <button class="um-item" data-act="theme"><span class="material-symbols-outlined" data-k="themeic"></span><span data-k="theme"></span></button>
        <div class="um-sep"></div>
        ${(opts.extraItems || []).map((it, i) => it.href
          ? `<a class="um-item" href="${it.href}"><span class="material-symbols-outlined">${it.icon}</span><span data-k="extra${i}">${esc(it.label)}</span></a>`
          : `<button class="um-item" data-extra="${i}"><span class="material-symbols-outlined">${it.icon}</span><span data-k="extra${i}">${esc(it.label)}</span></button>`).join("")}
        <a class="um-item danger" href="${typeof CFG.logoutHref === 'function' ? CFG.logoutHref() : CFG.logoutHref}"><span class="material-symbols-outlined">logout</span><span data-k="logout"></span></a>
      </div>`;
    const trigger = mountEl.querySelector(".um-trigger");
    const set = (k, v) => {
      const el = mountEl.querySelector(`[data-k="${k}"]`);
      if (el) el.textContent = v;
    };
    function refresh() {
      const dark = CFG.theme() === "dark";
      set("profile", t("view_profile"));
      set("settings", t("settings"));
      set("lang", t("language"));
      set("langval", CFG.lang().toUpperCase());
      set("themeic", dark ? "light_mode" : "dark_mode");
      set("theme", dark ? t("theme_light") : t("theme_dark"));
      set("logout", t("logout"));
      if (opts.labels) opts.labels().forEach((lbl, i) => set("extra" + i, lbl));
    }
    refresh();
    mountEl._refresh = refresh;

    const _closeProfile = () => mountEl.classList.remove("open");
    _registerPanel(_closeProfile);
    trigger.onclick = (e) => {
      e.stopPropagation();
      const opening = !mountEl.classList.contains("open");
      if (opening) _closeAll(_closeProfile);
      mountEl.classList.toggle("open", opening);
    };
    document.addEventListener("click", (e) => {
      if (!mountEl.contains(e.target)) mountEl.classList.remove("open");
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") mountEl.classList.remove("open");
    });
    mountEl.querySelector('[data-act="lang"]').onclick = () => {
      CFG.setLang(CFG.lang() === "fr" ? "en" : "fr");
      refresh();
      if (opts.onChange) opts.onChange("lang");
    };
    mountEl.querySelector('[data-act="theme"]').onclick = () => {
      CFG.setTheme(CFG.theme() === "dark" ? "light" : "dark");
      refresh();
      if (opts.onChange) opts.onChange("theme");
    };
    (opts.extraItems || []).forEach((it, i) => {
      const btn = mountEl.querySelector(`[data-extra="${i}"]`);
      if (btn && it.onclick) btn.onclick = () => { it.onclick(); mountEl.classList.remove("open"); };
    });
    return mountEl;
  }

  /* ── Bouton Suivre + cloche ────────────────────────────────────────── */
  async function followButton(el, opts) {
    const { type, id } = opts;
    const label = opts.label || "";
    const meData = await me();
    let st = opts.state;
    if (!st) {
      try {
        st = await api("GET", "/api/social/follow-state?target_type=" +
          encodeURIComponent(type) + "&target_id=" + encodeURIComponent(id));
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
        if (!meData.authenticated) { location.href = CFG.loginUrl(); return; }
        fb.disabled = true;
        try { st = await api("POST", "/api/social/follow", body()); }
        catch (e) { if (e.status === 401) return (location.href = CFG.loginUrl()); }
        paint();
        if (opts.onChange) opts.onChange(st);
      };
      if (bb) bb.onclick = async () => {
        bb.disabled = true;
        try {
          st = await api("PATCH", "/api/social/follow/notify", { ...body(), notify: !st.notify });
        } catch (e) { bb.disabled = false; return; }
        paint();
        if (opts.onChange) opts.onChange(st);
      };
    }
    paint();
    return { get: () => st, repaint: paint };
  }

  /* ── Notifications ─────────────────────────────────────────────────── */
  const NOTIF_ICON = {
    artist: "artist", festival: "festival", venue: "location_on",
    user: "person", event: "confirmation_number",
  };
  const NOTIF_TYPE_ICON = {
    like_post: "favorite", like_comment: "favorite",
    comment_post: "chat_bubble", reply_comment: "reply",
    new_date: "event_upcoming", reminder: "alarm", new_album: "album",
  };

  function notifReason(n) {
    const who = `<b>${esc(n.reason_label)}</b>`;
    switch (n.type) {
      case "like_post": return `${who} ${t("notif_like_post")}`;
      case "like_comment": return `${who} ${t("notif_like_comment")}`;
      case "comment_post": return `${who} ${t("notif_comment_post")}`;
      case "reply_comment": return `${who} ${t("notif_reply_comment")}`;
      case "new_date": return `${who} ${t("notif_new_date")}`;
      case "reminder": return `${who} — ${t("notif_reminder")}`;
      case "new_album": return `${who} : ${t("notif_new_album")}`;
      default: return `${t("notif_new")} ${who}`;
    }
  }

  function notifItem(n) {
    if (n.type === "role_grant" || n.type === "role_revoke") return notifRoleItem(n);
    const icon = NOTIF_TYPE_ICON[n.type] || NOTIF_ICON[n.reason_type] || "notifications";
    return `<a class="notif-item${n.read ? "" : " unread"}" href="${CFG.itemHref(n)}" data-id="${n.id}">
      <span class="notif-ic material-symbols-outlined">${icon}</span>
      <span class="notif-body">
        <span class="notif-reason">${notifReason(n)}</span>
        <span class="notif-post">${esc(n.title)}${n.subtitle ? " · " + esc(n.subtitle) : ""}</span>
        <span class="notif-time">${timeAgo(n.created_at)}</span>
      </span>
      ${n.read ? "" : `<span class="notif-dot"></span>`}
    </a>`;
  }

  function notifRoleItem(n) {
    const grant = n.type === "role_grant";
    const href = n.username ? CFG.profileHref(n.username) : "/";
    const icon = grant ? "workspace_premium" : "remove_moderator";
    const roleLabel = t("role_" + (n.reason_id || "user"));
    const msg = grant
      ? `${t("notif_role_now")} <b>${esc(roleLabel)}</b> 🎉`
      : `${t("notif_role_now_neutral")} <b>${esc(roleLabel)}</b>`;
    return `<a class="notif-item notif-admin${grant ? " notif-grant" : ""}${n.read ? "" : " unread"}" href="${href}" data-id="${n.id}">
      <span class="notif-ic material-symbols-outlined">${icon}</span>
      <span class="notif-body">
        <span class="notif-admin-tag"><span class="material-symbols-outlined">shield_person</span>${t("notif_admin_action")}</span>
        <span class="notif-reason">${msg}</span>
        <span class="notif-post">${t("notif_role_by")} <b>${esc(n.reason_label)}</b></span>
        <span class="notif-time">${timeAgo(n.created_at)}</span>
      </span>
      ${n.read ? "" : `<span class="notif-dot"></span>`}
    </a>`;
  }

  async function markRead(ids) {
    const payload = ids === true ? { all: true } : { ids };
    try { return (await api("POST", "/api/social/notifications/read", payload)).unread; }
    catch (e) { return null; }
  }

  /* Cloche du header : compteur de non-lus + menu des récentes. */
  async function notifBell(mountEl) {
    let unread = 0, loaded = false;
    mountEl.innerHTML = `
      <button class="nb-trigger icon-btn icon-only" aria-haspopup="true"
              aria-label="${esc(t("notifications"))}" title="${esc(t("notifications"))}">
        <span class="material-symbols-outlined">notifications</span>
      </button>
      <div class="nb-pop" role="menu" hidden></div>`;
    const trigger = mountEl.querySelector(".nb-trigger");
    const pop = mountEl.querySelector(".nb-pop");

    function paintBadge() {
      mountEl.querySelector(".nb-badge")?.remove();
      if (unread > 0) trigger.insertAdjacentHTML("beforeend",
        `<span class="nb-badge">${unread > 99 ? "99+" : unread}</span>`);
    }
    async function refreshCount() {
      try { unread = (await api("GET", "/api/social/notifications/count")).unread || 0; }
      catch (e) { unread = 0; }
      paintBadge();
    }
    async function loadPop() {
      pop.innerHTML = `
        <div class="nb-head"><b>${t("notifications")}</b>
          <button class="nb-markall">${t("notif_mark_all")}</button></div>
        <div class="nb-list"><div class="soc-empty">…</div></div>
        <a class="nb-all" href="${CFG.notificationsHref}">${t("notif_see_all")}</a>`;
      let d = { items: [] };
      try { d = await api("GET", "/api/social/notifications?limit=8"); } catch (e) {}
      const list = pop.querySelector(".nb-list");
      list.innerHTML = (d.items || []).length
        ? d.items.map(notifItem).join("")
        : `<div class="soc-empty">${t("notif_empty")}</div>`;
      pop.querySelector(".nb-markall").onclick = async (e) => {
        e.preventDefault(); e.stopPropagation();
        const u = await markRead(true);
        if (u !== null) unread = u;
        paintBadge();
        list.querySelectorAll(".notif-item").forEach((el) => {
          el.classList.remove("unread");
          el.querySelector(".notif-dot")?.remove();
        });
      };
      // Ouvrir une notification la marque lue — sans attendre la navigation.
      list.querySelectorAll(".notif-item").forEach((el) => el.addEventListener("click", () => {
        if (el.classList.contains("unread")) markRead([Number(el.dataset.id)]);
      }));
      loaded = true;
    }

    const _closeNotifs = () => { mountEl.classList.remove("open"); pop.hidden = true; };
    _registerPanel(_closeNotifs);
    trigger.onclick = async (e) => {
      e.stopPropagation();
      const opening = !mountEl.classList.contains("open");
      if (opening) _closeAll(_closeNotifs);
      mountEl.classList.toggle("open", opening);
      pop.hidden = !opening;
      if (opening && !loaded) await loadPop();
    };
    document.addEventListener("click", (e) => {
      if (!mountEl.contains(e.target)) { mountEl.classList.remove("open"); pop.hidden = true; }
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { mountEl.classList.remove("open"); pop.hidden = true; }
    });

    await refreshCount();
    // Rafraîchissement passif : discret, et suspendu quand l'onglet est caché.
    setInterval(() => { if (!document.hidden) refreshCount(); }, 60000);
    return { refreshCount, reload: () => { loaded = false; } };
  }

  return {
    init, t, esc, hue, api, me, timeAgo,
    avatar, userLink, poster, profiles, roleBadge,
    userMenu, followButton,
    notifBell, notifItem, notifReason, markRead,
    get cfg() { return CFG; },
  };
})();
