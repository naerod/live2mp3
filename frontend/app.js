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
          en:"This feature is restricted to application managers."},
    wip:{fr:"Cette fonctionnalité est encore en cours de développement…",
         en:"This feature is still under development…"}
  };
  if(me.is_admin){
    // admin : accès complet pour tests
  }else{
    document.body.classList.add("app-locked");
    overlay.classList.remove("hidden");
    if(!me.authenticated){
      msg.textContent=msgs.guest[lang()];
      btn.classList.remove("hidden");
    }else if(!me.is_gestionnaire){
      msg.textContent=msgs.user[lang()];
    }else{
      msg.textContent=msgs.wip[lang()];
    }
  }
})();

// --- Thème (localStorage) ---
const themeBtn=document.getElementById("theme");
function setTheme(t){document.documentElement.dataset.theme=t;
  localStorage.setItem("l2m-theme",t);
  themeBtn.querySelector(".material-symbols-outlined").textContent=
    t==="dark"?"dark_mode":"light_mode";}
setTheme(localStorage.getItem("l2m-theme")||"dark");
themeBtn.onclick=()=>setTheme(document.documentElement.dataset.theme==="dark"?"light":"dark");

// --- Langue (localStorage) ---
const langBtn=document.getElementById("lang");
function setLang(l){localStorage.setItem("l2m-lang",l);
  document.getElementById("lang-label").textContent=l.toUpperCase();applyI18n(l);}
setLang(localStorage.getItem("l2m-lang")||"fr");
langBtn.onclick=()=>setLang((localStorage.getItem("l2m-lang")||"fr")==="fr"?"en":"fr");

const $=id=>document.getElementById(id);
const show=id=>{["step-form","step-progress","step-editor","step-done"]
  .forEach(s=>$(s).classList.toggle("hidden",s!==id));};

let slug=null;
const STAGES=["ai_markers","render","tags","artwork","disc","bundle"];

$("btn-create").onclick=async()=>{
  const setlist=$("f-setlist").value.split("\n").map(s=>s.trim()).filter(Boolean)
    .map((title,i)=>({n:i+1,title,parts:title.includes("/")?
      title.split("/").map(p=>p.trim()):undefined}));
  const body={album:{artist:$("f-artist").value,title:$("f-title").value,
    date:$("f-date").value,venue:$("f-venue").value,festival:$("f-festival").value},
    tracks:setlist,target:$("f-target").value,source_url:$("f-url").value};
  const r=await fetch("/api/jobs",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify(body)});
  slug=(await r.json()).slug;
  show("step-editor"); initPeaks();
};

// --- Éditeur Peaks.js ---
let peaksInstance=null;
async function initPeaks(){
  const m=await (await fetch(`/api/jobs/${slug}/manifest`)).json();
  const container={overview:$("overview"),zoomview:$("zoom")};
  // La waveform.dat et l'audio sont servis par le backend une fois le download fait.
  try{
    peaksInstance=await Peaks.init({
      containers:container,
      mediaElement:Object.assign(document.createElement("audio"),
        {src:`/api/jobs/${slug}/audio`}),
      dataUri:{arraybuffer:`/api/jobs/${slug}/waveform.dat`},
    });
    m.tracks.forEach((t,i)=>{
      if(t.start==null)return;
      peaksInstance.segments.add({startTime:t.start,endTime:t.end,
        labelText:t.title,editable:true,id:"t"+t.n});
    });
  }catch(e){console.warn("Peaks indisponible (waveform non générée):",e);}
}

async function validateAndRender(){
  const tracks={};
  if(peaksInstance){peaksInstance.segments.getSegments().forEach(s=>{
    const n=s.id.replace("t","");tracks[n]={start:s.startTime,end:s.endTime};});}
  await fetch(`/api/jobs/${slug}/markers`,{method:"PUT",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({tracks,lock:true})});
  const media=(await(await fetch(`/api/jobs/${slug}/manifest`)).json())
    .target==="data_disc"?"audio":"audio";
  await fetch(`/api/jobs/${slug}/render?media=${media}`,{method:"POST"});
  startProgress();
}
$("btn-express").onclick=validateAndRender;
$("btn-precision").onclick=()=>$("zoom").scrollIntoView({behavior:"smooth"});

// --- Progression SSE ---
function startProgress(){
  show("step-progress");
  const ul=$("stages");ul.innerHTML="";
  const items={};
  STAGES.forEach(s=>{const li=document.createElement("li");
    li.innerHTML=`<span class="dot"></span><span>${s}</span>`;
    ul.appendChild(li);items[s]=li;});
  const es=new EventSource(`/api/jobs/${slug}/events`);
  es.onmessage=e=>{const ev=JSON.parse(e.data);
    const li=items[ev.stage];
    if(li){li.classList.toggle("running",ev.status==="running");
      li.classList.toggle("done",ev.status==="done");}
    if(ev.status==="complete"){es.close();finish();}
    if(ev.status==="error"){es.close();alert("Erreur: "+(ev.info.message||""));}};
}
async function finish(){
  const m=await(await fetch(`/api/jobs/${slug}/manifest`)).json();
  $("summary").innerHTML=`<p>${m.tracks.length} pistes · ${m.album.artist} — ${m.album.title}</p>`;
  $("btn-download").href=`/api/jobs/${slug}/bundle`;
  show("step-done");
}
