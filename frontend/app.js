// --- Accès conditionnel ---
(async()=>{
  let me={authenticated:false,is_gestionnaire:false,is_admin:false};
  try{me=await(await fetch("/api/me")).json();}catch(e){}
  const overlay=document.getElementById("access-overlay");
  const msg=document.getElementById("access-msg");
  const btn=document.getElementById("access-btn");
  const lang=()=>localStorage.getItem("l2m-lang")||"fr";
  const msgs={
    guest:{fr:"Connectez-vous pour accéder à toutes les fonctionnalités du site.",
           en:"Log in to access all features."},
    user:{fr:"Cette fonctionnalité est réservée aux gestionnaires de l'application.",
          en:"This feature is restricted to application managers."}
  };
  if(!me.is_gestionnaire){
    document.body.classList.add("app-locked");
    overlay.classList.remove("hidden");
    if(!me.authenticated){
      msg.textContent=msgs.guest[lang()];
      btn.classList.remove("hidden");
    }else{
      msg.textContent=msgs.user[lang()];
    }
  }
})();

// --- Header + Thème + Langue ---
L2M.initHeader({onLangChange:l=>applyI18n(l)});
function setLang(l){localStorage.setItem("l2m-lang",l);applyI18n(l);}
applyI18n(localStorage.getItem("l2m-lang")||"fr");

const $=id=>document.getElementById(id);
const LANG=()=>localStorage.getItem("l2m-lang")||"fr";
const T=k=>(I18N[LANG()]&&I18N[LANG()][k])||k;
const STEPS=["step-link","step-form","step-progress","step-editor","step-done"];
const show=id=>{STEPS.forEach(s=>$(s).classList.toggle("hidden",s!==id));
  window.scrollTo({top:0,behavior:"smooth"});};

let slug=null;          // slug du projet en cours
let videoInfo=null;     // métadonnées de la vidéo sondée
let setlistSource=null; // provenance de la setlist (setlist.fm) + attribution
let currentPhase=null;  // "prepare" | "render" (pour le bouton réessayer)
let peaksInstance=null;
// Ré-édition d'un album déjà publié (bouton « Ouvrir l'éditeur audio » de la
// fiche de gestion, reprise via /app#slug) : même éditeur, mais l'album n'est
// pas "créé" — publié reste vrai tout du long, wording de fin différent.
let reedit=false;

// ============================================================
// Étape 1 — Analyse du lien
// ============================================================
function setAnalyzing(on){
  $("btn-analyze").disabled=on;
  const st=$("analyze-status");
  st.classList.toggle("hidden",!on);
  if(on)st.innerHTML=`<span class="material-symbols-outlined spin">progress_activity</span><span>${T("analyzing")}</span>`;
}
function analyzeError(text){
  const st=$("analyze-status");
  st.classList.remove("hidden");
  st.innerHTML=`<span class="material-symbols-outlined" style="color:var(--like)">error</span><span>${text}</span>`;
}

$("btn-analyze").onclick=async()=>{
  const url=$("l-url").value.trim();
  if(!url){$("l-url").focus();return;}
  setAnalyzing(true);
  try{
    const r=await fetch("/api/tool/analyze",{method:"POST",
      headers:{"Content-Type":"application/json"},body:JSON.stringify({url})});
    if(!r.ok){
      const d=await r.json().catch(()=>({}));
      throw new Error(d.detail||`HTTP ${r.status}`);
    }
    const d=await r.json();
    setAnalyzing(false);
    videoInfo=d.video;
    setlistSource=d.setlist_source||null;
    fillForm(d.suggestion,d.video,d.ai);
    show("step-form");
  }catch(e){
    setAnalyzing(false);
    analyzeError(`${T("err_analyze")} ${e.message||""}`);
  }
};
$("l-url").addEventListener("keydown",e=>{if(e.key==="Enter")$("btn-analyze").click();});

$("btn-manual").onclick=()=>{
  videoInfo=null;
  $("src-card").classList.add("hidden");
  if(!$("track-rows").children.length)addFormRow({});
  show("step-form");
};
$("btn-back-link").onclick=()=>show("step-link");

