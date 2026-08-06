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
let multiMode=false;    // import multi-liens : 1 clip = 1 piste, concaténés
let multiThumb="";      // miniature du 1er clip lisible (pochette proposée)
let setlistSource=null; // provenance de la setlist (setlist.fm) + attribution
let currentPhase=null;  // "prepare" | "render" (pour le bouton réessayer)
let peaksInstance=null;
// Ré-édition d'un album déjà publié (bouton « Ouvrir l'éditeur audio » de la
// fiche de gestion, reprise via /app#slug) : même éditeur, mais l'album n'est
// pas "créé" — publié reste vrai tout du long, wording de fin différent.
let reedit=false;
// Sélecteur d'artiste (autocomplétion Deezer) — le même composant que sur la
// fiche de gestion. Saisir l'artiste au clavier laissait passer les fautes de
// frappe, qui créaient autant d'entités distinctes pour un même groupe.
let artistAC=null;
function mountArtistAC(id,label){
  // `allowFree` : un artiste absent de Deezer reste enregistrable, simplement
  // non lié à une page artiste.
  artistAC=L2M.autocomplete($("f-artist"),{
    endpoint:"/api/social/suggest/artists",icon:"music_note",allowFree:true,
    placeholder:"U2",value:{id:id||"",label:label||""}});
}
function artistValue(){
  const v=artistAC?artistAC.get():null;
  return {id:(v&&v.id)||"",label:((v&&v.label)||"").trim()};
}

