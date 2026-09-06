/* Éditeur de re-coupe multi-pistes à la waveform — composant autonome.
 *
 * Version « triptyque » du ClipTrimmer : ouvre sur jusqu'à 3 pistes contiguës
 * (précédente, courante, suivante) et permet de bouger leurs frontières,
 * avec des CADENAS pour chaque frontière partagée qui font suivre les deux
 * bornes voisines (comme dans l'éditeur Peaks.js de l'album complet, cf.
 * NOTES 2026-07-19). C'est le patron nécessaire pour un album live où les
 * chansons s'enchaînent : bouger une frontière rallonge la piste d'un côté
 * et raccourcit la voisine de l'autre — le cadenas fermé fait exactement ça
 * en un seul geste.
 *
 * Usage :
 *   const t = new MultiTrimmer(container, {
 *     waveformUrl, audioUrl, duration,
 *     tracks: [{n, title, start, end}, ...],        // 1 à 3 pistes contiguës
 *     boundaries: [{left_n, right_n, linked:true}], // frontières partagées
 *     targetN: n,                                   // piste mise en avant
 *     labels: {play, preview, length, ...},
 *   });
 *   t.getEdits(); // → [{n, start, end}] pour les pistes réellement bougées
 *   t.destroy();
 *
 * Aux couleurs du site (variables CSS --accent/--muted/--bg/--border/--text),
 * clair/sombre automatique.
 */
