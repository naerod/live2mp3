/* drafts.js — page /app/drafts : imports commencés mais jamais terminés.
   Un brouillon n'apparaît nulle part ailleurs (la vitrine filtre les projets
   sans média rendu) : cette page est le seul point d'entrée pour les reprendre
   ou faire le ménage. Clic sur la ligne = reprise dans l'éditeur. */
(function () {
  const T = {
    fr: {
      back: "Retour", title: "Brouillons",
      sub: "Imports commencés mais jamais terminés. Cliquez sur une ligne pour reprendre là où vous vous êtes arrêté.",
      empty_t: "Aucun brouillon", empty_d: "Tous vos imports sont allés au bout.",
      denied_t: "Réservé aux gestionnaires",
      denied_d: "Cette page liste les imports en cours des gestionnaires.",
      login_btn: "Connexion",
      tracks: "pistes", no_date: "Date inconnue", unknown: "Inconnu",
      del_title: "Supprimer ce brouillon",
      del_denied: "Vous ne pouvez supprimer que vos propres brouillons",
      confirm: (t) => `Supprimer définitivement le brouillon « ${t} » ?\n\nLe téléchargement et la détection déjà effectués seront perdus.`,
      del_err: "Échec de la suppression",
      since: "depuis", today: "aujourd'hui", day: "j", month: "mois", year: "an",
      stage_download: "Téléchargé", stage_waveform: "Forme d'onde",
      stage_ai_markers: "Coupes détectées", stage_render: "Rendu",
      stage_tags: "Tags", stage_artwork: "Pochette", stage_disc: "Disque",
      stage_none: "À peine commencé",
      q_title: "Rendus en cours", q_sub: "File d'attente des albums en cours de création. Glissez une ligne pour changer l'ordre.",
      q_running: "En cours", q_paused: "En pause", q_queued: "En attente",
      q_pause: "Mettre en pause", q_resume: "Reprendre", q_cancel: "Arrêter ce rendu",
      q_confirm: (t) => `Arrêter le rendu de « ${t} » ?\n\nLes pistes déjà encodées seront supprimées.`,
      q_err: "Action impossible",
    },
    en: {
      back: "Back", title: "Drafts",
      sub: "Imports that were started but never finished. Click a row to pick up where you left off.",
      empty_t: "No drafts", empty_d: "All your imports made it to the end.",
      denied_t: "Managers only",
      denied_d: "This page lists managers' in-progress imports.",
      login_btn: "Log in",
      tracks: "tracks", no_date: "Unknown date", unknown: "Unknown",
      del_title: "Delete this draft",
      del_denied: "You can only delete your own drafts",
      confirm: (t) => `Permanently delete the draft "${t}"?\n\nThe download and detection already done will be lost.`,
      del_err: "Delete failed",
      since: "for", today: "today", day: "d", month: "mo", year: "y",
      stage_download: "Downloaded", stage_waveform: "Waveform",
      stage_ai_markers: "Cuts detected", stage_render: "Rendered",
      stage_tags: "Tags", stage_artwork: "Cover", stage_disc: "Disc",
      stage_none: "Barely started",
    },
  };
  const LANG = () => localStorage.getItem("l2m-lang") || "fr";
  const t = (k) => (T[LANG()] || T.fr)[k] || k;
  const esc = (s) => L2M.esc(s == null ? "" : String(s));

  // Ancienneté depuis la dernière action réelle sur le brouillon. Au-delà de
  // 7 / 30 jours la pastille change de couleur (cf. .warn / .old).
  function ageBadge(iso) {
    if (!iso) return { txt: "", cls: "" };
    const then = new Date(iso).getTime();
    if (!then) return { txt: "", cls: "" };
    const days = Math.floor((Date.now() - then) / 86400000);
    if (days <= 0) return { txt: t("today"), cls: "" };
    let txt;
    if (days < 30) txt = `${t("since")} ${days} ${t("day")}`;
    else if (days < 365) txt = `${t("since")} ${Math.floor(days / 30)} ${t("month")}`;
    else txt = `${t("since")} ${Math.floor(days / 365)} ${t("year")}`;
    return { txt, cls: days >= 30 ? "old" : days >= 7 ? "warn" : "" };
  }

  function ownerHtml(o) {
    if (!o) return `<span class="dr-owner">${esc(t("unknown"))}</span>`;
    // avatar() rend l'initiale colorée quand l'utilisateur n'a pas d'image.
    return `<span class="dr-owner">${L2M.avatar(o, false)}<span>${esc(o.display_name)}</span></span>`;
  }

  function rowHtml(d) {
    const age = ageBadge(d.updated_at);
    const stage = t("stage_" + (d.stage || "none"));
    const bits = [];
    bits.push(`<span class="dr-meta-i"><span class="material-symbols-outlined">event</span>${esc(d.date || t("no_date"))}</span>`);
    if (d.tracks) bits.push(`<span class="dr-meta-i"><span class="material-symbols-outlined">queue_music</span>${d.tracks} ${esc(t("tracks"))}</span>`);
    bits.push(`<span class="dr-meta-i"><span class="material-symbols-outlined">timeline</span>${esc(stage)}</span>`);
    return `<div class="dr-row" data-slug="${esc(d.slug)}" role="button" tabindex="0">
      <span class="dr-main">
        <span class="dr-title">${esc(d.title || d.slug)}</span>
        <span class="dr-artist">${esc(d.artist)}</span>
        <span class="dr-meta">${bits.join("")}</span>
      </span>
      ${ownerHtml(d.owner)}
      <span class="dr-side">
        ${age.txt ? `<span class="dr-age ${age.cls}"><span class="material-symbols-outlined">schedule</span>${esc(age.txt)}</span>` : ""}
      </span>
      <button class="dr-del" data-del="${esc(d.slug)}" ${d.can_delete ? "" : "disabled"}
              title="${esc(d.can_delete ? t("del_title") : t("del_denied"))}">
        <span class="material-symbols-outlined">delete</span>
      </button>
    </div>`;
  }

  function denied(anon) {
    document.getElementById("content").innerHTML = `
      <div class="notfound">
        <span class="material-symbols-outlined">lock</span>
        <div style="font-size:15px;font-weight:600;color:var(--fg);margin-bottom:4px">${esc(t("denied_t"))}</div>
        <div>${esc(t("denied_d"))}</div>
        ${anon ? `<a class="primary" style="margin-top:14px;text-decoration:none" href="${L2M.loginUrl()}">${esc(t("login_btn"))}</a>` : ""}
      </div>`;
  }

  // --- File des rendus en cours ---------------------------------------
  // Section volontairement absente du DOM quand la file est vide : c'est un
  // état transitoire, pas une rubrique permanente de la page.
  let queuePoll = null;

  function queueRow(it) {
    const st = it.state === "running" ? t("q_running")
             : it.state === "paused" ? t("q_paused") : t("q_queued");
    const tags = (it.formats || []).map(f =>
      `<span class="q-tag q-tag-${f}">${f}</span>`).join("");
    const active = it.state === "running" || it.state === "paused";
    const pauseIco = it.state === "paused" ? "play_arrow" : "pause";
    const pauseLbl = it.state === "paused" ? t("q_resume") : t("q_pause");
    return `<div class="q-row q-${it.state}" data-slug="${esc(it.slug)}" draggable="${!active}">
      <span class="material-symbols-outlined q-ico">${active ? "sync" : "schedule"}</span>
      <span class="q-body">
        <span class="q-title">${esc(it.artist || "")}${it.artist ? " — " : ""}${esc(it.title || it.slug)}</span>
        <span class="q-meta"><span class="q-state">${esc(st)}</span>${tags}</span>
      </span>
      ${active ? `<button class="icon-btn icon-only q-pause" data-slug="${esc(it.slug)}" title="${esc(pauseLbl)}"><span class="material-symbols-outlined">${pauseIco}</span></button>` : ""}
      <button class="icon-btn icon-only q-cancel" data-slug="${esc(it.slug)}" title="${esc(t("q_cancel"))}"><span class="material-symbols-outlined">stop_circle</span></button>
    </div>`;
  }

  async function loadQueue() {
    let items = [];
    try {
      const r = await fetch("/api/render-queue");
      if (r.ok) items = (await r.json()).items || [];
    } catch (e) { /* la file est un confort : son absence ne casse pas la page */ }
    const host = document.getElementById("queue-slot");
    if (!host) return;
    if (!items.length) { host.innerHTML = ""; return; }
    host.innerHTML = `
      <div class="card q-card">
        <div class="dr-head">
          <span class="material-symbols-outlined">sync</span>
          <h1>${esc(t("q_title"))}</h1>
        </div>
        <p class="dr-sub">${esc(t("q_sub"))}</p>
        <div class="q-list">${items.map(queueRow).join("")}</div>
      </div>`;
    wireQueue(items);
  }

  function wireQueue(items) {
    const byId = Object.fromEntries(items.map(i => [i.slug, i]));
    document.querySelectorAll(".q-pause").forEach(b => b.addEventListener("click", async () => {
      const slug = b.dataset.slug;
      const want = byId[slug] && byId[slug].state !== "paused";
      b.disabled = true;
      try {
        const r = await fetch(`/api/jobs/${encodeURIComponent(slug)}/render/pause?paused=${want}`, { method: "POST" });
        if (!r.ok) throw new Error();
      } catch (e) { alert(t("q_err")); }
      loadQueue();
    }));
    document.querySelectorAll(".q-cancel").forEach(b => b.addEventListener("click", async () => {
      const slug = b.dataset.slug;
      const it = byId[slug] || {};
      if (!confirm(T[LANG()].q_confirm(it.title || slug))) return;
      b.disabled = true;
      try {
        const r = await fetch(`/api/jobs/${encodeURIComponent(slug)}/render/cancel`, { method: "POST" });
        if (!r.ok) throw new Error();
      } catch (e) { alert(t("q_err")); }
      loadQueue(); load();
    }));
    // Réordonnancement : seules les lignes en attente sont déplaçables — le
    // rendu actif occupe le worker, le sortir de sa place n'aurait aucun sens.
    const list = document.querySelector(".q-list");
    if (!list) return;
    let dragged = null;
    list.addEventListener("dragstart", e => {
      dragged = e.target.closest(".q-row");
      if (dragged) dragged.classList.add("q-dragging");
    });
    list.addEventListener("dragend", () => {
      if (dragged) dragged.classList.remove("q-dragging");
      dragged = null;
      const slugs = [...list.querySelectorAll(".q-row")].map(r => r.dataset.slug);
      fetch("/api/render-queue/reorder", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ slugs }),
      }).catch(() => {}).then(() => loadQueue());
    });
    list.addEventListener("dragover", e => {
      e.preventDefault();
      if (!dragged) return;
      const over = e.target.closest(".q-row");
      if (!over || over === dragged || !over.getAttribute("draggable")) return;
      const rect = over.getBoundingClientRect();
      const after = e.clientY > rect.top + rect.height / 2;
      list.insertBefore(dragged, after ? over.nextSibling : over);
    });
  }

  async function load() {
    let d;
    try {
      const r = await fetch("/api/drafts");
      if (r.status === 401 || r.status === 403) return denied(r.status === 401);
      d = await r.json();
    } catch (e) { return denied(false); }
    const list = (d.drafts || []);
    document.getElementById("content").innerHTML = `
      <div id="queue-slot"></div>
      <div class="card">
        <div class="dr-head">
          <span class="material-symbols-outlined">drafts</span>
          <h1>${esc(t("title"))}</h1>
        </div>
        <p class="dr-sub">${esc(t("sub"))}</p>
        ${list.length ? `<div class="dr-list">${list.map(rowHtml).join("")}</div>` : `
          <div class="dr-empty">
            <span class="material-symbols-outlined">check_circle</span>
            <div class="dr-empty-t">${esc(t("empty_t"))}</div>
            <div>${esc(t("empty_d"))}</div>
          </div>`}
      </div>`;
    wire(list);
    loadQueue();
    // Un rendu avance sans que l'utilisateur agisse : rafraîchissement doux.
    if (queuePoll) clearInterval(queuePoll);
    queuePoll = setInterval(loadQueue, 5000);
  }

  function wire(list) {
    const byId = Object.fromEntries(list.map(d => [d.slug, d]));
    document.querySelectorAll(".dr-row").forEach(row => {
      const slug = row.dataset.slug;
      const open = () => { location.href = `/app#${encodeURIComponent(slug)}`; };
      row.addEventListener("click", open);
      row.addEventListener("keydown", e => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
      });
    });
    document.querySelectorAll(".dr-del").forEach(btn => {
      btn.addEventListener("click", async e => {
        // Sans cela, le clic remonterait à la ligne et ouvrirait l'éditeur.
        e.stopPropagation();
        if (btn.disabled) return;
        const slug = btn.dataset.del;
        const d = byId[slug] || {};
        if (!confirm(T[LANG()].confirm(d.title || slug))) return;
        btn.disabled = true;
        try {
          const r = await fetch(`/api/jobs/${encodeURIComponent(slug)}`, { method: "DELETE" });
          if (!r.ok) throw new Error(r.status);
          load();
        } catch (err) { btn.disabled = false; alert(t("del_err")); }
      });
    });
  }

  (async function init() {
    document.getElementById("t-back").textContent = t("back");
    await L2M.initHeader({ onLangChange: () => { document.getElementById("t-back").textContent = t("back"); load(); } });
    load();
  })();
})();
