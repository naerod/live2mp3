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
      q_title: "Rendus en cours", q_sub: "Albums en cours de création ou d'encodage vidéo. Glissez une ligne pour changer l'ordre.",
      q_running: "En cours", q_paused: "En pause", q_queued: "En attente",
      q_pause: "Mettre en pause", q_resume: "Reprendre", q_cancel: "Arrêter ce rendu",
      q_confirm: (t) => `Arrêter le rendu de « ${t} » ?\n\nLes pistes déjà encodées seront supprimées.`,
      q_confirm_video: (t) => `Arrêter le rendu vidéo de « ${t} » ?\n\nL'album audio est déjà en place et n'est pas touché ; seul le MP4 en cours d'encodage est abandonné.`,
      q_err: "Action impossible",
      q_kind_audio: "Album audio", q_kind_video: "Rendu vidéo",
      q_empty: "Aucun rendu en cours.",
      q_step: "Étape", q_open: "Voir le détail du rendu",
      qs_render: "Découpe et encodage des pistes", qs_tags: "Métadonnées MP3",
      qs_artwork: "Pochettes PDF", qs_disc: "Image disque",
      qs_video: "Ré-encodage de la vidéo",
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
      // La rubrique file d'attente n'avait jamais été traduite : un
      // gestionnaire en anglais y lisait les clés brutes (« q_title »).
      q_title: "Renders in progress", q_sub: "Albums being created or video-encoded. Drag a row to reorder.",
      q_running: "Running", q_paused: "Paused", q_queued: "Queued",
      q_pause: "Pause", q_resume: "Resume", q_cancel: "Stop this render",
      q_confirm: (t) => `Stop rendering "${t}"?\n\nTracks already encoded will be deleted.`,
      q_confirm_video: (t) => `Stop the video render of "${t}"?\n\nThe audio album is already in place and stays untouched; only the MP4 being encoded is discarded.`,
      q_err: "Action failed",
      q_kind_audio: "Audio album", q_kind_video: "Video render",
      q_empty: "No render in progress.",
      q_step: "Step", q_open: "View render details",
      qs_render: "Cutting & encoding tracks", qs_tags: "MP3 metadata",
      qs_artwork: "PDF artwork", qs_disc: "Disc image",
      qs_video: "Video re-encoding",
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
    const href = `/app#${encodeURIComponent(d.slug)}`;
    return `<div class="dr-row" data-slug="${esc(d.slug)}" role="button" tabindex="0">
      <a class="dr-open" href="${href}" tabindex="-1" aria-hidden="true"></a>
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
    // Depuis le découplage du 2026-08-03, une ligne dit *quelle* étape tourne :
    // l'album audio d'un rendu vidéo est déjà écoutable, ne pas le distinguer
    // laissait croire qu'il restait indisponible pendant tout l'encodage.
    const video = it.kind === "video";
    const kindLbl = video ? t("q_kind_video") : t("q_kind_audio");
    // Étape en cours (« Étape 2/4 · Métadonnées MP3 ») : sans elle, une barre
    // seule ne dit pas ce que la machine est en train de faire.
    const st2 = it.step;
    const stageTxt = st2
      ? `${t("q_step")} ${st2.index}/${st2.total} · ${t("qs_" + st2.stage)}`
      : "";
    // Progression réelle (ffmpeg / compteur de pistes) quand le worker en
    // publie une ; sinon barre indéterminée — un « 0 % » figé se lit comme un
    // blocage.
    const p = it.pct;
    const bar = active ? `<div class="q-bar${p == null ? " q-indet" : ""}">
        <div class="q-fill" style="${p == null ? "" : `width:${Math.round(p)}%`}"></div>
      </div>` : "";
    return `<div class="q-row q-${it.state}" data-slug="${esc(it.slug)}" data-kind="${esc(it.kind || "render")}" draggable="${!active}">
      <span class="material-symbols-outlined q-ico">${active ? (video ? "movie" : "sync") : "schedule"}</span>
      <a class="q-body q-open" draggable="false" href="/app#${encodeURIComponent(it.slug)}" title="${esc(t("q_open"))}">
        <span class="q-title">${esc(it.artist || "")}${it.artist ? " — " : ""}${esc(it.title || it.slug)}</span>
        <span class="q-meta"><span class="q-state">${esc(st)}</span>
          <span class="q-kind">${esc(kindLbl)}</span>${tags}
          ${stageTxt ? `<span class="q-stage">${esc(stageTxt)}</span>` : ""}
          ${active && p != null ? `<span class="q-pct">${Math.round(p)} %</span>` : ""}</span>
        ${bar}
      </a>
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
    // Section toujours présente, même vide : elle a vocation à l'être la
    // plupart du temps, et sa disparition faisait douter de son existence.
    host.innerHTML = `
      <div class="card q-card">
        <div class="dr-head">
          <span class="material-symbols-outlined">sync</span>
          <h1>${esc(t("q_title"))}</h1>
        </div>
        <p class="dr-sub">${esc(t("q_sub"))}</p>
        ${items.length ? `<div class="q-list">${items.map(queueRow).join("")}</div>`
          : `<div class="q-none"><span class="material-symbols-outlined">done_all</span>${esc(t("q_empty"))}</div>`}
      </div>`;
    if (items.length) wireQueue(items);
  }

  function wireQueue(items) {
    // Clé (slug, nature) et non slug seul : à la bascule audio→vidéo, le job
    // audio est encore « started » quand le job vidéo entre en file — le même
    // album apparaît alors brièvement sur deux lignes.
    const key = (i) => `${i.slug}|${i.kind || "render"}`;
    const byId = Object.fromEntries(items.map(i => [key(i), i]));
    const of = (b) => byId[`${b.dataset.slug}|${b.closest(".q-row").dataset.kind}`] || {};
    // Rouvrir le détail du rendu. Seul le corps de la ligne est cliquable, pas
    // la ligne entière : pause et arrêt sont des boutons voisins, et un clic
    // destructeur ne doit jamais partir d'un geste de navigation.
    document.querySelectorAll(".q-open").forEach(body => {
      const slug = body.closest(".q-row").dataset.slug;
      const open = () => { location.href = `/app#${encodeURIComponent(slug)}`; };
      // Le lien natif fait déjà la navigation au clic gauche (et permet
      // clic-molette / Ctrl+clic) : on ne garde que le clavier.
      body.addEventListener("keydown", e => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
      });
    });
    document.querySelectorAll(".q-pause").forEach(b => b.addEventListener("click", async () => {
      const slug = b.dataset.slug;
      const want = of(b).state !== "paused";
      b.disabled = true;
      try {
        const r = await fetch(`/api/jobs/${encodeURIComponent(slug)}/render/pause?paused=${want}`, { method: "POST" });
        if (!r.ok) throw new Error();
      } catch (e) { alert(t("q_err")); }
      loadQueue();
    }));
    document.querySelectorAll(".q-cancel").forEach(b => b.addEventListener("click", async () => {
      const slug = b.dataset.slug;
      const it = of(b);
      const dict = T[LANG()] || T.fr;
      const ask = it.kind === "video" ? dict.q_confirm_video : dict.q_confirm;
      if (!confirm(ask(it.title || slug))) return;
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
      // L'ancre .dr-open couvre la ligne : clic gauche, molette et Ctrl+clic
      // passent par le navigateur. Le handler ne sert plus qu'au clavier.
      row.addEventListener("click", e => { if (!e.target.closest("a,button")) open(); });
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