// ============================================================
// Étape 1 — Analyse du lien
// ============================================================
function setAnalyzing(on,key="analyzing"){
  $("btn-analyze").disabled=on;
  const st=$("analyze-status");
  st.classList.toggle("hidden",!on);
  if(on)st.innerHTML=`<span class="material-symbols-outlined spin">progress_activity</span><span>${T(key)}</span>`;
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

// ── Import multi-liens ─────────────────────────────────────────────────────
$("multi-toggle").addEventListener("change",e=>{
  const on=e.target.checked;
  $("single-url-row").classList.toggle("hidden",on);
  $("multi-url-wrap").classList.toggle("hidden",!on);
  $("analyze-status").classList.add("hidden");
});
$("btn-analyze-multi").onclick=async()=>{
  const urls=$("l-urls").value.split("\n").map(s=>s.trim()).filter(Boolean);
  if(!urls.length){$("l-urls").focus();return;}
  setAnalyzing(true,"analyzing_multi");$("btn-analyze-multi").disabled=true;
  try{
    const r=await fetch("/api/tool/analyze-multi",{method:"POST",
      headers:{"Content-Type":"application/json"},body:JSON.stringify({urls})});
    if(!r.ok){
      const d=await r.json().catch(()=>({}));
      throw new Error(d.detail||`HTTP ${r.status}`);
    }
    const d=await r.json();
    setAnalyzing(false);
    fillFormMulti(d);
    show("step-form");
  }catch(e){
    setAnalyzing(false);
    analyzeError(`${T("err_analyze_multi")} ${e.message||""}`);
  }finally{$("btn-analyze-multi").disabled=false;}
};

function fillFormMulti(d){
  multiMode=true;videoInfo=null;setlistSource=null;
  multiThumb=d.thumbnail||"";
  const sug=d.suggestion||{};
  mountArtistAC(sug.artist_id||"",sug.artist||"");
  $("f-title").value="";$("f-date").value="";$("f-venue").value="";$("f-festival").value="";
  $("src-card").classList.add("hidden");
  $("setlist-src").classList.add("hidden");
  // Consigne setlist adaptée : 1 piste = 1 lien (pas de détection IA à l'écoute).
  $("setlist-hint").setAttribute("data-i18n","setlist_hint_multi");
  // « Ajouter une piste » n'a pas de sens ici (une piste sans lien est ignorée).
  $("btn-add-track").classList.add("hidden");
  // Flèches de réordonnancement visibles (l'ordre pilote la concaténation).
  $("track-rows").classList.add("reorderable");
  applyI18n(LANG());
  // Vidéo indisponible en multi-liens (V1 audio seul) : on masque l'option.
  const vwrap=$("f-video").closest(".opt-check");if(vwrap)vwrap.classList.add("hidden");
  $("f-video").checked=false;
  $("track-rows").innerHTML="";
  (d.clips||[]).forEach(c=>addFormRow({
    title:c.title||"",artist:c.artist||"",url:c.url,error:c.error||"",
    clipArtist:c.clip_artist||"",
  }));
  if(!(d.clips||[]).length)addFormRow({});
}

function exitMultiMode(){
  multiMode=false;multiThumb="";
  const vwrap=$("f-video").closest(".opt-check");
  if(vwrap)vwrap.classList.remove("hidden");
  $("f-video").checked=true;
  // Restaure les libellés du mode mono-lien.
  $("setlist-hint").setAttribute("data-i18n","setlist_hint");
  $("btn-add-track").classList.remove("hidden");
  $("track-rows").classList.remove("reorderable");
  applyI18n(LANG());
}
$("btn-manual").onclick=()=>{
  exitMultiMode();
  videoInfo=null;
  mountArtistAC("","");
  $("src-card").classList.add("hidden");
  if(!$("track-rows").children.length)addFormRow({});
  show("step-form");
};
$("btn-back-link").onclick=()=>{exitMultiMode();show("step-link");};

// ============================================================
// Étape 2 — Formulaire de vérification
// ============================================================
function fmtDur(s){
  s=Math.round(s||0);
  const h=Math.floor(s/3600),m=Math.floor((s%3600)/60),sec=s%60;
  return(h?`${h}:${String(m).padStart(2,"0")}`:m)+":"+String(sec).padStart(2,"0");
}
// Timecodes de l'éditeur : HH:MM:SS.XX, largeur fixe. Le format MM:SS.X
// précédent débordait du champ passé une heure de concert (« 100:39 » tronqué),
// et masquait les centièmes alors que c'est la précision qu'on ajuste.
function fmtTime(s){
  if(s==null||isNaN(s))return"";
  s=Math.max(0,s);
  const h=Math.floor(s/3600),m=Math.floor((s%3600)/60),sec=s%60;
  return`${String(h).padStart(2,"0")}:${String(m).padStart(2,"0")}:`
       +`${sec<10?"0":""}${sec.toFixed(2)}`;
}
function parseTime(v){
  v=(v||"").trim();if(!v)return null;
  const parts=v.split(":").map(Number);
  if(parts.some(isNaN))return null;
  let s=0;for(const p of parts)s=s*60+p;
  return s;
}

function fillForm(sug,video,ai){
  mountArtistAC(sug.artist_id||"",sug.artist||"");
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
    <span class="t-badge">${hasTime?`<span class="material-symbols-outlined" title="timecodes">schedule</span>${fmtDur(t.start)}`:""}</span>
    <span class="row-move">
      <button class="icon-btn icon-only row-up" title="${T("move_up")}"><span class="material-symbols-outlined">arrow_upward</span></button>
      <button class="icon-btn icon-only row-down" title="${T("move_down")}"><span class="material-symbols-outlined">arrow_downward</span></button>
    </span>
    <button class="icon-btn icon-only row-del" title="${T("del")}"><span class="material-symbols-outlined">delete</span></button>`;
  row.querySelector(".t-title").value=t.title||"";
  row.querySelector(".t-artist").value=t.artist||"";
  row.dataset.start=t.start!=null?t.start:"";
  row.dataset.end=t.end!=null?t.end:"";
  // Mode multi-liens : la ligne mémorise son lien source. Un lien illisible
  // est signalé et devra être corrigé ou retiré avant la préparation.
  if(t.url!==undefined)row.dataset.url=t.url||"";
  if(t.error){
    row.classList.add("row-error");
    row.title=t.error;
    const b=row.querySelector(".t-badge");
    if(b)b.innerHTML=`<span class="material-symbols-outlined" style="color:var(--like)" title="${t.error}">error</span>`;
  }else if(t.url){
    const b=row.querySelector(".t-badge");
    if(b)b.innerHTML=`<a href="${t.url}" target="_blank" rel="noopener" title="${T("open_link")}"><span class="material-symbols-outlined">link</span></a>`;
  }
  row.querySelector(".row-del").onclick=()=>{row.remove();renumber(rows);};
  // Réordonnancement (mode multi-liens) : l'ordre des lignes = l'ordre de
  // concaténation à la préparation. On déplace la ligne entière, ce qui
  // préserve titre/artiste/lien saisis.
  row.querySelector(".row-up").onclick=()=>{
    const prev=row.previousElementSibling;
    if(prev)rows.insertBefore(row,prev);renumber(rows);};
  row.querySelector(".row-down").onclick=()=>{
    const next=row.nextElementSibling;
    if(next)rows.insertBefore(next,row);renumber(rows);};
  rows.appendChild(row);
  renumber(rows);
}
function renumber(container){
  const rows=[...container.querySelectorAll(".track-row")];
  rows.forEach((r,i)=>{
    r.querySelector(".tn").textContent=(i+1)+".";
    const up=r.querySelector(".row-up"),down=r.querySelector(".row-down");
    if(up)up.disabled=(i===0);
    if(down)down.disabled=(i===rows.length-1);
  });
}
$("btn-add-track").onclick=()=>addFormRow({});

$("btn-create").onclick=async()=>{
  const av=artistValue();
  const artist=av.label;
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
  const album={artist,artist_id:av.id,title,date:$("f-date").value||null,
    venue:$("f-venue").value.trim()||null,
    festival:$("f-festival").value.trim()||null};
  let body;
  if(multiMode){
    // Un lien illisible casserait la concaténation : on bloque avant l'envoi.
    if($("track-rows").querySelector(".track-row.row-error")){
      alert(T("err_multi_broken"));return;}
    const clips=[...$("track-rows").querySelectorAll(".track-row")].map(r=>{
      const url=r.dataset.url||"";
      if(!url)return null;
      const c={url,title:r.querySelector(".t-title").value.trim()};
      const a=r.querySelector(".t-artist").value.trim();
      if(a)c.artist=a;
      return c;
    }).filter(Boolean);
    if(!clips.length){alert(T("err_required"));return;}
    body={album,target:$("f-target").value,tracks:[],clips,
      thumbnail_url:multiThumb,video:false,source_url:""};
  }else{
    body={album,tracks,
      target:$("f-target").value,
      source_url:(videoInfo&&videoInfo.webpage_url)||$("l-url").value.trim(),
      video:$("f-video").checked,
      thumbnail_url:(videoInfo&&videoInfo.thumbnail)||"",
      setlistfm_url:(setlistSource&&setlistSource.url)||"",
      duration:(videoInfo&&videoInfo.duration)||null,
    };
  }
  $("btn-create").disabled=true;
  try{
    const r=await fetch("/api/jobs",{method:"POST",
      headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    if(!r.ok){
      const d=await r.json().catch(()=>({}));
      const err=new Error(d.detail||`HTTP ${r.status}`);
      err.status=r.status;
      throw err;
    }
    slug=(await r.json()).slug;
    location.hash=slug;   // reprise possible si l'onglet se ferme
    await startPrepare();
  }catch(e){
    // 409 = un brouillon ou album existe déjà sous ce slug : plutôt qu'un
    // simple message d'erreur, on propose d'aller directement le reprendre
    // dans les brouillons (sinon l'utilisateur doit deviner où il est parti).
    if(e.status===409){
      confirmDialog(e.message||T("err_generic"),()=>{location.href="/app/drafts";},{
        icon:"drafts",
        yes:T("dup_goto_drafts"),
        no:T("dup_close"),
      });
    }else{
      alert(e.message||T("err_generic"));
    }
  }finally{
    $("btn-create").disabled=false;
  }
};

// ============================================================
// Étapes 3 & 5 — Progression SSE
// ============================================================
const PREP_STAGES=["download","preview","waveform","ai_markers"];
const RENDER_STAGES=["render","tags","artwork","disc"];

function runProgress(titleKey,stages,onComplete,onCancelled){
  show("step-progress");
  $("progress-title").textContent=T(titleKey);
  $("progress-err").classList.add("hidden");
  $("progress-actions").classList.add("hidden");
  hideSkip();
  // Bouton d'arrêt : seulement pendant un rendu (la préparation est courte et
  // dispose déjà de son propre « passer cette étape »).
  const cancelBtn=$("btn-cancel-render");
  if(cancelBtn)cancelBtn.classList.toggle("hidden",currentPhase!=="render");
  const ul=$("stages");ul.innerHTML="";
  const items={};
  stages.forEach(s=>{
    const li=document.createElement("li");
    // Multi-liens : plusieurs sources téléchargées -> libellé au pluriel.
    const label=(s==="download"&&multiMode)?T("stage_download_multi"):T("stage_"+s);
    li.innerHTML=`<div class="stage-head"><span class="dot"></span>`+
      `<span class="stage-label">${label}</span>`+
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
          // Compteur de pistes quand le stage en fournit un (render) : le
          // ré-encodage vidéo dure des minutes par piste, un simple « % »
          // donne l'impression que ça ne bouge pas.
          pct.textContent=info.total
            ?`${info.done}/${info.total} · ${Math.round(info.pct)} %`
            :(info.phase?T("phase_"+info.phase)+" ":"")+Math.round(info.pct)+" %";
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
    if(ev.status==="paused"&&li){
      li.classList.add("indet");
      li.querySelector(".stage-pct").textContent=T("paused_word");
    }
    if(ev.status==="cancelled"){
      es.close();
      if($("btn-cancel-render"))$("btn-cancel-render").classList.add("hidden");
      toast(T("cancelled_msg"));
      if(onCancelled)onCancelled();
      return;
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
let EDIT=[];   // [{title, artist, start, end, linked, oi}]
let EDIT_ORIG=null;   // instantané de l'analyse IA d'origine (pour "Réinitialiser")
let editMulti=false;      // album multi-liens : réordonnancement possible
let reorderPending=false; // ordre modifié localement, pas encore ré-assemblé

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
  wireMediaResilience();
  if(peaksInstance){peaksInstance.destroy();peaksInstance=null;}
  EDIT=buildEditFromTracks(m.tracks);
  // Réordonnancement réservé aux albums multi-liens (pistes = chansons entières
  // et contiguës). `oi` = index de la piste dans le manifeste (ordre temporel,
  // préservé par l'autosave qui re-trie toujours par start) : c'est la
  // permutation envoyée à /reorder.
  editMulti=!!(m.source&&(m.source.multi||m.source.clips));
  EDIT.forEach((t,i)=>{t.oi=i;});
  reorderPending=false;
  $("reorder-bar").classList.add("hidden");
  // Copie profonde figée : référence pour le bouton « Réinitialiser ».
  // Toujours prise sur le manifeste, jamais sur la reprise locale : sinon
  // « Réinitialiser » ne ramènerait plus à l'analyse IA d'origine.
  EDIT_ORIG=JSON.parse(JSON.stringify(EDIT));
  // Reprise d'un travail non validé (F5, onglet fermé, session SSO expirée).
  // En création, le brouillon serveur est écrit en continu : le manifeste fait
  // foi et la copie locale ne sert que s'il n'a rien reçu (première session
  // hors ligne). En ré-édition, rien n'est autosauvé côté serveur — la copie
  // locale est alors la seule reprise possible.
  const localEdit=loadEditLocal(slug);
  if(localEdit&&(reedit||!EDIT.length)){
    EDIT=localEdit.edit;
    // Ordre local non mappable au manifeste : on désactive le réordonnancement
    // pour cette session (oi ne correspondrait plus aux index du manifeste).
    editMulti=false;
    toast(T("edit_restored"));
  }else if(localEdit)clearEditLocal(slug);
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
  saveEditLocal();
  scheduleDraftSave();
}

// --- Brouillon serveur des coupes ----------------------------------------
// Attendu de longue date mais jamais branché : le backend expose de quoi
// écrire les coupes sans rien déclencher, mais l'éditeur ne les envoyait
// qu'au clic sur « Valider ». Fermer l'onglet perdait tout le découpage.
// On enregistre donc en continu (débounce), et le brouillon rouvert repart de
// l'état réel plutôt que de l'analyse IA.
// Exception : la ré-édition d'un album publié n'écrit rien tant que
// l'utilisateur n'a pas validé — on ne modifie pas un album en ligne à chaque
// glissement de marqueur. La copie locale suffit dans ce cas.
// Chaque modification déclenche son enregistrement (demande du 2026-08-03) :
// un timecode changé, un cadenas, un ajout, une suppression, un marqueur glissé
// sont tous des actions ponctuelles, enregistrées **immédiatement**. Le délai de
// 3 s d'origine ne servait qu'à absorber les rafales, mais il laissait une
// fenêtre pendant laquelle quitter la page perdait la dernière action.
//
// Seule exception : la frappe au clavier (titre, artiste), qui émet un
// évènement par lettre. Enregistrer à chaque touche réécrirait le manifest
// des dizaines de fois pour un seul mot, sur un stockage partagé avec la prod —
// on attend donc une brève pause dans la saisie.
const SAVE_NOW=0, SAVE_TYPING=700;
let draftTimer=null, draftInFlight=false, draftDirty=false;
function scheduleDraftSave(delay){
  if(reedit||!slug)return;
  // Ordre modifié non appliqué : ne pas autosauver (le serveur re-trie par
  // start et figerait des timecodes incohérents avec l'ordre affiché).
  if(reorderPending)return;
  clearTimeout(draftTimer);
  draftTimer=setTimeout(runDraftSave,delay==null?SAVE_NOW:delay);
}
// Un enregistrement à la fois : sans ce verrou, glisser un marqueur émettrait
// des requêtes concurrentes dont l'ordre d'arrivée n'est pas garanti — la plus
// ancienne pourrait écraser la plus récente. Toute modification survenue
// pendant l'envoi est reprise juste après.
async function runDraftSave(){
  if(draftInFlight){draftDirty=true;return;}
  draftInFlight=true;
  try{await saveDraft();}
  finally{
    draftInFlight=false;
    if(draftDirty){draftDirty=false;scheduleDraftSave(SAVE_NOW);}
  }
}
async function saveDraft(opts){
  opts=opts||{};
  if(reedit||!slug||!EDIT.length)return false;
  // Le serveur refuse une piste sans titre ou à bornes inversées. On n'envoie
  // donc pas, mais **on le dit** : cet abandon était muet, si bien qu'une seule
  // piste sans titre suffisait à bloquer tous les enregistrements d'une session
  // entière sans le moindre signal (2026-08-03, concert Linkin Park).
  const noTitle=EDIT.some(t=>!t.title.trim());
  const badTimes=EDIT.some(t=>t.end<=t.start);
  if(noTitle||badTimes){
    draftState("blocked",noTitle?"draft_blocked_title":"draft_blocked_times");
    return false;
  }
  // `n` est obligatoire côté serveur (SetlistTrackIn) : l'omettre faisait
  // échouer chaque autosave en 422, silencieusement. Même charge utile que le
  // bouton « Valider », artiste omis plutôt qu'envoyé vide.
  const tracks=EDIT.map((t,i)=>{
    const o={n:i+1,title:t.title.trim(),start:t.start,end:t.end};
    if((t.artist||"").trim())o.artist=t.artist.trim();
    return o;
  });
  try{
    const r=await fetch(`/api/jobs/${slug}/setlist`,{method:"PUT",
      redirect:"manual",headers:{"Content-Type":"application/json"},
      // `keepalive` : la requête survit à la navigation. Sans lui, quitter la
      // page (clic sur le logo) annulait l'envoi en vol et perdait les
      // dernières coupes — le cas exact rapporté le 2026-08-03.
      keepalive:!!opts.flush,
      body:JSON.stringify({tracks})});
    // Distinguer les deux causes : une redirection ou un 401/403 = session
    // expirée ; tout autre code = refus applicatif. Les confondre affichait
    // « Session expirée » sur une erreur de validation, et surtout laissait
    // croire à un simple souci d'authentification alors que rien ne
    // s'enregistrait.
    if(r.type==="opaqueredirect"||r.status===401||r.status===403){
      showSessionBanner();draftState("err");return false;
    }
    if(!r.ok){
      const d=await r.json().catch(()=>({}));
      console.warn("brouillon non enregistré:",r.status,d.detail||d);
      draftState("err");return false;
    }
    hideSessionBanner();
    draftState("ok");
    return true;
  }catch(e){draftState("err");/* réseau : la copie locale reste le filet */}
  return false;
}

// Quitter la page ne doit plus rien perdre : le débounce de 3 s laissait
// systématiquement une fenêtre pendant laquelle un clic sur le logo emportait
// les dernières coupes. `pagehide` couvre la navigation et la fermeture ;
// `visibilitychange` rattrape les cas où `pagehide` n'est pas émis (mobile).
function flushDraftSave(){
  if(reedit||!slug||!EDIT.length)return;
  clearTimeout(draftTimer);
  saveDraft({flush:true});
}
window.addEventListener("pagehide",flushDraftSave);
document.addEventListener("visibilitychange",()=>{
  if(document.visibilityState==="hidden")flushDraftSave();
});

// État visible de l'enregistrement. Un échec doit se voir : c'est l'absence de
// signal qui a fait perdre une heure de découpage le 2026-08-02.
// `blocked` = rien n'a été envoyé (état d'édition invalide) ; distinct d'un
// échec réseau, et surtout jamais silencieux.
function draftState(kind,reasonKey){
  const el=$("draft-saved");
  if(!el)return;
  const ic=el.querySelector(".material-symbols-outlined");
  const tx=el.querySelector("[data-i18n]");
  el.classList.remove("hidden","err");
  clearTimeout(draftState._hide);
  if(kind==="ok"){
    ic.textContent="cloud_done";
    if(tx)tx.textContent=T("draft_saved");
    draftState._hide=setTimeout(()=>el.classList.add("hidden"),2500);
  }else if(kind==="blocked"){
    el.classList.add("err");
    ic.textContent="cloud_alert";
    if(tx)tx.textContent=T(reasonKey||"draft_blocked_title");
  }else{
    el.classList.add("err");
    ic.textContent="cloud_off";
    if(tx)tx.textContent=T("draft_failed");
  }
}

// --- Sauvegarde locale des coupes en cours d'édition ---------------------
// Rien n'est persisté côté serveur avant « Valider » : une session SSO
// expirée, un onglet fermé ou un crash effaçaient une heure d'ajustements.
// On garde donc une copie locale, réécrite à chaque modification et relue à
// l'ouverture de l'éditeur. Purgée seulement après un envoi réussi.
const EDIT_KEY=s=>`l2m-edit-${s}`;
function saveEditLocal(){
  if(!slug||!EDIT.length)return;
  try{localStorage.setItem(EDIT_KEY(slug),
    JSON.stringify({at:Date.now(),edit:EDIT}));}catch(e){}
}
function loadEditLocal(s){
  try{
    const d=JSON.parse(localStorage.getItem(EDIT_KEY(s))||"null");
    return d&&Array.isArray(d.edit)&&d.edit.length?d:null;
  }catch(e){return null;}
}
function clearEditLocal(s){try{localStorage.removeItem(EDIT_KEY(s));}catch(e){}}

// --- Survie du lecteur à une expiration de session SSO --------------------
// L'audio de travail est streamé par plages : chaque avance dans la forme
// d'onde refait une requête HTTP, et chacune repasse par le forward-auth
// Authentik. Session expirée => 302 vers la page de login : un <audio> ne sait
// pas suivre une redirection d'authentification, il reçoit du HTML, coupe le
// son, gèle Peaks.js et remet currentTime à 0 (lecteur grisé, durée 0:00).
// On détecte, on répare tout seul si c'était juste le réseau, et on propose une
// reconnexion en nouvel onglet sinon — jamais un rechargement, qui viderait
// l'éditeur.
let lastGoodTime=0;       // dernière position lue, l'erreur média remet à 0
let mediaRecovering=false;
let sessionPoll=null;

// `redirect:"manual"` : un 302 d'Authentik donne une réponse opaque
// (type "opaqueredirect") au lieu d'être suivi en cross-origin — c'est le
// signal d'expiration, sans dépendre du contenu de la réponse.
// Le verdict porte sur la **session**, donc sur la présence d'une identité —
// pas sur `authenticated`, qui exige en plus l'appartenance au groupe
// `live2mp3-user`. L'outpost Authentik de la preprod ne transmet pas toujours
// l'en-tête de groupes (constaté le 2026-07-20, revu le 2026-08-03) : le
// bandeau « Session expirée » s'affichait alors que la session était valide et
// que le lecteur audio fonctionnait — un faux positif à chaque sonde.
async function sessionAlive(){
  try{
    const r=await fetch("/api/me",{redirect:"manual",cache:"no-store"});
    if(r.type==="opaqueredirect"||!r.ok)return false;
    const d=await r.json().catch(()=>null);
    // Réponse non-JSON = page de login servie à la place de l'API : session
    // bel et bien perdue, même si le code HTTP est 200.
    if(!d)return false;
    return !!(d.username||d.authenticated);
  }catch(e){return false;}
}

function showSessionBanner(){$("session-warn").classList.remove("hidden");}
function hideSessionBanner(){$("session-warn").classList.add("hidden");}

async function recoverMedia(){
  if(mediaRecovering||!slug)return;
  mediaRecovering=true;
  try{
    if(!await sessionAlive()){showSessionBanner();return;}
    hideSessionBanner();
    // Session valide : coupure passagère. On recharge la source et on revient
    // où on en était (le paramètre ne sert qu'à contourner le cache).
    const a=$("ed-audio");
    const t=lastGoodTime;
    a.src=`/api/jobs/${slug}/audio?r=${Date.now()}`;
    a.addEventListener("loadedmetadata",()=>{
      try{a.currentTime=t;}catch(e){}
    },{once:true});
    a.load();
  }finally{mediaRecovering=false;}
}

function wireMediaResilience(){
  const a=$("ed-audio");
  if(a.dataset.resilient)return;   // openEditor() peut être rappelé
  a.dataset.resilient="1";
  a.addEventListener("timeupdate",()=>{if(a.currentTime>0)lastGoodTime=a.currentTime;});
  a.addEventListener("error",recoverMedia);
  $("btn-session-relogin").onclick=()=>{
    // Nouvel onglet, jamais une navigation : l'éditeur courant garde ses
    // coupes en mémoire et redevient fonctionnel dès le cookie réémis.
    window.open("/outpost.goauthentik.io/start?rd=/app","_blank","noopener");
    const iv=setInterval(async()=>{
      if(await sessionAlive()){clearInterval(iv);hideSessionBanner();recoverMedia();}
    },2000);
    setTimeout(()=>clearInterval(iv),120000);
  };
  // Sonde périodique : prévenir pendant que l'éditeur répond encore, plutôt
  // que de laisser l'utilisateur découvrir la panne sur un lecteur mort.
  // Deux échecs consécutifs avant d'alarmer : une coupure réseau d'une seconde
  // ou un aller-retour un peu long ne sont pas une session expirée, et le
  // bandeau ne se retire qu'à la sonde suivante — le faux positif restait donc
  // affiché une minute pour rien.
  clearInterval(sessionPoll);
  let misses=0;
  sessionPoll=setInterval(async()=>{
    if($("step-editor").classList.contains("hidden"))return;
    if(await sessionAlive()){misses=0;hideSessionBanner();}
    else if(++misses>=2)showSessionBanner();
  },60000);
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
      <button class="icon-btn icon-only mini t-setstart" title="${T('set_start')}"><span class="material-symbols-outlined">first_page</span></button>
      <input class="t-time t-start seekable" value="${fmtTime(t.start)}" title="${T('seek_tc')}">
      <span class="t-sep">→</span>
      <button class="icon-btn icon-only mini t-setend" title="${T('set_end')}"><span class="material-symbols-outlined">last_page</span></button>
      <input class="t-time t-end${endLocked?'':' seekable'}" value="${fmtTime(t.end)}"${endLocked?' disabled':` title="${T('seek_tc')}"`}>
      ${canLock
        ? `<button class="icon-btn icon-only mini t-lock${t.linked?' on':''}" title="${t.linked?T('unlink_end'):T('link_end')}"><span class="material-symbols-outlined">${t.linked?'lock':'lock_open'}</span></button>`
        : `<span class="t-lock-spacer"></span>`}
    </span>
    <span class="t-actions-sep"></span>
    ${editMulti?`<span class="row-move">
      <button class="icon-btn icon-only mini row-up" title="${T('move_up')}"${i===0?' disabled':''}><span class="material-symbols-outlined">arrow_upward</span></button>
      <button class="icon-btn icon-only mini row-down" title="${T('move_down')}"${i===EDIT.length-1?' disabled':''}><span class="material-symbols-outlined">arrow_downward</span></button>
    </span>`:''}
    <button class="icon-btn icon-only mini row-play" title="${T('play')}"><span class="material-symbols-outlined">play_arrow</span></button>
    <button class="icon-btn icon-only mini row-del" title="${T('del')}"><span class="material-symbols-outlined">delete</span></button>`;
  row.querySelector(".t-title").value=t.title;
  row.querySelector(".t-artist").value=t.artist;
  // Texte : maj sans rebuild (préserve le focus), juste le label du segment.
  row.querySelector(".t-title").addEventListener("input",e=>{
    EDIT[i].title=e.target.value;
    if(peaksInstance){const s=peaksInstance.segments.getSegment("t"+i);
      if(s)s.update({labelText:(i+1)+". "+e.target.value});}
    // Ces champs ne passent pas par commitEdit() (qui reconstruit les lignes et
    // ferait perdre le focus en pleine frappe) : sans planification explicite,
    // saisir un titre n'enregistrait rien. Combiné au garde-fou ci-dessous, qui
    // bloque tant qu'une piste est sans titre, une piste ajoutée puis nommée
    // laissait le brouillon définitivement non enregistré (2026-08-03).
    scheduleDraftSave(SAVE_TYPING);
  });
  row.querySelector(".t-artist").addEventListener("input",e=>{
    EDIT[i].artist=e.target.value;scheduleDraftSave(SAVE_TYPING);});
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
  const up=row.querySelector(".row-up"),down=row.querySelector(".row-down");
  if(up)up.onclick=()=>moveEditRow(i,-1);
  if(down)down.onclick=()=>moveEditRow(i,1);
  return row;
}

// Réordonnancement (albums multi-liens) : on permute les pistes localement puis
// l'utilisateur applique (re-concaténation serveur). L'audio n'est pas encore
// ré-assemblé ici ; les timecodes affichés restent ceux du master actuel
// jusqu'à l'application — d'où la barre d'action explicite.
function moveEditRow(i,dir){
  const j=i+dir;
  if(j<0||j>=EDIT.length)return;
  const tmp=EDIT[i];EDIT[i]=EDIT[j];EDIT[j]=tmp;
  reorderPending=true;
  renderRows();
  updateReorderBar();
}
function updateReorderBar(){
  const bar=$("reorder-bar");
  if(bar)bar.classList.toggle("hidden",!reorderPending);
  // Tant que l'ordre n'est pas ré-assemblé, la validation est bloquée : les
  // MP3 seraient produits dans l'ordre du master actuel, pas celui affiché.
  const rb=$("btn-render");if(rb)rb.disabled=reorderPending;
}
async function applyReorder(){
  const order=EDIT.map(t=>t.oi);
  const apply=$("btn-reorder-apply");apply.disabled=true;
  const cancel=$("btn-reorder-cancel");if(cancel)cancel.disabled=true;
  toast(T("reorder_applying"));
  try{
    const r=await fetch(`/api/jobs/${slug}/reorder`,{method:"POST",
      headers:{"Content-Type":"application/json"},body:JSON.stringify({order})});
    if(!r.ok){const d=await r.json().catch(()=>({}));
      throw new Error(d.detail||`HTTP ${r.status}`);}
    clearEditLocal(slug);   // l'ordre local est désormais dans le master
    reorderPending=false;
    await openEditor();     // recharge tout depuis le manifeste ré-assemblé
    toast(T("reorder_done"));
  }catch(e){
    alert(`${T("err_reorder")} ${e.message||""}`);
  }finally{
    apply.disabled=false;if(cancel)cancel.disabled=false;
  }
}
$("btn-reorder-apply").onclick=applyReorder;
$("btn-reorder-cancel").onclick=()=>{reorderPending=false;openEditor();};

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
// `opts` : {yes, no, icon, onNo} pour réutiliser la modale hors réinitialisation.
function confirmDialog(msg,onYes,opts){
  opts=opts||{};
  const back=document.createElement("div");
  back.className="cfm-back";
  back.innerHTML=`
    <div class="cfm-box" role="dialog" aria-modal="true">
      <p class="cfm-msg"></p>
      <div class="cfm-actions">
        <button class="icon-btn cfm-no"><span class="material-symbols-outlined">close</span> <span></span></button>
        <button class="primary cfm-yes"><span class="material-symbols-outlined">${opts.icon||"restart_alt"}</span> <span></span></button>
      </div>
    </div>`;
  // Le message peut contenir des \n (paragraphes) : pre-line posé ici plutôt
  // que dans app.css, en cours de refonte sur une autre branche de travail.
  const msgEl=back.querySelector(".cfm-msg");
  msgEl.style.whiteSpace="pre-line";
  msgEl.textContent=msg;
  back.querySelector(".cfm-no span:last-child").textContent=opts.no||T("dlg_no");
  back.querySelector(".cfm-yes span:last-child").textContent=opts.yes||T("dlg_yes");
  let answered=false;
  const close=()=>{
    back.remove();
    if(!answered&&opts.onNo)opts.onNo();
  };
  back.querySelector(".cfm-no").onclick=close;
  back.addEventListener("click",e=>{if(e.target===back)close();});
  back.querySelector(".cfm-yes").onclick=()=>{answered=true;back.remove();onYes();};
  document.body.appendChild(back);
}

// --- Avertissement « export MP4 » ---
// Cocher la case fait passer le rendu de ~5 min à 30 min–2 h (ré-encodage
// H.264 de tout le concert). Sans avertissement, l'utilisateur croit à un
// blocage pendant la découpe.
(function(){
  const cb=$("f-video");if(!cb)return;
  cb.addEventListener("change",()=>{
    if(!cb.checked)return;
    confirmDialog(T("mp4_warn"),()=>{},{
      icon:"movie",
      yes:T("mp4_warn_yes"),
      no:T("mp4_warn_no"),
      onNo:()=>{cb.checked=false;},
    });
  });
})();

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
    // Coupes acceptées par le serveur : la copie de secours n'a plus lieu
    // d'être (et rouvrir l'éditeur doit repartir du manifeste).
    clearEditLocal(slug);
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
  if(r.status===409){
    // Un rendu tourne déjà pour cet album : on rattache l'affichage au lieu
    // d'en lancer un second, qui écrirait les mêmes fichiers en parallèle.
    toast(T("already_rendering"),true);
  }else if(!r.ok){alert(T("err_generic"));return;}
  runProgress(reedit?"render_title_reedit":"render_title",RENDER_STAGES,finish,
              ()=>show("step-editor"));
}

