/* settings.js — page /settings. Rubrique Notifications : préférences par type
   (nouveaux posts d'un artiste / festival / lieu / utilisateur suivi) et par
   canal (in-app, email). Enregistrement automatique à chaque changement.
   L'email est modélisé mais l'envoi reste désactivé tant que le master-switch
   serveur est faux (Phase 3) — un bandeau l'indique. Conforme RGPD : tout est
   activé par défaut mais désactivable. */
(function () {
  const T = {
    fr: {
      back: "Retour", title: "Paramètres", notifs: "Notifications",
      notifs_sub: "Choisissez ce qui déclenche une notification et par quel canal.",
      col_inapp: "Sur le site", col_email: "Email",
      "grp:new_posts": "Nouveaux posts", "grp:likes": "Likes", "grp:comments": "Commentaires",
      "new_post:artist": "Nouveaux posts d'un artiste / groupe suivi",
      "new_post:festival": "Nouveaux posts d'un festival suivi",
      "new_post:venue": "Nouveaux posts d'un lieu suivi",
      "new_post:user": "Nouveaux posts d'un utilisateur suivi",
      "like:post": "Un utilisateur a aimé votre post",
      "like:comment": "Un utilisateur a aimé votre commentaire",
      "comment:post": "Commentaires d'un utilisateur sous votre post",
      "comment:reply": "Réponse à votre commentaire",
      email_off: "Les notifications par email seront activées prochainement. Vos choix sont enregistrés dès maintenant.",
      saved: "Enregistré", login: "Connectez-vous pour accéder à vos paramètres.", login_btn: "Connexion",
      rgpd: "Vous pouvez désactiver n'importe quelle notification à tout moment. Aucun email ne vous sera envoyé pour une catégorie décochée.",
      see_notifs: "Voir mes notifications",
    },
    en: {
      back: "Back", title: "Settings", notifs: "Notifications",
      notifs_sub: "Choose what triggers a notification and through which channel.",
      col_inapp: "On site", col_email: "Email",
      "grp:new_posts": "New posts", "grp:likes": "Likes", "grp:comments": "Comments",
      "new_post:artist": "New posts from a followed artist / band",
      "new_post:festival": "New posts from a followed festival",
      "new_post:venue": "New posts from a followed venue",
      "new_post:user": "New posts from a followed user",
      "like:post": "Someone liked your post",
      "like:comment": "Someone liked your comment",
      "comment:post": "Comments from a user under your post",
      "comment:reply": "Reply to your comment",
      email_off: "Email notifications will be enabled soon. Your choices are saved now.",
      saved: "Saved", login: "Log in to access your settings.", login_btn: "Log in",
      rgpd: "You can turn off any notification at any time. No email will be sent for an unchecked category.",
      see_notifs: "See my notifications",
    },
  };
  const LANG = () => localStorage.getItem("l2m-lang") || "fr";
  const t = (k) => (T[LANG()] || T.fr)[k] || k;
  let DATA = null;

  function toggle(cat, chan, on) {
    return `<label class="sw" title="${chan}">
      <input type="checkbox" data-cat="${cat}" data-chan="${chan}"${on ? " checked" : ""}>
      <span class="sw-track"><span class="sw-thumb"></span></span></label>`;
  }

  function render() {
    const p = DATA.prefs;
    const emailOff = !DATA.email_enabled;
    const catRow = cat => `
      <div class="set-row">
        <div class="set-label">${t(cat)}</div>
        <div class="set-toggles">
          <div class="set-cell">${toggle(cat, "inapp", p[cat].inapp)}</div>
          <div class="set-cell">${toggle(cat, "email", p[cat].email)}</div>
        </div>
      </div>`;
    // Regroupement en rubriques (nouveaux posts / likes / commentaires).
    const groups = DATA.groups || [{ key: null, keys: DATA.categories }];
    const rows = groups.map(g => `
      ${g.key ? `<div class="set-group-title">${t("grp:" + g.key)}</div>` : ""}
      ${g.keys.map(catRow).join("")}`).join("");
    document.getElementById("content").innerHTML = `
      <div class="card set-card">
        <div class="set-head">
          <h1>${t("title")}</h1>
          <a class="icon-btn" href="/notifications"><span class="material-symbols-outlined">notifications</span> ${t("see_notifs")}</a>
        </div>
        <h2 class="set-sec"><span class="material-symbols-outlined">notifications_active</span> ${t("notifs")}</h2>
        <p class="set-sub">${t("notifs_sub")}</p>
        <div class="set-grid">
          <div class="set-row set-colhead">
            <div class="set-label"></div>
            <div class="set-toggles">
              <div class="set-cell">${t("col_inapp")}</div>
              <div class="set-cell">${t("col_email")}</div>
            </div>
          </div>
          ${rows}
        </div>
        ${emailOff ? `<div class="note set-note"><span class="material-symbols-outlined">schedule_send</span>${t("email_off")}</div>` : ""}
        <div class="note set-rgpd"><span class="material-symbols-outlined">shield</span>${t("rgpd")}</div>
        <div class="set-saved" id="saved"></div>
      </div>`;

    document.querySelectorAll('.set-grid input[type="checkbox"]').forEach(cb => {
      cb.onchange = save;
    });
  }

  async function save() {
    const prefs = {};
    DATA.categories.forEach(cat => prefs[cat] = { inapp: false, email: false });
    document.querySelectorAll('.set-grid input[type="checkbox"]').forEach(cb => {
      prefs[cb.dataset.cat][cb.dataset.chan] = cb.checked;
    });
    try {
      const r = await fetch("/api/social/notif-prefs", { method: "PUT",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ prefs }) });
      if (r.ok) { DATA.prefs = (await r.json()).prefs; flashSaved(); }
    } catch (e) {}
  }
  let savedTimer = null;
  function flashSaved() {
    const el = document.getElementById("saved");
    el.innerHTML = `<span class="material-symbols-outlined">check_circle</span> ${t("saved")}`;
    el.classList.add("show");
    clearTimeout(savedTimer);
    savedTimer = setTimeout(() => el.classList.remove("show"), 1600);
  }

  async function load() {
    document.getElementById("t-back").textContent = t("back");
    await L2M.initHeader({ onLangChange: () => { if (DATA) render(); } });
    const r = await fetch("/api/social/notif-prefs");
    if (!r.ok) {
      document.getElementById("content").innerHTML =
        `<div class="notfound"><span class="material-symbols-outlined">lock</span>
          <div>${t("login")}</div>
          <a class="primary" style="margin-top:14px;text-decoration:none" href="${L2M.loginUrl()}">${t("login_btn")}</a></div>`;
      return;
    }
    DATA = await r.json();
    render();
  }
  load();
})();
