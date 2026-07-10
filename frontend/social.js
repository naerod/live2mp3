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
      inner = esc(name.slice(0, 1) || "?");
      style = ` style="background:hsl(${hue(user.username || name)},42%,46%)"`;
    }
    const av = `<span class="soc-avatar"${style}>${inner}</span>`;
    return (link !== false && user.username) ? `<a href="/u/${encodeURIComponent(user.username)}">${av}</a>` : av;
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

  /* ---------------- Widget commentaires ---------------- */
  async function comments(root, slug) {
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
        const c = await api("POST", `/api/social/albums/${slug}/comments`, { body });
        c.reply_count = 0; c.replies = [];
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
      const d = await api("GET", `/api/social/albums/${slug}/comments?sort=${sort}&offset=${offset}&limit=20`);
      total = d.total;
      if (!d.comments.length && offset === 0) {
        list.innerHTML = `<div class="soc-empty">${t("no_comments")}</div>`;
      } else {
        list.insertAdjacentHTML("beforeend", d.comments.map((c) => commentHtml(c, c.id)).join(""));
      }
      offset += d.comments.length;
      moreSlot.innerHTML = offset < total
        ? `<button class="soc-more" id="more-top"><span class="material-symbols-outlined">expand_more</span> ${t("more_comments")}</button>` : "";
      const mt = document.getElementById("more-top");
      if (mt) mt.onclick = loadPage;
    }
    loadPage();

    /* --- rendu --- */
    function voteHtml(c) {
      if (c.deleted) return `<div class="soc-vote"></div>`;
      const cls = c.score > 0 ? "pos" : c.score < 0 ? "neg" : "";
      return `<div class="soc-vote" data-cid="${c.id}" data-myvote="${c.my_vote}" data-score="${c.score}">
        <button class="up${c.my_vote === 1 ? " on" : ""}" data-dir="1"><span class="material-symbols-outlined">arrow_upward</span></button>
        <span class="soc-score ${cls}">${c.score}</span>
        <button class="down${c.my_vote === -1 ? " on" : ""}" data-dir="-1"><span class="material-symbols-outlined">arrow_downward</span></button>
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
        ${voteHtml(c)}
        <div class="soc-main">
          ${byline}
          ${bodyHtml(c)}
          ${actionsHtml(c)}
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

      // Vote
      const voteBox = btn.closest(".soc-vote");
      if (voteBox) {
        if (!meData.authenticated) { location.href = loginUrl(); return; }
        const dir = parseInt(btn.dataset.dir, 10);
        const cur = parseInt(voteBox.dataset.myvote, 10);
        const val = cur === dir ? 0 : dir;
        try {
          const r = await api("POST", `/api/social/comments/${cid}/vote`, { value: val });
          voteBox.outerHTML = voteHtml({ id: cid, score: r.score, my_vote: r.my_vote });
        } catch (err) { if (err.status === 401) location.href = loginUrl(); }
        return;
      }

      const act = btn.dataset.act;
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
        node.querySelector(".soc-vote").innerHTML = "";
        node.querySelector(".soc-main").querySelector(".soc-byline").innerHTML = `<span class="soc-dim">${t("deleted_c")}</span>`;
        node.querySelector(".soc-body").outerHTML = `<div class="soc-body deleted">${t("deleted_c")}</div>`;
        const a = node.querySelector(".soc-actions"); if (a) a.remove();
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

  return { t, esc, avatar, userLink, timeAgo, loginUrl, me, likeButton, comments, LANG };
})();
