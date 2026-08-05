/* Éditeur de découpe précise à la waveform — composant autonome, sans
 * dépendance. Lit le binaire `waveform.dat` (format audiowaveform v2, le même
 * que l'éditeur d'album), l'affiche sur un canvas et propose deux poignées
 * début/fin déplaçables + une lecture d'aperçu. Rendu au millième de seconde.
 *
 * Usage :
 *   const t = new ClipTrimmer(container, {
 *     waveformUrl, audioUrl, duration,
 *     start: 0, end: duration,
 *     labels: {...}, onChange: (start, end) => {}
 *   });
 *   t.getStart(); t.getEnd(); t.destroy();
 *
 * Aux couleurs du site (variables CSS --accent/--muted/--bg/--border/--text),
 * clair/sombre automatique.
 */
(function () {
  "use strict";

  var STYLE_ID = "clip-trimmer-style";
  function injectStyle() {
    if (document.getElementById(STYLE_ID)) return;
    var css =
      ".ct-wrap{display:flex;flex-direction:column;gap:8px}" +
      ".ct-stage{position:relative;width:100%;height:120px;background:var(--bg);" +
      "border:1px solid var(--border);border-radius:8px;overflow:hidden;" +
      "cursor:crosshair;touch-action:none}" +
      ".ct-canvas{position:absolute;inset:0;width:100%;height:100%;display:block}" +
      ".ct-controls{display:flex;align-items:center;gap:10px;flex-wrap:wrap}" +
      ".ct-controls button{display:inline-flex;align-items:center;gap:5px;" +
      "background:var(--card,var(--bg));color:var(--text);border:1px solid var(--border);" +
      "border-radius:7px;padding:5px 10px;font:inherit;font-size:12.5px;cursor:pointer}" +
      ".ct-controls button:hover{border-color:var(--accent)}" +
      ".ct-controls button .material-symbols-outlined{font-size:17px}" +
      ".ct-times{font-size:12.5px;color:var(--muted);font-variant-numeric:tabular-nums}" +
      ".ct-times b{color:var(--text);font-weight:600}";
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

  // Décode le format binaire audiowaveform v2 → tableau de paires min/max
  // normalisées dans [-1, 1].
  function parseWaveform(buf) {
    var dv = new DataView(buf);
    var version = dv.getInt32(0, true);
    if (version !== 1 && version !== 2) throw new Error("waveform.dat illisible");
    var flags = dv.getUint32(4, true);
    var is8 = (flags & 1) === 1;
    // v2 : header de 24 octets (5 int32 + rien de plus utile ici).
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

  function ClipTrimmer(container, opts) {
    injectStyle();
    this.opts = opts || {};
    this.duration = Math.max(0.001, +opts.duration || 0);
    this.start = Math.min(Math.max(0, +opts.start || 0), this.duration);
    this.end = Math.min(Math.max(this.start, +opts.end || this.duration), this.duration);
    this.labels = opts.labels || {};
    this.onChange = opts.onChange || function () {};
    this.wave = null;
    this._drag = null; // "start" | "end" | "region" | null
    this._raf = null;

    var wrap = document.createElement("div");
    wrap.className = "ct-wrap";
    wrap.innerHTML =
      '<div class="ct-stage"><canvas class="ct-canvas"></canvas></div>' +
      '<div class="ct-controls">' +
      '<button type="button" data-act="play"><span class="material-symbols-outlined">play_arrow</span>' +
      "<span data-t=\"play\"></span></button>" +
      '<button type="button" data-act="preview"><span class="material-symbols-outlined">headphones</span>' +
      "<span data-t=\"preview\"></span></button>" +
      '<button type="button" data-act="setstart"><span class="material-symbols-outlined">first_page</span>' +
      "<span data-t=\"setstart\"></span></button>" +
      '<button type="button" data-act="setend"><span class="material-symbols-outlined">last_page</span>' +
      "<span data-t=\"setend\"></span></button>" +
      '<span class="ct-times"></span></div>';
    container.innerHTML = "";
    container.appendChild(wrap);

    this.stage = wrap.querySelector(".ct-stage");
    this.canvas = wrap.querySelector(".ct-canvas");
    this.ctx = this.canvas.getContext("2d");
    this.times = wrap.querySelector(".ct-times");
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
    this._renderTimes();
  }

  ClipTrimmer.prototype._applyLabels = function (wrap) {
    var L = this.labels;
    var map = { play: L.play || "Lire", preview: L.preview || "Écouter la sélection",
                setstart: L.set_start || "Début ici", setend: L.set_end || "Fin ici" };
    wrap.querySelectorAll("[data-t]").forEach(function (el) {
      el.textContent = map[el.getAttribute("data-t")] || "";
    });
  };

  ClipTrimmer.prototype._loadWaveform = function (url) {
    var self = this;
    fetch(url).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.arrayBuffer();
    }).then(function (buf) {
      self.wave = parseWaveform(buf);
      self._draw();
    }).catch(function (e) { console.warn("waveform:", e); self._draw(); });
  };

  ClipTrimmer.prototype._resize = function () {
    var dpr = window.devicePixelRatio || 1;
    var w = this.stage.clientWidth, h = this.stage.clientHeight;
    this.canvas.width = Math.round(w * dpr);
    this.canvas.height = Math.round(h * dpr);
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this._cssW = w; this._cssH = h;
    this._draw();
  };

  ClipTrimmer.prototype._x2t = function (x) {
    return Math.min(this.duration, Math.max(0, (x / this._cssW) * this.duration));
  };
  ClipTrimmer.prototype._t2x = function (t) {
    return (t / this.duration) * this._cssW;
  };

  ClipTrimmer.prototype._draw = function () {
    var ctx = this.ctx, w = this._cssW, h = this._cssH;
    if (!ctx || !w) return;
    ctx.clearRect(0, 0, w, h);
    var accent = cssVar("--accent", "#8893f2");
    var muted = cssVar("--muted", "#8b90a0");
    var mid = h / 2;

    // Waveform : gris hors sélection, accent dedans.
    if (this.wave) {
      var n = this.wave.length, p = this.wave.pairs;
      var sx = this._t2x(this.start), ex = this._t2x(this.end);
      for (var i = 0; i < n; i++) {
        var x = (i / n) * w;
        var mn = p[i * 2], mx = p[i * 2 + 1];
        var y1 = mid - mx * mid * 0.95, y2 = mid - mn * mid * 0.95;
        ctx.strokeStyle = (x >= sx && x <= ex) ? accent : muted;
        ctx.globalAlpha = (x >= sx && x <= ex) ? 0.95 : 0.4;
        ctx.beginPath();
        ctx.moveTo(x + 0.5, y1);
        ctx.lineTo(x + 0.5, Math.max(y1 + 0.5, y2));
        ctx.stroke();
      }
      ctx.globalAlpha = 1;
    }

    // Voile sur les zones exclues.
    var sX = this._t2x(this.start), eX = this._t2x(this.end);
    ctx.fillStyle = cssVar("--bg", "#12141c");
    ctx.globalAlpha = 0.55;
    ctx.fillRect(0, 0, sX, h);
    ctx.fillRect(eX, 0, w - eX, h);
    ctx.globalAlpha = 1;

    // Poignées.
    this._handle(sX, accent);
    this._handle(eX, accent);

    // Tête de lecture.
    if (!this.audio.paused || this.audio.currentTime > 0) {
      var px = this._t2x(this.audio.currentTime);
      ctx.strokeStyle = cssVar("--text", "#e7e9f0");
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(px, 0); ctx.lineTo(px, h); ctx.stroke();
    }
  };

  ClipTrimmer.prototype._handle = function (x, color) {
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

  ClipTrimmer.prototype._renderTimes = function () {
    var L = this.labels;
    this.times.innerHTML = "<b>" + fmt(this.start) + "</b> → <b>" + fmt(this.end) +
      "</b> &nbsp;(" + (L.length || "durée") + " " + fmt(this.end - this.start) + ")";
  };

  ClipTrimmer.prototype._commit = function () {
    this._renderTimes();
    this._draw();
    this.onChange(this.start, this.end);
  };

  ClipTrimmer.prototype._bind = function (wrap) {
    var self = this;
    var HIT = 8; // px de tolérance pour attraper une poignée

    function pointerT(e) {
      var rect = self.stage.getBoundingClientRect();
      var cx = (e.touches ? e.touches[0].clientX : e.clientX) - rect.left;
      return { t: self._x2t(cx), x: cx };
    }

    this.stage.addEventListener("pointerdown", function (e) {
      var pt = pointerT(e);
      var sx = self._t2x(self.start), ex = self._t2x(self.end);
      if (Math.abs(pt.x - sx) <= HIT) self._drag = "start";
      else if (Math.abs(pt.x - ex) <= HIT) self._drag = "end";
      else {
        // Clic simple hors poignée : on déplace la tête de lecture là.
        self.audio.currentTime = pt.t;
        self._draw();
        return;
      }
      self.stage.setPointerCapture(e.pointerId);
      e.preventDefault();
    });

    this.stage.addEventListener("pointermove", function (e) {
      if (!self._drag) return;
      var pt = pointerT(e);
      if (self._drag === "start") {
        self.start = Math.min(pt.t, self.end - 0.05);
        self.start = Math.max(0, self.start);
      } else if (self._drag === "end") {
        self.end = Math.max(pt.t, self.start + 0.05);
        self.end = Math.min(self.duration, self.end);
      }
      self._commit();
    });

    function endDrag() { self._drag = null; }
    this.stage.addEventListener("pointerup", endDrag);
    this.stage.addEventListener("pointercancel", endDrag);

    wrap.querySelectorAll(".ct-controls button").forEach(function (b) {
      b.addEventListener("click", function () { self._action(b.getAttribute("data-act")); });
    });

    this.audio.addEventListener("play", function () { self._tick(); });
    this.audio.addEventListener("pause", function () {
      self._syncPlayIcon(); cancelAnimationFrame(self._raf); self._draw();
    });
    this.audio.addEventListener("ended", function () { self._syncPlayIcon(); });
    this.audio.addEventListener("timeupdate", function () {
      // Arrêt en fin de sélection pendant l'écoute de l'aperçu.
      if (self._previewing && self.audio.currentTime >= self.end) {
        self.audio.pause(); self._previewing = false;
      }
    });
    this._playIcon = wrap.querySelector('[data-act="play"] .material-symbols-outlined');
  };

  ClipTrimmer.prototype._action = function (act) {
    if (act === "play") {
      if (this.audio.paused) { this._previewing = false; this.audio.play(); }
      else this.audio.pause();
    } else if (act === "preview") {
      this._previewing = true;
      this.audio.currentTime = this.start;
      this.audio.play();
    } else if (act === "setstart") {
      this.start = Math.min(this.audio.currentTime, this.end - 0.05);
      this.start = Math.max(0, this.start); this._commit();
    } else if (act === "setend") {
      this.end = Math.max(this.audio.currentTime, this.start + 0.05);
      this.end = Math.min(this.duration, this.end); this._commit();
    }
  };

  ClipTrimmer.prototype._syncPlayIcon = function () {
    if (this._playIcon) this._playIcon.textContent = this.audio.paused ? "play_arrow" : "pause";
  };

  ClipTrimmer.prototype._tick = function () {
    var self = this;
    this._syncPlayIcon();
    (function loop() {
      if (self.audio.paused) return;
      self._draw();
      self._raf = requestAnimationFrame(loop);
    })();
  };

  ClipTrimmer.prototype.getStart = function () { return +this.start.toFixed(3); };
  ClipTrimmer.prototype.getEnd = function () { return +this.end.toFixed(3); };

  ClipTrimmer.prototype.destroy = function () {
    cancelAnimationFrame(this._raf);
    window.removeEventListener("resize", this._onWinResize);
    try { this.audio.pause(); this.audio.src = ""; } catch (e) {}
  };

  window.ClipTrimmer = ClipTrimmer;
})();