(function () {
  "use strict";

  var STYLE_ID = "multi-trimmer-style";
  function injectStyle() {
    if (document.getElementById(STYLE_ID)) return;
    var css =
      ".mt-wrap{display:flex;flex-direction:column;gap:10px}" +
      ".mt-stage{position:relative;width:100%;height:150px;background:var(--bg);" +
      "border:1px solid var(--border);border-radius:8px;overflow:hidden;" +
      "cursor:crosshair;touch-action:none}" +
      ".mt-canvas{position:absolute;inset:0;width:100%;height:100%;display:block}" +
      // Étiquette au-dessus de chaque piste (dans le canvas également, mais un
      // overlay HTML est plus lisible qu'un texte canvas — polices propres).
      ".mt-labels{position:absolute;inset:0;pointer-events:none}" +
      ".mt-label{position:absolute;top:4px;font-size:11px;font-weight:600;" +
      "color:var(--text);background:rgba(0,0,0,.35);padding:2px 6px;" +
      "border-radius:4px;white-space:nowrap;max-width:calc(100% - 12px);" +
      "overflow:hidden;text-overflow:ellipsis}" +
      ".mt-label.target{background:var(--accent);color:#0d0f13}" +
      // Cadenas positionnés au-dessus des frontières partagées, cliquables.
      // Le parent couvre tout le stage (référence de coordonnées absolues) ;
      // les cadenas eux-mêmes se calent à `bottom:6px`. Sans ça, un parent
      // « height:0; bottom:6px » place les enfants en haut du parent (y = stage.h - 6px),
      // donc ~24px sous overflow:hidden → seul le haut du bouton visible.
      ".mt-locks{position:absolute;inset:0;pointer-events:none}" +
      ".mt-lock{position:absolute;bottom:6px;pointer-events:auto;transform:translateX(-50%);" +
      "background:var(--card,var(--bg));border:1px solid var(--border);" +
      "border-radius:50%;width:26px;height:26px;display:flex;align-items:center;" +
      "justify-content:center;cursor:pointer;color:var(--muted);box-shadow:0 1px 4px rgba(0,0,0,.35);z-index:2}" +
      ".mt-lock.linked{color:var(--accent);border-color:var(--accent)}" +
      ".mt-lock:hover{border-color:var(--accent)}" +
      ".mt-lock .material-symbols-outlined{font-size:15px}" +
      ".mt-controls{display:flex;align-items:center;gap:10px;flex-wrap:wrap}" +
      ".mt-controls button{display:inline-flex;align-items:center;gap:5px;" +
      "background:var(--card,var(--bg));color:var(--text);border:1px solid var(--border);" +
      "border-radius:7px;padding:5px 10px;font:inherit;font-size:12.5px;cursor:pointer}" +
      ".mt-controls button:hover{border-color:var(--accent)}" +
      ".mt-controls button .material-symbols-outlined{font-size:17px}" +
      ".mt-times{font-size:12.5px;color:var(--muted);font-variant-numeric:tabular-nums}" +
      ".mt-times b{color:var(--text);font-weight:600}" +
      // Légende sous le canvas rappelant l'état des cadenas.
      ".mt-legend{font-size:11.5px;color:var(--muted);line-height:1.4}" +
      ".mt-legend .kbd{border:1px solid var(--border);border-radius:3px;padding:0 4px;" +
      "font-size:10.5px;background:var(--bg)}";
    var s = document.createElement("style");
    s.id = STYLE_ID;
    s.textContent = css;
    document.head.appendChild(s);
  }

  function cssVar(name, fallback) {
    var v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }

  function fmt(sec) {
    sec = Math.max(0, sec || 0);
    var m = Math.floor(sec / 60);
    var s = sec - m * 60;
    return m + ":" + (s < 10 ? "0" : "") + s.toFixed(1);
  }

  // Format identique à ClipTrimmer.
  function parseWaveform(buf) {
    var dv = new DataView(buf);
    var version = dv.getInt32(0, true);
    if (version !== 1 && version !== 2) throw new Error("waveform.dat illisible");
    var flags = dv.getUint32(4, true);
    var is8 = (flags & 1) === 1;
    var headerLen = version === 2 ? 24 : 20;
    var length = dv.getInt32(16, true);
    var pairs = new Float32Array(length * 2);
    var off = headerLen;
    var norm = is8 ? 128 : 32768;
    for (var i = 0; i < length * 2; i++) {
      var val = is8 ? dv.getInt8(off) : dv.getInt16(off, true);
      pairs[i] = val / norm;
      off += is8 ? 1 : 2;
    }
    return { pairs: pairs, length: length };
  }

  function MultiTrimmer(container, opts) {
    injectStyle();
    this.opts = opts || {};
    this.duration = Math.max(0.001, +opts.duration || 0);
    this.labels = opts.labels || {};
    this.targetN = opts.targetN;

    // Bornes de chaque piste (start/end) dans la tranche. Copie modifiable ;
    // les originaux sont conservés pour détecter ce qui a réellement bougé.
    this.tracks = (opts.tracks || []).map(function (t) {
      return { n: t.n, title: t.title || "",
               start: Math.max(0, +t.start || 0),
               end: Math.min(this.duration, +t.end || 0) };
    }, this);
    this._origin = this.tracks.map(function (t) {
      return { n: t.n, start: t.start, end: t.end };
    });

    // Frontières partagées : chacune lie deux pistes voisines. `linked` par
    // défaut selon l'état de préparation ; l'utilisateur peut basculer.
    this.boundaries = (opts.boundaries || []).map(function (b) {
      return { left_n: b.left_n, right_n: b.right_n, linked: b.linked !== false };
    });
    // Au montage : si linked, on réaligne la borne right.start sur left.end
    // (le manifest peut avoir des flottants à ~ms près qui feraient croire à
    // un délié). Idempotent sinon.
    this.boundaries.forEach(function (b) {
      if (!b.linked) return;
      var l = this._trackByN(b.left_n), r = this._trackByN(b.right_n);
      if (l && r) r.start = l.end;
    }, this);

    this.wave = null;
    this._drag = null; // { kind: 'end'|'start'|'boundary', track|left+right, x0, ... }
    this._raf = null;

    var wrap = document.createElement("div");
    wrap.className = "mt-wrap";
    wrap.innerHTML =
      '<div class="mt-stage">' +
      '<canvas class="mt-canvas"></canvas>' +
      '<div class="mt-labels"></div>' +
      '<div class="mt-locks"></div>' +
      "</div>" +
      '<div class="mt-controls">' +
      '<button type="button" data-act="play"><span class="material-symbols-outlined">play_arrow</span>' +
      "<span data-t=\"play\"></span></button>" +
      '<button type="button" data-act="preview"><span class="material-symbols-outlined">headphones</span>' +
      "<span data-t=\"preview\"></span></button>" +
      '<span class="mt-times"></span>' +
      "</div>" +
      '<p class="mt-legend" data-t="legend"></p>';
    container.innerHTML = "";
    container.appendChild(wrap);

    this.stage = wrap.querySelector(".mt-stage");
    this.canvas = wrap.querySelector(".mt-canvas");
    this.ctx = this.canvas.getContext("2d");
    this.labelsEl = wrap.querySelector(".mt-labels");
    this.locksEl = wrap.querySelector(".mt-locks");
    this.timesEl = wrap.querySelector(".mt-times");
    this.audio = document.createElement("audio");
    this.audio.preload = "auto";
    this.audio.src = opts.audioUrl;
    wrap.appendChild(this.audio);

    this._applyLabels(wrap);
    this._bind(wrap);
    this._loadWaveform(opts.waveformUrl);
    this._resize();
    this._onWinResize = this._resize.bind(this);
    window.addEventListener("resize", this._onWinResize);
    this._renderLabels();
    this._renderLocks();
    this._renderTimes();
  }

  MultiTrimmer.prototype._trackByN = function (n) {
    for (var i = 0; i < this.tracks.length; i++)
      if (this.tracks[i].n === n) return this.tracks[i];
    return null;
  };

  MultiTrimmer.prototype._applyLabels = function (wrap) {
    var L = this.labels;
    var map = { play: L.play || "Lire", preview: L.preview || "Écouter la piste",
                legend: L.legend || "" };
    wrap.querySelectorAll("[data-t]").forEach(function (el) {
      var txt = map[el.getAttribute("data-t")] || "";
      el.textContent = txt;
      // Sans libellé fourni (la légende est facultative), on masque le nœud
      // plutôt que de laisser un élément vide occuper une ligne de flux.
      el.hidden = !txt;
    });
  };

  MultiTrimmer.prototype._loadWaveform = function (url) {
    var self = this;
    fetch(url).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.arrayBuffer();
    }).then(function (buf) {
      self.wave = parseWaveform(buf);
      self._draw();
    }).catch(function (e) { console.warn("waveform:", e); self._draw(); });
  };

  MultiTrimmer.prototype._resize = function () {
    var dpr = window.devicePixelRatio || 1;
    var w = this.stage.clientWidth, h = this.stage.clientHeight;
    this.canvas.width = Math.round(w * dpr);
    this.canvas.height = Math.round(h * dpr);
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this._cssW = w; this._cssH = h;
    this._draw();
    this._renderLabels();
    this._renderLocks();
  };

  MultiTrimmer.prototype._x2t = function (x) {
    return Math.min(this.duration, Math.max(0, (x / this._cssW) * this.duration));
  };
  MultiTrimmer.prototype._t2x = function (t) {
    return (t / this.duration) * this._cssW;
  };

  // Silhouette de la waveform teintée piste par piste : la piste cible en
  // couleur accent, les voisines en couleur douce (muted). Les zones hors
  // tranche (padding et gaps volontaires) restent en gris pâle.
  MultiTrimmer.prototype._draw = function () {
    var ctx = this.ctx, w = this._cssW, h = this._cssH;
    if (!ctx || !w) return;
    ctx.clearRect(0, 0, w, h);
    var accent = cssVar("--accent", "#8893f2");
    var muted = cssVar("--muted", "#8b90a0");
    var mid = h / 2;

    // Fond des régions par piste (accent transparent pour la cible, léger pour
    // les voisines) → aide à voir DU premier coup d'œil quelle piste est où.
    for (var i = 0; i < this.tracks.length; i++) {
      var t = this.tracks[i];
      var x1 = this._t2x(t.start), x2 = this._t2x(t.end);
      ctx.globalAlpha = (t.n === this.targetN) ? 0.14 : 0.06;
      ctx.fillStyle = (t.n === this.targetN) ? accent : muted;
      ctx.fillRect(x1, 0, x2 - x1, h);
    }
    ctx.globalAlpha = 1;

    // Ligne de séparation aux frontières entre pistes (fines, pointillés).
    ctx.setLineDash([2, 3]);
    ctx.strokeStyle = muted;
    for (var j = 0; j < this.tracks.length - 1; j++) {
      var xb = this._t2x(this.tracks[j].end);
      ctx.beginPath(); ctx.moveTo(xb, 0); ctx.lineTo(xb, h); ctx.stroke();
      // Si non lié et gap : marque la borne right.start aussi.
      var rs = this.tracks[j + 1].start;
      if (Math.abs(rs - this.tracks[j].end) > 0.05) {
        var xr = this._t2x(rs);
        ctx.beginPath(); ctx.moveTo(xr, 0); ctx.lineTo(xr, h); ctx.stroke();
      }
    }
    ctx.setLineDash([]);

    // Waveform : accent dans la piste cible, muted ailleurs.
    if (this.wave) {
      var n = this.wave.length, p = this.wave.pairs;
      var tgt = this._trackByN(this.targetN);
      var sx = tgt ? this._t2x(tgt.start) : 0;
      var ex = tgt ? this._t2x(tgt.end) : 0;
      for (var k = 0; k < n; k++) {
        var x = (k / n) * w;
        var mn = p[k * 2], mx = p[k * 2 + 1];
        var y1 = mid - mx * mid * 0.90;
        var y2 = mid - mn * mid * 0.90;
        var inTarget = tgt && x >= sx && x <= ex;
        ctx.strokeStyle = inTarget ? accent : muted;
        ctx.globalAlpha = inTarget ? 0.95 : 0.55;
        ctx.beginPath();
        ctx.moveTo(x + 0.5, y1);
        ctx.lineTo(x + 0.5, Math.max(y1 + 0.5, y2));
        ctx.stroke();
      }
      ctx.globalAlpha = 1;
    }

    // Poignées : bord gauche de la 1re piste (si target), bord droit de la
    // dernière (si target). Frontières partagées : voir _drawHandles.
    this._drawHandles();

    // Tête de lecture.
    if (!this.audio.paused || this.audio.currentTime > 0) {
      var px = this._t2x(this.audio.currentTime);
      ctx.strokeStyle = cssVar("--text", "#e7e9f0");
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(px, 0); ctx.lineTo(px, h); ctx.stroke();
    }
  };

  // Handles rendus séparément pour être clic-testables via _handleAt().
  MultiTrimmer.prototype._drawHandles = function () {
    var accent = cssVar("--accent", "#8893f2");
    // Handle du bord gauche de la 1re piste (uniquement si c'est la cible).
    if (this.tracks.length > 0 && this.tracks[0].n === this.targetN) {
      this._handle(this._t2x(this.tracks[0].start), accent, "left-edge");
    }
    // Handle du bord droit de la dernière piste (uniquement si c'est la cible).
    var last = this.tracks[this.tracks.length - 1];
    if (last && last.n === this.targetN) {
      this._handle(this._t2x(last.end), accent, "right-edge");
    }
    // Frontières partagées : 1 handle si linked, 2 sinon (borne de chaque côté).
    for (var i = 0; i < this.tracks.length - 1; i++) {
      var b = this.boundaries[i];
      var linked = b && b.linked;
      if (linked) {
        // La borne partagée est l'end du gauche (= start du droit).
        this._handle(this._t2x(this.tracks[i].end), accent, "shared-" + i);
      } else {
        // Deux handles distincts : end du gauche + start du droit.
        this._handle(this._t2x(this.tracks[i].end), accent, "left-" + i);
        this._handle(this._t2x(this.tracks[i + 1].start), accent, "right-" + i);
      }
    }
  };

  MultiTrimmer.prototype._handle = function (x, color) {
    var ctx = this.ctx, h = this._cssH;
    ctx.strokeStyle = color; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke();
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.moveTo(x - 5, 0); ctx.lineTo(x + 5, 0); ctx.lineTo(x, 9); ctx.closePath();
    ctx.fill();
    ctx.beginPath();
    ctx.moveTo(x - 5, h); ctx.lineTo(x + 5, h); ctx.lineTo(x, h - 9); ctx.closePath();
    ctx.fill();
  };

  MultiTrimmer.prototype._renderLabels = function () {
    if (!this._cssW) return;
    var html = "";
    for (var i = 0; i < this.tracks.length; i++) {
      var t = this.tracks[i];
      var x1 = this._t2x(t.start), x2 = this._t2x(t.end);
      var cls = "mt-label" + (t.n === this.targetN ? " target" : "");
      var num = String(t.n).padStart(2, "0");
      var label = num + " · " + t.title.replace(/</g, "&lt;");
      html += '<span class="' + cls + '" style="left:' +
              (x1 + 4) + "px;max-width:" + Math.max(30, x2 - x1 - 12) + 'px">' +
              label + "</span>";
    }
    this.labelsEl.innerHTML = html;
  };

  MultiTrimmer.prototype._renderLocks = function () {
    if (!this._cssW) return;
    var html = "";
    for (var i = 0; i < this.tracks.length - 1; i++) {
      var b = this.boundaries[i] || { linked: true };
      var xb = this._t2x(this.tracks[i].end);
      // Icône lock/lock_open + classe pour distinguer visuellement.
      var icon = b.linked ? "link" : "link_off";
      var cls = "mt-lock" + (b.linked ? " linked" : "");
      var title = (this.labels[b.linked ? "unlock" : "lock"]
                   || (b.linked ? "Cliquez pour délier"
                                : "Cliquez pour lier"));
      html += '<button type="button" class="' + cls +
              '" style="left:' + xb + 'px" data-b="' + i +
              '" title="' + title.replace(/"/g, "&quot;") + '">' +
              '<span class="material-symbols-outlined">' + icon + "</span></button>";
    }
    this.locksEl.innerHTML = html;
    var self = this;
    this.locksEl.querySelectorAll(".mt-lock").forEach(function (btn) {
      btn.addEventListener("click", function (e) {
        e.stopPropagation();
        self._toggleLock(+btn.getAttribute("data-b"));
      });
    });
  };

  MultiTrimmer.prototype._toggleLock = function (i) {
    var b = this.boundaries[i];
    if (!b) return;
    b.linked = !b.linked;
    if (b.linked) {
      // Refermer la liaison : le côté droit s'aligne sur le gauche
      // (choix arbitraire — l'utilisateur bougera ensemble ensuite).
      var l = this._trackByN(b.left_n), r = this._trackByN(b.right_n);
      if (l && r) r.start = l.end;
    }
    this._commit();
  };

  MultiTrimmer.prototype._renderTimes = function () {
    // Récap concise sur la piste cible : bornes courantes + durée.
    var t = this._trackByN(this.targetN);
    if (!t) { this.timesEl.textContent = ""; return; }
    var L = this.labels;
    this.timesEl.innerHTML = "<b>" + fmt(t.start) + "</b> → <b>" + fmt(t.end) +
      "</b> &nbsp;(" + (L.length || "durée") + " " + fmt(t.end - t.start) + ")";
  };

  MultiTrimmer.prototype._commit = function () {
    this._renderLabels();
    this._renderLocks();
    this._renderTimes();
    this._draw();
  };

  // Détermine ce qui est sous le pointeur. Renvoie une description utilisable
  // par le drag (bougeant les bonnes valeurs). Tolérance HIT autour du x.
  MultiTrimmer.prototype._pick = function (x) {
    var HIT = 8;
    // Bord gauche de la 1re piste si target.
    if (this.tracks.length > 0 && this.tracks[0].n === this.targetN) {
      var x0 = this._t2x(this.tracks[0].start);
      if (Math.abs(x - x0) <= HIT) return { kind: "leftEdge", i: 0 };
    }
    // Bord droit de la dernière si target.
    var last = this.tracks.length - 1;
    if (this.tracks[last] && this.tracks[last].n === this.targetN) {
      var xL = this._t2x(this.tracks[last].end);
      if (Math.abs(x - xL) <= HIT) return { kind: "rightEdge", i: last };
    }
    // Frontières partagées.
    for (var i = 0; i < this.tracks.length - 1; i++) {
      var b = this.boundaries[i] || { linked: true };
      var xEnd = this._t2x(this.tracks[i].end);
      var xStart = this._t2x(this.tracks[i + 1].start);
      if (b.linked) {
        if (Math.abs(x - xEnd) <= HIT) return { kind: "shared", i: i };
      } else {
        // Deux handles distincts, on choisit le plus proche.
        var dEnd = Math.abs(x - xEnd), dStart = Math.abs(x - xStart);
        if (dEnd <= HIT || dStart <= HIT) {
          return { kind: (dEnd <= dStart) ? "endLeft" : "startRight", i: i };
        }
      }
    }
    return null;
  };

  MultiTrimmer.prototype._bind = function (wrap) {
    var self = this;
    function pointerT(e) {
      var rect = self.stage.getBoundingClientRect();
      var cx = (e.touches ? e.touches[0].clientX : e.clientX) - rect.left;
      return { t: self._x2t(cx), x: cx };
    }
    this.stage.addEventListener("pointerdown", function (e) {
      // Ignorer les clics sur les cadenas (bouton HTML par-dessus le canvas).
      if (e.target && e.target.closest && e.target.closest(".mt-lock")) return;
      var pt = pointerT(e);
      var hit = self._pick(pt.x);
      if (!hit) {
        // Clic hors handle → déplace la tête de lecture.
        self.audio.currentTime = pt.t;
        self._draw();
        return;
      }
      self._drag = hit;
      self.stage.setPointerCapture(e.pointerId);
      e.preventDefault();
    });
    this.stage.addEventListener("pointermove", function (e) {
      if (!self._drag) return;
      var pt = pointerT(e);
      self._applyDrag(pt.t);
      self._commit();
    });
    function endDrag() { self._drag = null; }
    this.stage.addEventListener("pointerup", endDrag);
    this.stage.addEventListener("pointercancel", endDrag);

    wrap.querySelectorAll(".mt-controls button").forEach(function (b) {
      b.addEventListener("click", function () { self._action(b.getAttribute("data-act")); });
    });
    this.audio.addEventListener("play", function () { self._tick(); });
    this.audio.addEventListener("pause", function () {
      self._syncPlayIcon();
      if (self._raf) cancelAnimationFrame(self._raf);
      self._draw();
    });
    this.audio.addEventListener("ended", function () { self._syncPlayIcon(); });
    this.audio.addEventListener("timeupdate", function () {
      if (self._previewing) {
        var t = self._trackByN(self.targetN);
        if (t && self.audio.currentTime >= t.end) {
          self.audio.pause(); self._previewing = false;
        }
      }
    });
    this._playIcon = wrap.querySelector('[data-act="play"] .material-symbols-outlined');
  };

  // Applique le drag courant à la valeur `t` (temps en secondes DANS la tranche).
  MultiTrimmer.prototype._applyDrag = function (t) {
    var d = this._drag;
    if (!d) return;
    var MIN = 0.05; // largeur minimale d'une piste (secondes)
    if (d.kind === "leftEdge") {
      var tr = this.tracks[d.i];
      tr.start = Math.max(0, Math.min(t, tr.end - MIN));
    } else if (d.kind === "rightEdge") {
      var tr2 = this.tracks[d.i];
      tr2.end = Math.min(this.duration, Math.max(t, tr2.start + MIN));
    } else if (d.kind === "shared") {
      // Frontière liée : les deux pistes suivent la même valeur.
      var L = this.tracks[d.i], R = this.tracks[d.i + 1];
      var lo = L.start + MIN, hi = R.end - MIN;
      var v = Math.max(lo, Math.min(hi, t));
      L.end = v; R.start = v;
    } else if (d.kind === "endLeft") {
      var L2 = this.tracks[d.i];
      L2.end = Math.max(L2.start + MIN,
                        Math.min(this.tracks[d.i + 1].start, t));
    } else if (d.kind === "startRight") {
      var R2 = this.tracks[d.i + 1];
      R2.start = Math.min(R2.end - MIN,
                          Math.max(this.tracks[d.i].end, t));
    }
  };

  MultiTrimmer.prototype._action = function (act) {
    if (act === "play") {
      if (this.audio.paused) { this._previewing = false; this.audio.play(); }
      else this.audio.pause();
    } else if (act === "preview") {
      var t = this._trackByN(this.targetN);
      if (!t) return;
      this._previewing = true;
      this.audio.currentTime = t.start;
      this.audio.play();
    }
  };

  MultiTrimmer.prototype._syncPlayIcon = function () {
    if (this._playIcon) this._playIcon.textContent = this.audio.paused ? "play_arrow" : "pause";
  };

  MultiTrimmer.prototype._tick = function () {
    var self = this;
    this._syncPlayIcon();
    (function loop() {
      if (self.audio.paused) return;
      self._draw();
      self._raf = requestAnimationFrame(loop);
    })();
  };

  // Ce qui a réellement bougé depuis le montage — le backend n'écrit et ne
  // re-encode que ces pistes-là (les autres restent intactes).
  MultiTrimmer.prototype.getEdits = function () {
    var out = [];
    for (var i = 0; i < this.tracks.length; i++) {
      var cur = this.tracks[i], og = this._origin[i];
      var dS = Math.abs(cur.start - og.start);
      var dE = Math.abs(cur.end - og.end);
      if (dS > 0.001 || dE > 0.001) {
        out.push({ n: cur.n,
                   start: +cur.start.toFixed(3),
                   end: +cur.end.toFixed(3) });
      }
    }
    return out;
  };

  MultiTrimmer.prototype.destroy = function () {
    if (this._raf) cancelAnimationFrame(this._raf);
    window.removeEventListener("resize", this._onWinResize);
    try { this.audio.pause(); this.audio.src = ""; } catch (e) {}
  };

  window.MultiTrimmer = MultiTrimmer;
})();