// --- Arrêt du rendu ---
$("btn-cancel-render").onclick=()=>{
  confirmDialog(T("cancel_confirm"),async()=>{
    try{
      const r=await fetch(`/api/jobs/${slug}/render/cancel`,{method:"POST"});
      if(!r.ok)throw new Error();
      // La suite arrive par SSE (statut « cancelled ») : retour à l'éditeur.
    }catch(e){toast(T("err_generic"),true);}
  },{icon:"stop_circle",yes:T("cancel_yes"),no:T("cancel_no")});
};

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
  watchVideoRender();
}

// --- Rendu vidéo de phase 2 (arrière-plan) --------------------------------
// Depuis le découplage du 2026-08-03, l'écran final s'affiche dès que l'album
// audio est prêt ; le MP4 continue dans un job séparé. On ne bloque donc plus
// l'utilisateur, mais on lui montre où en est ce rendu — avec un vrai
// pourcentage, lu sur la file (le worker y publie l'avancement ffmpeg réel).
let videoPoll=null;
function stopVideoWatch(){clearInterval(videoPoll);videoPoll=null;}

async function videoJob(){
  try{
    const q=await(await fetch("/api/render-queue")).json();
    return (q.items||[]).find(i=>i.slug===slug&&i.kind==="video")||null;
  }catch(e){return undefined;}   // undefined = indéterminé (réseau), pas « fini »
}

