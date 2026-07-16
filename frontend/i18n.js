const I18N = {
  fr:{app_title:"Créateur de concerts — découpe automatique et mastering IA",
    login:"Se connecter",
    form_title:"Nouveau concert",artist:"Artiste",album:"Album",date:"Date",
    venue:"Lieu",festival:"Tournée/festival",target:"Type de disque",
    data_disc:"Disque de données (ISO)",audio_cd:"CD audio (CUE+WAV)",
    source:"Lien source",setlist:"Setlist (un titre par ligne)",
    dvd_note:"DVD-Video est hors périmètre V1.",create:"Créer le projet",
    progress_title:"Traitement",editor_title:"Ajustement des coupes",
    express:"Valider et rendre",precision:"Mode précision",
    back:"Retour",done_title:"Terminé",download:"Télécharger le bundle"},
  en:{app_title:"Concert creator — automatic splitting and AI mastering",
    login:"Log in",
    form_title:"New concert",artist:"Artist",album:"Album",date:"Date",
    venue:"Venue",festival:"Tournée/festival",target:"Disc type",
    data_disc:"Data disc (ISO)",audio_cd:"Audio CD (CUE+WAV)",
    source:"Source link",setlist:"Setlist (one title per line)",
    dvd_note:"DVD-Video is out of scope for V1.",create:"Create project",
    progress_title:"Processing",editor_title:"Adjust cuts",
    express:"Validate and render",precision:"Precision mode",
    back:"Back",done_title:"Done",download:"Download bundle"}
};
function applyI18n(lang){
  document.documentElement.lang=lang;
  document.querySelectorAll("[data-i18n]").forEach(el=>{
    const k=el.getAttribute("data-i18n");
    const t=I18N[lang][k];
    if(!t)return;
    // remplace uniquement le premier noeud texte pour préserver les inputs
    const node=[...el.childNodes].find(n=>n.nodeType===3&&n.textContent.trim());
    if(node)node.textContent=t; else if(!el.children.length)el.textContent=t;
    else el.firstChild&&(el.firstChild.textContent=t);
  });
}