// ============================================================
// Étape 2 — Formulaire de vérification
// ============================================================
function fmtDur(s){
  s=Math.round(s||0);
  const h=Math.floor(s/3600),m=Math.floor((s%3600)/60),sec=s%60;
  return(h?`${h}:${String(m).padStart(2,"0")}`:m)+":"+String(sec).padStart(2,"0");
}
function fmtTime(s){
  if(s==null||isNaN(s))return"";
  s=Math.max(0,s);
  const m=Math.floor(s/60),sec=(s%60);
  return`${m}:${sec<10?"0":""}${sec.toFixed(1)}`;
}
function parseTime(v){
  v=(v||"").trim();if(!v)return null;
  const parts=v.split(":").map(Number);
  if(parts.some(isNaN))return null;
  let s=0;for(const p of parts)s=s*60+p;
  return s;
}

function fillForm(sug,video,ai){
  $("f-artist").value=sug.artist||"";
  $("f-title").value=sug.title||"";
  $("f-date").value=sug.date||"";
  $("f-venue").value=[sug.venue,sug.city].filter(Boolean).join(", ");
  $("f-festival").value=sug.festival||"";
  // Carte source
  if(video){
    $("src-card").classList.remove("hidden");
    $("src-thumb").src=video.thumbnail||"";
    $("src-thumb").style.display=video.thumbnail?"":"none";
    $("src-title").textContent=video.title||video.webpage_url;
    $("src-sub").textContent=[video.channel,fmtDur(video.duration)]
      .filter(Boolean).join(" · ");
    const chip=$("src-chip");
    chip.classList.remove("hidden");
    $("src-chip-txt").textContent=ai?T("ai_badge"):
      (video.chapters&&video.chapters.length?T("chapters_badge"):T("no_ai_badge"));
  }else{
    $("src-card").classList.add("hidden");
  }
  // Provenance de la setlist — l'attribution setlist.fm est obligatoire.
  const src=$("setlist-src");
  if(src){
    if(setlistSource){
      src.classList.remove("hidden");
      src.innerHTML=`<span class="material-symbols-outlined">verified</span>`+
        `<span>${T("setlist_official")(setlistSource.tracks)} `+
        `<a href="${setlistSource.url}" target="_blank" rel="noopener">`+
        `${setlistSource.name}</a></span>`;
    }else{src.classList.add("hidden");}
  }
  // Setlist
  $("track-rows").innerHTML="";
  (sug.tracks||[]).forEach(t=>addFormRow(t));
  if(!(sug.tracks||[]).length)addFormRow({});
}

function addFormRow(t){
  const rows=$("track-rows");
  const row=document.createElement("div");
  row.className="track-row";
  const hasTime=t.start!=null&&t.end!=null;
  row.innerHTML=`
    <span class="tn"></span>
    <input class="t-title" placeholder="${T("tr_title_ph")}" value="">
    <input class="t-artist" placeholder="${T("tr_artist_ph")}" value="">
    <span class="t-badge">${hasTime?`<span class="material-symbols-outlined" title="timecodes">schedule</span>${fmtTime(t.start).split(".")[0]}`:""}</span>
    <button class="icon-btn icon-only row-del" title="${T("del")}"><span class="material-symbols-outlined">delete</span></button>`;
  row.querySelector(".t-title").value=t.title||"";
  row.querySelector(".t-artist").value=t.artist||"";
  row.dataset.start=t.start!=null?t.start:"";
  row.dataset.end=t.end!=null?t.end:"";
  row.querySelector(".row-del").onclick=()=>{row.remove();renumber(rows);};
  rows.appendChild(row);
  renumber(rows);
}
function renumber(container){
  [...container.querySelectorAll(".track-row")].forEach((r,i)=>{
    r.querySelector(".tn").textContent=(i+1)+".";
  });
}
$("btn-add-track").onclick=()=>addFormRow({});