function paintVideo(job){
  const card=$("video-progress");
  if(!card)return;
  card.classList.remove("hidden","vp-queued","vp-running","vp-done");
  const pctEl=card.querySelector(".vp-pct"),fill=$("vp-fill");
  const lbl=card.querySelector(".vp-label");
  if(!job){   // plus dans la file = terminé
    card.classList.add("vp-done");
    lbl.setAttribute("data-i18n","video_done_title");
    card.querySelector(".vp-note").setAttribute("data-i18n","video_done_note");
    fill.style.width="100%";pctEl.textContent="";
    applyI18n(LANG());
    return;
  }
  const queued=job.state==="queued",paused=job.state==="paused";
  card.classList.add(queued?"vp-queued":"vp-running");
  lbl.setAttribute("data-i18n",queued?"video_queued_title":"video_running_title");
  card.querySelector(".vp-note").setAttribute("data-i18n","video_running_note");
  // `pct` est nul tant que ffmpeg n'a pas émis sa première mesure (ouverture du
  // fichier source) : afficher « 0 % » laisserait croire à un blocage.
  const p=job.pct;
  fill.style.width=(p==null?0:Math.round(p))+"%";
  pctEl.textContent=paused?T("paused_word")
    :(p==null?T("running_word"):Math.round(p)+" %");
  applyI18n(LANG());
}

