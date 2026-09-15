/* entity.js — page auto d'une entité suivable :
   /artist/{id}  (id Deezer)   /festival/{slug}   /venue/{slug}
   Regroupe tous les posts (fiches album) liés à l'entité + bouton suivre/cloche.
   Ces pages sont auto-alimentées : aucun contenu saisi à la main. */
(function () {
  const T = {
    fr: { back: "Retour", followers: "abonnés", posts: "posts",
          artist: "Artiste / groupe", festival: "Festival", venue: "Lieu",
          no_posts: "Aucun post pour l'instant.", not_found: "Page introuvable." },
    en: { back: "Back", followers: "followers", posts: "posts",
          artist: "Artist / band", festival: "Festival", venue: "Venue",
          no_posts: "No posts yet.", not_found: "Page not found." },
  };
  const LANG = () => localStorage.getItem("l2m-lang") || "fr";
  const t = (k) => (T[LANG()] || T.fr)[k] || k;
  const esc = L2M.esc;

  const parts = location.pathname.split("/").filter(Boolean);   // [type, id…]
  const TYPE = parts[0];
  const ID = decodeURIComponent(parts.slice(1).join("/"));
  const ICON = { artist: "artist", festival: "festival", venue: "location_on" };

  let DATA = null;

  function albumCard(a) {
    const cover = a.has_cover
      ? `<div class="cv is-load"><img class="cv-img" loading="lazy" decoding="async" alt=""
           src="/cover/${a.slug}?v=${a.cover_v||0}&w=320"
           onload="this.parentNode.classList.remove('is-load');this.classList.add('rdy')"></div>`
      : `<div class="cv"><span class="material-symbols-outlined">album</span></div>`;
    return `<a class="prof-alb" href="/album/${encodeURIComponent(a.slug)}">
      ${cover}
      <div class="b"><div class="t">${esc(a.title || a.slug)}</div>
        <div class="a">${esc(a.artist || "")}</div></div>
    </a>`;
  }

  function render() {
    const d = DATA;
    const pic = (TYPE === "artist" && d.picture)
      ? `<img class="entity-pic" src="${esc(d.picture)}" alt="" loading="lazy">`
      : `<div class="entity-pic ph"><span class="material-symbols-outlined">${ICON[TYPE] || "tag"}</span></div>`;
    const grid = d.posts.length
      ? `<div class="prof-albums">${d.posts.map(albumCard).join("")}</div>`
      : `<div class="soc-empty">${t("no_posts")}</div>`;
    document.getElementById("content").innerHTML = `
      <div class="card">
        <div class="prof-hero entity-hero">
          ${pic}
          <div class="prof-id">
            <div class="entity-kind"><span class="material-symbols-outlined">${ICON[TYPE] || "tag"}</span>${t(TYPE)}</div>
            <h1>${esc(d.label)}</h1>
            <div class="entity-stats"><b id="fw-count">${d.followers}</b> ${t("followers")}
              <span class="dot">·</span> <b>${d.post_count}</b> ${t("posts")}</div>
          </div>
          <div class="follow-wrap" id="follow-slot"></div>
        </div>
      </div>
      ${grid}`;

    L2M.followButton(document.getElementById("follow-slot"), {
      type: TYPE, id: ID, label: d.label,
      state: { following: d.following, notify: d.notify, followers: d.followers },
      onChange: (st) => { const el = document.getElementById("fw-count"); if (el) el.textContent = st.followers; },
    });
    document.getElementById("t-back").textContent = t("back");
  }

  async function load() {
    document.getElementById("t-back").textContent = t("back");
    await L2M.initHeader({ onLangChange: () => { if (DATA) render(); } });
    const r = await fetch(`/api/social/entity/${encodeURIComponent(TYPE)}/${encodeURIComponent(ID)}`);
    if (!r.ok) {
      document.getElementById("content").innerHTML =
        `<div class="notfound"><span class="material-symbols-outlined">search_off</span>${t("not_found")}</div>`;
      return;
    }
    DATA = await r.json();
    document.title = `live2mp3 — ${DATA.label}`;
    render();
  }
  load();
})();
