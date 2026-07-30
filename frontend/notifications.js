/* notifications.js — centre de notifications /notifications.
   Liste paginée, « tout marquer comme lu », clic = marque lu + ouvre l'album. */
(function () {
  const T = {
    fr: { back: "Retour", title: "Notifications", empty: "Aucune notification pour l'instant.",
          mark_all: "Tout marquer comme lu", more: "Voir plus",
          login: "Connectez-vous pour voir vos notifications.", login_btn: "Connexion" },
    en: { back: "Back", title: "Notifications", empty: "No notifications yet.",
          mark_all: "Mark all as read", more: "Show more",
          login: "Log in to see your notifications.", login_btn: "Log in" },
  };
  const LANG = () => localStorage.getItem("l2m-lang") || "fr";
  const t = (k) => (T[LANG()] || T.fr)[k] || k;
  let offset = 0, total = 0, unread = 0, DATA = true;

  function shell() {
    document.getElementById("content").innerHTML = `
      <div class="card notif-page">
        <div class="notif-page-head">
          <h1>${t("title")}</h1>
          <button class="icon-btn" id="mark-all"><span class="material-symbols-outlined">done_all</span> ${t("mark_all")}</button>
        </div>
        <div id="list" class="notif-list"></div>
        <div id="more-slot"></div>
      </div>`;
    document.getElementById("mark-all").onclick = async () => {
      try {
        await fetch("/api/social/notifications/read", { method: "POST",
          headers: { "Content-Type": "application/json" }, body: JSON.stringify({ all: true }) });
      } catch (e) {}
      document.querySelectorAll("#list .notif-item:not(.notif-draft):not(.notif-render)").forEach(el => {
        el.classList.remove("unread"); el.querySelector(".notif-dot")?.remove();
      });
    };
  }

  async function loadPage() {
    const d = await fetch(`/api/social/notifications?offset=${offset}&limit=20`).then(r => r.json());
    if (!d.authenticated) {
      document.getElementById("content").innerHTML =
        `<div class="notfound"><span class="material-symbols-outlined">notifications_off</span>
          <div>${t("login")}</div>
          <a class="primary" style="margin-top:14px;text-decoration:none" href="${L2M.loginUrl()}">${t("login_btn")}</a></div>`;
      return;
    }
    total = d.total; unread = d.unread;
    const list = document.getElementById("list");
    if (!d.items.length && offset === 0) {
      list.innerHTML = `<div class="soc-empty">${t("empty")}</div>`;
    } else {
      list.insertAdjacentHTML("beforeend", d.items.map(L2M.notifItem).join(""));
      L2M.loadArtistThumbs(list);
    }
    offset += d.items.length;
    const more = document.getElementById("more-slot");
    more.innerHTML = offset < total
      ? `<button class="soc-more" id="more"><span class="material-symbols-outlined">expand_more</span> ${t("more")}</button>` : "";
    const mb = document.getElementById("more"); if (mb) mb.onclick = loadPage;
  }

  function wireClicks() {
    // Clic sur une notif : marque lue (fire-and-forget) avant la navigation.
    document.getElementById("content").addEventListener("click", (e) => {
      const item = e.target.closest(".notif-item"); if (!item) return;
      const id = parseInt(item.dataset.id, 10);
      // Les rappels de brouillon n'ont pas d'id (synthétiques) : rien à marquer.
      if (!Number.isInteger(id)) return;
      if (item.classList.contains("unread")) {
        navigator.sendBeacon
          ? navigator.sendBeacon("/api/social/notifications/read",
              new Blob([JSON.stringify({ ids: [id] })], { type: "application/json" }))
          : fetch("/api/social/notifications/read", { method: "POST", keepalive: true,
              headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ids: [id] }) });
      }
    });
  }

  async function load() {
    document.getElementById("t-back").textContent = t("back");
    await L2M.initHeader({ onLangChange: () => { offset = 0; shell(); loadPage(); } });
    shell();
    wireClicks();
    await loadPage();
  }
  load();
})();