async function watchVideoRender(){
  stopVideoWatch();
  const first=await videoJob();
  // Aucun rendu vidéo pour cet album (import audio-only, ou déjà terminé) :
  // le bloc reste masqué plutôt que d'annoncer une fin qui n'a pas eu lieu.
  if(!first)return;
  paintVideo(first);
  videoPoll=setInterval(async()=>{
    if($("step-done").classList.contains("hidden")){stopVideoWatch();return;}
    const job=await videoJob();
    if(job===undefined)return;    // réseau : on garde le dernier état affiché
    paintVideo(job);
    if(!job)stopVideoWatch();
  },4000);
}

$("btn-again").onclick=()=>{
  stopVideoWatch();
  $("video-progress").classList.add("hidden");
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
    // Un rendu en cours reprend la main sur l'éditeur : sans ça, revenir sur la
    // page pendant un encodage rouvrait l'éditeur, sans aucun moyen de revoir
    // l'avancement — et un second « Valider » lançait un rendu concurrent.
    let queued=null;
    try{
      const q=await(await fetch("/api/render-queue")).json();
      queued=(q.items||[]).find(i=>i.slug===h)||null;
    }catch(e){}
    if(queued&&queued.kind==="video"){
      // Phase 2 seule : l'audio est déjà rendu, l'album est complet côté son.
      // On revient donc à l'écran final (qui affiche l'avancement du MP4),
      // surtout pas à l'écran de progression du rendu audio.
      reedit=!!m.published;
      await finish();
      return;
    }
    if(queued){
      currentPhase="render";
      reedit=!!m.published;
      runProgress(reedit?"render_title_reedit":"render_title",RENDER_STAGES,
                  finish,()=>show("step-editor"));
      return;
    }
    const st=m.pipeline_state||{};
    if(st.download==="done"&&st.ai_markers==="done")openEditor();
    else await startPrepare();
  }catch(e){/* pas de reprise possible : rester sur l'étape lien */}
})();