$("btn-create").onclick=async()=>{
  const artist=$("f-artist").value.trim();
  const title=$("f-title").value.trim();
  if(!artist||!title){alert(T("err_required"));return;}
  const tracks=[...$("track-rows").querySelectorAll(".track-row")].map((r,i)=>{
    const t={n:i+1,title:r.querySelector(".t-title").value.trim()};
    const a=r.querySelector(".t-artist").value.trim();
    if(a)t.artist=a;
    if(r.dataset.start!=="")t.start=parseFloat(r.dataset.start);
    if(r.dataset.end!=="")t.end=parseFloat(r.dataset.end);
    return t;
  }).filter(t=>t.title);
  const body={
    album:{artist,title,date:$("f-date").value||null,
      venue:$("f-venue").value.trim()||null,
      festival:$("f-festival").value.trim()||null},
    tracks,
    target:$("f-target").value,
    source_url:(videoInfo&&videoInfo.webpage_url)||$("l-url").value.trim(),
    video:$("f-video").checked,
    thumbnail_url:(videoInfo&&videoInfo.thumbnail)||"",
    setlistfm_url:(setlistSource&&setlistSource.url)||"",
    duration:(videoInfo&&videoInfo.duration)||null,
  };
  $("btn-create").disabled=true;
  try{
    const r=await fetch("/api/jobs",{method:"POST",
      headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    if(!r.ok){
      const d=await r.json().catch(()=>({}));
      throw new Error(d.detail||`HTTP ${r.status}`);
    }
    slug=(await r.json()).slug;
    location.hash=slug;   // reprise possible si l'onglet se ferme
    await startPrepare();
  }catch(e){
    alert(e.message||T("err_generic"));
  }finally{
    $("btn-create").disabled=false;
  }
};

// ============================================================
// Étapes 3 & 5 — Progression SSE
// ============================================================
const PREP_STAGES=["download","preview","waveform","ai_markers"];
const RENDER_STAGES=["render","tags","artwork","disc","bundle"];

function runProgress(titleKey,stages,onComplete){
  show("step-progress");
  $("progress-title").textContent=T(titleKey);
  $("progress-err").classList.add("hidden");
  $("progress-actions").classList.add("hidden");
  hideSkip();
  const ul=$("stages");ul.innerHTML="";
  const items={};
  stages.forEach(s=>{
    const li=document.createElement("li");
    li.innerHTML=`<div class="stage-head"><span class="dot"></span>`+
      `<span class="stage-label">${T("stage_"+s)}</span>`+
      `<span class="stage-pct"></span></div>`+
      `<div class="stage-bar"><div class="stage-fill"></div></div>`;
    ul.appendChild(li);items[s]=li;
  });
  const es=new EventSource(`/api/jobs/${slug}/events`);
  es.onmessage=e=>{
    const ev=JSON.parse(e.data);
    const li=items[ev.stage];
    if(li){
      const running=ev.status==="running",done=ev.status==="done";
      li.classList.toggle("running",running);
      li.classList.toggle("done",done);
      const pct=li.querySelector(".stage-pct");
      const fill=li.querySelector(".stage-fill");
      if(running){
        const info=ev.info||{};
        if(info.pct!=null){
          // Progression connue -> barre remplie au pourcentage
          li.classList.remove("indet");
          fill.style.width=Math.round(info.pct)+"%";
          pct.textContent=(info.phase?T("phase_"+info.phase)+" ":"")+Math.round(info.pct)+" %";
        }else{
          // Progression inconnue -> barre animée « ça tourne »
          li.classList.add("indet");
          fill.style.width="";
          pct.textContent=info.phase?T("phase_"+info.phase):T("running_word");
        }
      }else if(done){
        li.classList.remove("indet");
        fill.style.width="100%";
        pct.textContent="";
        if(ev.stage==="ai_markers"&&ev.info&&ev.info.source)
          pct.textContent=T("src_"+ev.info.source)||"";
      }
      // Détection des chansons : proposer de passer tant qu'elle tourne.
      if(ev.stage==="ai_markers"){
        if(running&&(ev.info||{}).skippable)showSkip();else hideSkip();
      }
    }
    if(ev.status==="complete"){es.close();onComplete();}
    if(ev.status==="error"){
      es.close();
      $("progress-err").classList.remove("hidden");
      $("progress-err").innerHTML=`<span class="material-symbols-outlined" style="color:var(--like)">error</span><span>${ev.info&&ev.info.message||T("err_generic")}</span>`;
      $("progress-actions").classList.remove("hidden");
    }
  };
  es.onerror=()=>{/* keepalive/reconnexion gérés par EventSource */};
}

// --- Passer la détection des chansons (étape la plus longue) ---
function showSkip(){
  const w=$("skip-detect-wrap");if(!w)return;
  w.classList.remove("hidden");
}
function hideSkip(){
  const w=$("skip-detect-wrap");if(!w)return;
  w.classList.add("hidden");
  const b=$("btn-skip-detect");
  if(b){b.disabled=false;b.querySelector("[data-i18n]").textContent=T("skip_detect");}
}
$("btn-skip-detect").onclick=async()=>{
  const b=$("btn-skip-detect");
  b.disabled=true;
  b.querySelector("[data-i18n]").textContent=T("skipping");
  try{
    const r=await fetch(`/api/jobs/${slug}/skip-detection`,{method:"POST"});
    if(!r.ok)throw new Error();
    // La suite arrive par SSE : l'étape se termine en « passée » puis l'éditeur.
  }catch(e){
    b.disabled=false;
    b.querySelector("[data-i18n]").textContent=T("skip_detect");
    toast(T("err_generic"),true);
  }
};

async function startPrepare(){
  currentPhase="prepare";
  const r=await fetch(`/api/jobs/${slug}/prepare`,{method:"POST"});
  if(!r.ok){alert(T("err_generic"));return;}
  runProgress("prep_title",PREP_STAGES,openEditor);
}

$("btn-err-back").onclick=()=>show(currentPhase==="render"?"step-editor":"step-form");
$("btn-err-retry").onclick=()=>{
  if(currentPhase==="render")startRender();
  else startPrepare();
};

// ============================================================
// Étape 4 — Éditeur de coupes (vérification humaine)
// ============================================================
const SEG_COLORS=["rgba(136,147,242,.55)","rgba(122,204,174,.55)"];
const DISC_SECONDS=88*60;   // capacité d'un disque physique (88 min)

// Modèle de l'éditeur. `linked` : le début de la piste suit la fin de la
// précédente (grisé, pas de gap). Toujours false pour la 1re piste (début
// libre) ; la fin de la dernière piste reste toujours éditable.
let EDIT=[];   // [{title, artist, start, end, linked}]
let EDIT_ORIG=null;   // instantané de l'analyse IA d'origine (pour "Réinitialiser")

// Reconstruit EDIT depuis les pistes brutes du manifeste (analyse IA).
function buildEditFromTracks(tracks){
  // Le DÉBUT de chaque chanson est la référence (l'IA l'indique, l'humain
  // l'ajuste) : chaque piste commence pile sur sa musique. La FIN suit le
  // début de la piste suivante, donc la transition parlée reste à la fin de la
  // piste (skippable). endRaw = fin libre mémorisée (fin musicale détectée),
  // restaurée si on délie pour couper la transition.
  const e=tracks.filter(t=>t.start!=null&&t.end!=null)
    .map(t=>({title:t.title||"",artist:t.artist||"",
              start:+t.start,end:+t.end,endRaw:+t.end,linked:false}));
  e.sort((a,b)=>a.start-b.start);
  e.forEach((t,i)=>{t.linked=i<e.length-1;});  // fin liée sauf la dernière
  return e;
}

async function openEditor(){
  show("step-editor");
  const m=await(await fetch(`/api/jobs/${slug}/manifest`)).json();
  reedit=!!m.published;
  // Attribut (pas juste le texte) : un changement de langue en cours de
  // session ré-applique data-i18n via applyI18n() et écraserait un simple
  // textContent codé en dur ici.
  $("btn-render").querySelector("[data-i18n]")
    .setAttribute("data-i18n",reedit?"validate_render_reedit":"validate_render");
  applyI18n(LANG());
  const audio=$("ed-audio");
  audio.src=`/api/jobs/${slug}/audio`;
  if(peaksInstance){peaksInstance.destroy();peaksInstance=null;}
  EDIT=buildEditFromTracks(m.tracks);
  // Copie profonde figée : référence pour le bouton « Réinitialiser ».
  EDIT_ORIG=JSON.parse(JSON.stringify(EDIT));
  relinkEnds();
  renderRows();
  updateDiscMarker();

  const PeaksLib=window.Peaks||window.peaks;  // global UMD : `peaks` en v3
  if(!PeaksLib){console.warn("peaks.js non chargé");return;}
  // Couleurs explicites : les défauts de peaks.js sont noirs (fond blanc).
  const acc=getComputedStyle(document.documentElement)
    .getPropertyValue("--accent").trim()||"#8893f2";
  const muted=getComputedStyle(document.documentElement)
    .getPropertyValue("--muted").trim()||"#8b90a0";
  const options={
    zoomview:{container:$("zoom"),waveformColor:acc,playedWaveformColor:muted,
      playheadColor:muted,axisLabelColor:muted,axisGridlineColor:"#44485a",
      // Étiquettes lisibles sur fond sombre dans la vue zoomée.
      segmentOptions:{overlayLabelColor:muted}},
    // Overview = repères visuels seulement : on masque les titres (couleur
    // transparente) pour ne garder que des barres claires et distinctes.
    overview:{container:$("overview"),waveformColor:muted,highlightColor:acc,
      playheadColor:muted,
      segmentOptions:{overlayLabelColor:"rgba(0,0,0,0)"}},
    mediaElement:audio,
    dataUri:{arraybuffer:`/api/jobs/${slug}/waveform.dat`},
    zoomLevels:[512,1024,2048,4096,8192],
    segmentOptions:{markers:true,overlay:true,overlayOpacity:0.28,
      overlayBorderWidth:2},
  };
  PeaksLib.init(options,(err,peaks)=>{
    if(err||!peaks){console.warn("Peaks indisponible:",err);return;}
    peaksInstance=peaks;
    syncPeaks();
    updateDiscMarker();
    // Fenêtre zoomée initiale = valeur du slider (240 s = 1 min derrière +
    // 3 min devant), pilotée en secondes plutôt qu'en niveaux discrets.
    applyZoomWindow(+($("zoom-window").value)||240);
    peaks.on("segments.dragend",({segment})=>{
      const i=+segment.id.slice(1);
      if(!EDIT[i])return;
      EDIT[i].start=segment.startTime;   // bord gauche = début (référence)
      const last=i===EDIT.length-1;
      // Bord droit d'une fin liée = frontière avec la suivante (déplace son
      // début) ; sinon = fin libre de la piste.
      if(!last&&EDIT[i].linked)EDIT[i+1].start=segment.endTime;
      else EDIT[i].endRaw=segment.endTime;
      commitEdit();
    });
    // Clic sur la forme d'onde (ou saut du lecteur) → recentre la vue zoomée
    // sur le point : 1 min derrière, 3 min devant (fenêtre de 4 min).
    peaks.on("player.seeked",t=>frameAround(t));
    wireZoomControls();
  });
}

// Applique une largeur de fenêtre zoomée (en secondes) à la vue « zoom » et
// synchronise le slider + son libellé.
function applyZoomWindow(seconds){
  seconds=Math.max(15,Math.min(600,Math.round(seconds)));
  const zv=peaksInstance&&peaksInstance.views&&peaksInstance.views.getView("zoomview");
  if(zv&&zv.setZoom)zv.setZoom({seconds});
  const sl=$("zoom-window");if(sl)sl.value=seconds;
  const lb=$("zoom-window-label");if(lb)lb.textContent=fmtClock(seconds);
}

// Recentre la vue zoomée autour d'un temps, sans changer le niveau de zoom en
// cours : ne fait que déplacer la fenêtre (25% avant / 75% après), pour ne pas
// écraser un zoom manuel de l'utilisateur à chaque clic/seek sur la forme d'onde.
function frameAround(t){
  const zv=peaksInstance&&peaksInstance.views&&peaksInstance.views.getView("zoomview");
  if(!zv||!zv.setStartTime)return;
  const win=+($("zoom-window").value)||240;
  zv.setStartTime(Math.max(0,t-win*0.25));
}

function wireZoomControls(){
  const sl=$("zoom-window");
  if(sl)sl.oninput=()=>applyZoomWindow(+sl.value);
  // +/- ajustent la largeur de la fenêtre (zoom in = fenêtre plus courte).
  $("btn-zoom-in").onclick=()=>applyZoomWindow((+$("zoom-window").value)-30);
  $("btn-zoom-out").onclick=()=>applyZoomWindow((+$("zoom-window").value)+30);
  $("btn-reset-cuts").onclick=resetCuts;
}

// mm:ss à partir d'un nombre de secondes.
function fmtClock(s){
  s=Math.round(s);
  return Math.floor(s/60)+":"+String(s%60).padStart(2,"0");
}

// Réinitialise toutes les coupes à l'analyse IA d'origine (après confirmation).
function resetCuts(){
  if(!EDIT_ORIG)return;
  confirmDialog(T("reset_confirm"),()=>{
    EDIT=JSON.parse(JSON.stringify(EDIT_ORIG));
    commitEdit();
    if(peaksInstance){const a=$("ed-audio");frameAround(a.currentTime||0);}
    toast(T("reset_done"));
  });
}

// Fin d'une piste liée = début de la suivante (la transition parlée reste donc
// à la fin de la piste) ; sinon = sa endRaw libre. La dernière piste garde
// toujours sa fin libre.
function relinkEnds(){
  for(let i=0;i<EDIT.length;i++){
    const last=i===EDIT.length-1;
    if(!last&&EDIT[i].linked)EDIT[i].end=EDIT[i+1].start;
    else EDIT[i].end=EDIT[i].endRaw;
  }
}

// Recalcule tout après une mutation : ordre, liaisons, lignes, segments,
// marqueur disque. Une seule porte d'entrée => cohérence garantie.
function commitEdit(){
  EDIT.sort((a,b)=>a.start-b.start);
  relinkEnds();
  renderRows();
  syncPeaks();
  updateDiscMarker();
}

function syncPeaks(){
  if(!peaksInstance)return;
  peaksInstance.segments.getSegments().forEach(s=>{
    if(+s.id.slice(1)>=EDIT.length)peaksInstance.segments.removeById(s.id);
  });
  EDIT.forEach((t,i)=>{
    const id="t"+i;
    const opts={startTime:t.start,endTime:Math.max(t.start+0.05,t.end),
      labelText:`${i+1}. ${t.title}`,color:SEG_COLORS[i%2]};
    const seg=peaksInstance.segments.getSegment(id);
    if(seg)seg.update(opts);
    else peaksInstance.segments.add({id,editable:true,...opts});
  });
}

function renderRows(){
  const rows=$("edit-rows");
  rows.innerHTML="";
  EDIT.forEach((t,i)=>rows.appendChild(buildEditRow(t,i)));
}

function buildEditRow(t,i){
  const row=document.createElement("div");
  const invalid=t.end<=t.start;
  row.className="track-row"+(invalid?" invalid":"");
  const last=i===EDIT.length-1;
  const canLock=!last;               // la dernière piste garde sa fin libre
  const endLocked=canLock&&t.linked; // fin grisée (= début de la suivante) ?
  row.innerHTML=`
    <span class="tn">${i+1}.</span>
    <input class="t-title" placeholder="${T('tr_title_ph')}">
    <input class="t-artist" placeholder="${T('tr_artist_ph')}">
    <span class="t-times">
      <input class="t-time t-start seekable" value="${fmtTime(t.start)}" title="${T('seek_tc')}">
      <button class="icon-btn icon-only mini t-setstart" title="${T('set_start')}"><span class="material-symbols-outlined">first_page</span></button>
      <span class="t-sep">→</span>
      <input class="t-time t-end${endLocked?'':' seekable'}" value="${fmtTime(t.end)}"${endLocked?' disabled':` title="${T('seek_tc')}"`}>
      ${canLock
        ? `<button class="icon-btn icon-only mini t-lock${t.linked?' on':''}" title="${t.linked?T('unlink_end'):T('link_end')}"><span class="material-symbols-outlined">${t.linked?'lock':'lock_open'}</span></button>`
        : `<span class="t-lock-spacer"></span>`}
      <button class="icon-btn icon-only mini t-setend" title="${T('set_end')}"><span class="material-symbols-outlined">last_page</span></button>
    </span>
    <button class="icon-btn icon-only mini row-play" title="${T('play')}"><span class="material-symbols-outlined">play_arrow</span></button>
    <button class="icon-btn icon-only mini row-del" title="${T('del')}"><span class="material-symbols-outlined">delete</span></button>`;
  row.querySelector(".t-title").value=t.title;
  row.querySelector(".t-artist").value=t.artist;
  // Texte : maj sans rebuild (préserve le focus), juste le label du segment.
  row.querySelector(".t-title").addEventListener("input",e=>{
    EDIT[i].title=e.target.value;
    if(peaksInstance){const s=peaksInstance.segments.getSegment("t"+i);
      if(s)s.update({labelText:(i+1)+". "+e.target.value});}
  });
  row.querySelector(".t-artist").addEventListener("input",e=>{
    EDIT[i].artist=e.target.value;});
  // Clic sur un timecode → place le curseur d'écoute pile dessus (et recadre
  // la vue zoomée) pour vérifier la coupe, sans empêcher l'édition manuelle.
  const seekTo=sec=>{const a=$("ed-audio");a.currentTime=Math.max(0,sec);};
  const si=row.querySelector(".t-start");
  si.addEventListener("click",()=>seekTo(EDIT[i].start));
  // Début : toujours éditable (référence de la piste).
  si.addEventListener("change",e=>{
    const v=parseTime(e.target.value);if(v==null)return;
    EDIT[i].start=v;commitEdit();seekTo(v);});
  // Fin : éditable seulement si déliée ou dernière piste.
  const ei=row.querySelector(".t-end");
  if(!endLocked){
    ei.addEventListener("click",()=>seekTo(EDIT[i].end));
    ei.addEventListener("change",e=>{
      const v=parseTime(e.target.value);if(v==null)return;
      EDIT[i].endRaw=v;commitEdit();seekTo(v);});
  }
  const lock=row.querySelector(".t-lock");
  if(lock)lock.onclick=()=>{EDIT[i].linked=!EDIT[i].linked;commitEdit();};
  row.querySelector(".t-setstart").onclick=()=>setFromPlayhead(i,"start");
  row.querySelector(".t-setend").onclick=()=>setFromPlayhead(i,"end");
  row.querySelector(".row-play").onclick=()=>{
    const a=$("ed-audio");a.currentTime=EDIT[i].start;a.play();};
  row.querySelector(".row-del").onclick=()=>{EDIT.splice(i,1);commitEdit();};
  return row;
}

// Reprend la position exacte du lecteur comme début ou fin de la piste.
function setFromPlayhead(i,which){
  const t=Math.max(0,$("ed-audio").currentTime||0);
  if(which==="start"){
    if(t>=EDIT[i].end){toast(T("err_times"),true);return;}
    EDIT[i].start=t;   // référence ; la fin liée de la piste précédente suivra
  }else{
    const last=i===EDIT.length-1;
    if(!last&&EDIT[i].linked){
      // Fin liée = frontière avec la suivante : on déplace son début.
      if(t<=EDIT[i].start){toast(T("err_times"),true);return;}
      EDIT[i+1].start=t;
    }else{
      if(t<=EDIT[i].start){toast(T("err_times"),true);return;}
      EDIT[i].endRaw=t;
    }
  }
  commitEdit();
}

// Frontières de disque : on remplit chaque disque jusqu'à 88 min de musique
// (durées cumulées), sans jamais couper une piste — la piste qui déborde
// démarre le disque suivant. Le marqueur est posé à son début (temps source).
function discBoundaries(){
  const out=[];let cumul=0;
  for(let i=0;i<EDIT.length;i++){
    const dur=Math.max(0,EDIT[i].end-EDIT[i].start);
    if(cumul>0&&cumul+dur>DISC_SECONDS){
      out.push({time:EDIT[i].start,disc:out.length+2});
      cumul=dur;
    }else{cumul+=dur;}
  }
  return out;
}

function updateDiscMarker(){
  const bounds=discBoundaries();
  const warn=$("disc-warn");
  if(warn){
    if(bounds.length){
      const totalMin=Math.round(
        EDIT.reduce((a,t)=>a+Math.max(0,t.end-t.start),0)/60);
      warn.classList.remove("hidden");
      warn.querySelector(".disc-warn-txt").textContent=
        T("disc_warn")(totalMin,bounds.length+1);
    }else{warn.classList.add("hidden");}
  }
  if(peaksInstance&&peaksInstance.points){
    peaksInstance.points.removeAll();
    bounds.forEach(b=>peaksInstance.points.add({
      time:b.time,labelText:T("disc_point")(b.disc),
      color:"#e0736f",editable:false}));
  }
}

$("btn-add-track2").onclick=()=>{
  const a=$("ed-audio");
  const start=a.currentTime||0;
  const end=Math.min(start+60,a.duration||start+60);
  EDIT.push({title:"",artist:"",start,end,endRaw:end,linked:false});
  commitEdit();
};

// Modale de confirmation Oui/Non (pas de confirm() natif — cf. DA du projet).
function confirmDialog(msg,onYes){
  const back=document.createElement("div");
  back.className="cfm-back";
  back.innerHTML=`
    <div class="cfm-box" role="dialog" aria-modal="true">
      <p class="cfm-msg"></p>
      <div class="cfm-actions">
        <button class="icon-btn cfm-no"><span class="material-symbols-outlined">close</span> <span></span></button>
        <button class="primary cfm-yes"><span class="material-symbols-outlined">restart_alt</span> <span></span></button>
      </div>
    </div>`;
  back.querySelector(".cfm-msg").textContent=msg;
  back.querySelector(".cfm-no span:last-child").textContent=T("dlg_no");
  back.querySelector(".cfm-yes span:last-child").textContent=T("dlg_yes");
  const close=()=>back.remove();
  back.querySelector(".cfm-no").onclick=close;
  back.addEventListener("click",e=>{if(e.target===back)close();});
  back.querySelector(".cfm-yes").onclick=()=>{close();onYes();};
  document.body.appendChild(back);
}

let _toastTimer=null;
function toast(msg,err){
  const el=$("toast");if(!el)return;
  el.textContent=msg;
  el.className="toast show"+(err?" err":"");
  clearTimeout(_toastTimer);
  _toastTimer=setTimeout(()=>el.classList.remove("show"),2400);
}

// ============================================================
// Validation → rendu
// ============================================================
$("btn-render").onclick=async()=>{
  if(!EDIT.length){alert(T("err_no_tracks"));return;}
  relinkEnds();
  const tracks=[];
  for(let i=0;i<EDIT.length;i++){
    const t=EDIT[i];
    if(!t.title.trim()){alert(T("err_titles"));return;}
    if(t.end<=t.start){alert(`${T("err_times")} — ${t.title||("#"+(i+1))}`);return;}
    const o={n:i+1,title:t.title.trim(),start:t.start,end:t.end};
    if(t.artist.trim())o.artist=t.artist.trim();
    tracks.push(o);
  }
  $("btn-render").disabled=true;
  try{
    const r=await fetch(`/api/jobs/${slug}/setlist`,{method:"PUT",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({tracks})});
    if(!r.ok){
      const d=await r.json().catch(()=>({}));
      throw new Error(d.detail||`HTTP ${r.status}`);
    }
    $("ed-audio").pause();
    await startRender();
  }catch(e){
    alert(e.message||T("err_generic"));
  }finally{
    $("btn-render").disabled=false;
  }
};

async function startRender(){
  currentPhase="render";
  const r=await fetch(`/api/jobs/${slug}/render`,{method:"POST"});
  if(!r.ok){alert(T("err_generic"));return;}
  runProgress(reedit?"render_title_reedit":"render_title",RENDER_STAGES,finish);
}

async function finish(){
  const m=await(await fetch(`/api/jobs/${slug}/manifest`)).json();
  $("summary").innerHTML=
    `<p><strong>${m.album.artist}</strong> — ${m.album.title}<br>`+
    `${m.tracks.length} ${T("tracks_word")}</p>`+
    // La note "créé en brouillon" est fausse pour une ré-édition : l'album
    // était déjà publié et le reste (aucun changement de `published` ici).
    (reedit?"":`<p class="note"><span class="material-symbols-outlined">visibility_off</span>`+
    `<span>${T("done_draft_note")}</span></p>`);
  $("btn-album").href=`/album/${slug}`;
  $("btn-manage").href=`/app/album/${slug}`;
  $("btn-download").href=`/api/jobs/${slug}/bundle`;
  if(reedit){
    document.querySelector("#step-done h1 [data-i18n]")
      .setAttribute("data-i18n","done_title_reedit");
    applyI18n(LANG());
  }
  show("step-done");
}

$("btn-again").onclick=()=>{
  slug=null;videoInfo=null;
  $("l-url").value="";
  history.replaceState(null,"",location.pathname);
  if(peaksInstance){peaksInstance.destroy();peaksInstance=null;}
  show("step-link");
};

// --- Reprise d'un projet en cours (#slug dans l'URL) ---
(async()=>{
  const h=decodeURIComponent(location.hash.slice(1));
  if(!h)return;
  try{
    const r=await fetch(`/api/jobs/${h}/manifest`);
    if(!r.ok)return;
    const m=await r.json();
    slug=h;
    const st=m.pipeline_state||{};
    if(st.download==="done"&&st.ai_markers==="done")openEditor();
    else await startPrepare();
  }catch(e){/* pas de reprise possible : rester sur l'étape lien */}
})();
