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
let currentPhase=null;  // "prepare" | "render" (pour le bouton réessayer)
let peaksInstance=null;

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
  const ul=$("stages");ul.innerHTML="";
  const items={};
  stages.forEach(s=>{
    const li=document.createElement("li");
    li.innerHTML=`<span class="dot"></span><span>${T("stage_"+s)}</span><span class="stage-pct"></span>`;
    ul.appendChild(li);items[s]=li;
  });
  const es=new EventSource(`/api/jobs/${slug}/events`);
  es.onmessage=e=>{
    const ev=JSON.parse(e.data);
    const li=items[ev.stage];
    if(li){
      li.classList.toggle("running",ev.status==="running");
      li.classList.toggle("done",ev.status==="done");
      const pct=li.querySelector(".stage-pct");
      if(ev.status==="running"&&ev.info){
        if(ev.info.pct!=null)pct.textContent=Math.round(ev.info.pct)+" %";
        else if(ev.info.phase)pct.textContent=T("phase_"+ev.info.phase);
      }else if(ev.status==="done"){
        pct.textContent="";
        if(ev.stage==="ai_markers"&&ev.info&&ev.info.source)
          pct.textContent=T("src_"+ev.info.source)||"";
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

async function openEditor(){
  show("step-editor");
  const m=await(await fetch(`/api/jobs/${slug}/manifest`)).json();
  const audio=$("ed-audio");
  audio.src=`/api/jobs/${slug}/audio`;
  if(peaksInstance){peaksInstance.destroy();peaksInstance=null;}
  $("edit-rows").innerHTML="";
  const options={
    zoomview:{container:$("zoom")},
    overview:{container:$("overview")},
    mediaElement:audio,
    dataUri:{arraybuffer:`/api/jobs/${slug}/waveform.dat`},
    zoomLevels:[256,512,1024,2048,4096],
  };
  Peaks.init(options,(err,peaks)=>{
    if(err){console.warn("Peaks indisponible:",err);}
    peaksInstance=peaks||null;
    m.tracks.forEach((t,i)=>{
      if(t.start==null||t.end==null)return;
      if(peaksInstance)peaksInstance.segments.add({
        id:"t"+t.n,startTime:t.start,endTime:t.end,
        labelText:`${t.n}. ${t.title}`,editable:true,
        color:SEG_COLORS[i%2]});
      addEditRow(t);
    });
    if(peaksInstance){
      peaksInstance.on("segments.dragend",({segment})=>{
        const row=$("edit-rows").querySelector(`[data-seg="${segment.id}"]`);
        if(row){
          row.querySelector(".t-start").value=fmtTime(segment.startTime);
          row.querySelector(".t-end").value=fmtTime(segment.endTime);
        }
      });
      $("btn-zoom-in").onclick=()=>peaksInstance.zoom.zoomIn();
      $("btn-zoom-out").onclick=()=>peaksInstance.zoom.zoomOut();
    }
  });
}

let segSeq=1000;
function addEditRow(t){
  const rows=$("edit-rows");
  const row=document.createElement("div");
  row.className="track-row";
  const segId="t"+t.n;
  row.dataset.seg=segId;
  row.innerHTML=`
    <span class="tn"></span>
    <input class="t-title" placeholder="${T("tr_title_ph")}">
    <input class="t-artist" placeholder="${T("tr_artist_ph")}">
    <span class="t-times">
      <input class="t-time t-start" value="${fmtTime(t.start)}">
      <span class="t-sep">→</span>
      <input class="t-time t-end" value="${fmtTime(t.end)}">
    </span>
    <button class="icon-btn icon-only row-play" title="${T("play")}"><span class="material-symbols-outlined">play_arrow</span></button>
    <button class="icon-btn icon-only row-del" title="${T("del")}"><span class="material-symbols-outlined">delete</span></button>`;
  row.querySelector(".t-title").value=t.title||"";
  row.querySelector(".t-artist").value=t.artist||"";
  row.querySelector(".row-play").onclick=()=>{
    const s=parseTime(row.querySelector(".t-start").value);
    if(s==null)return;
    const a=$("ed-audio");a.currentTime=s;a.play();
  };
  row.querySelector(".row-del").onclick=()=>{
    if(peaksInstance)peaksInstance.segments.removeById(segId);
    row.remove();renumber(rows);
  };
  const sync=()=>{
    const s=parseTime(row.querySelector(".t-start").value);
    const e=parseTime(row.querySelector(".t-end").value);
    if(peaksInstance&&s!=null&&e!=null&&e>s){
      const seg=peaksInstance.segments.getSegment(segId);
      if(seg)seg.update({startTime:s,endTime:e});
    }
  };
  row.querySelector(".t-start").addEventListener("change",sync);
  row.querySelector(".t-end").addEventListener("change",sync);
  const updateLabel=()=>{
    if(!peaksInstance)return;
    const seg=peaksInstance.segments.getSegment(segId);
    if(seg)seg.update({labelText:row.querySelector(".t-title").value});
  };
  row.querySelector(".t-title").addEventListener("change",updateLabel);
  rows.appendChild(row);
  renumber(rows);
}

$("btn-add-track2").onclick=()=>{
  const a=$("ed-audio");
  const start=a.currentTime||0;
  const end=Math.min(start+60,a.duration||start+60);
  const n=++segSeq;
  if(peaksInstance)peaksInstance.segments.add({
    id:"t"+n,startTime:start,endTime:end,
    labelText:T("tr_title_ph"),editable:true,
    color:SEG_COLORS[$("edit-rows").children.length%2]});
  addEditRow({n,title:"",start,end});
};

// ============================================================
// Validation → rendu
// ============================================================
$("btn-render").onclick=async()=>{
  const rows=[...$("edit-rows").querySelectorAll(".track-row")];
  if(!rows.length){alert(T("err_no_tracks"));return;}
  const tracks=[];
  for(const r of rows){
    const title=r.querySelector(".t-title").value.trim();
    const s=parseTime(r.querySelector(".t-start").value);
    const e=parseTime(r.querySelector(".t-end").value);
    if(!title){alert(T("err_titles"));return;}
    if(s==null||e==null||e<=s){alert(`${T("err_times")} — ${title}`);return;}
    const t={n:tracks.length+1,title,start:s,end:e};
    const a=r.querySelector(".t-artist").value.trim();
    if(a)t.artist=a;
    tracks.push(t);
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
  runProgress("render_title",RENDER_STAGES,finish);
}

async function finish(){
  const m=await(await fetch(`/api/jobs/${slug}/manifest`)).json();
  $("summary").innerHTML=
    `<p><strong>${m.album.artist}</strong> — ${m.album.title}<br>`+
    `${m.tracks.length} ${T("tracks_word")}</p>`;
  $("btn-album").href=`/album/${slug}`;
  $("btn-download").href=`/api/jobs/${slug}/bundle`;
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
