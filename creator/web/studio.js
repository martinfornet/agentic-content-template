"use strict";
const $ = s => document.querySelector(s);
const html = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const labels = ["Brief","Tournage","Voix & musique","Montage","Export"];
const stages = {proposed:1,footage_approved:2,voice_generated:2,voice_approved:3,assembled:4,export_approved:4};
let rows = [], project, takes = [], urls = [], busy = false;
const panel = (title, content) => '<section class="panel"><h2>'+title+'</h2>'+content+'</section>';
const button = (label, action) => '<button class="primary" data-action="'+action+'">'+label+'</button>';
const media = kind => "/media/"+project.id+"/"+kind;
function message(text, error=false) {
  $("#status").textContent = text;
  $("#status").className = error ? "error" : "";
}
function clearUrls() { urls.forEach(u=>URL.revokeObjectURL(u)); urls=[]; }
function select(id) {
  if (busy) return;
  project = rows.find(x=>x.id === id);
  location.hash = project ? project.id : "new";
  clearUrls(); takes=[]; message(""); paint();
}
function shots() {
  clearUrls();
  $("#takes").innerHTML = takes.map((file,i)=>{
    const u=URL.createObjectURL(file); urls.push(u);
    return '<figure><video controls preload="metadata" src="'+u+'"></video><figcaption>Plan '+(i+1)+' · '+html(file.name)+'</figcaption>'+
      '<button data-move="'+i+'" aria-label="Avancer le plan '+(i+1)+'" '+(i===0?'disabled':'')+'>↑</button>'+
      '<button data-remove="'+i+'">Retirer</button></figure>';
  }).join("");
}
function paint() {
  $("#projects").innerHTML=rows.map(p=>'<button data-id="'+html(p.id)+'" class="'+(p.id===project?.id?'active':'')+'">'+html(p.proposal.idea)+'</button>').join("");
  $("#title").textContent=project?.proposal.idea || "Nouveau projet";
  const space=$("#workspace");
  if (!project) {
    space.innerHTML=panel("Préparer le tournage",'<label>Direction créative<textarea id="direction" placeholder="Un geste de cuisine à filmer chez soi"></textarea></label><label>Tendance à explorer (facultatif)<input id="trend"></label>'+button("Générer le brief","idea"));
    return;
  }
  const p=project, step=stages[p.stage], brief='<h3>Plans à filmer</h3><p>'+html(p.proposal.shooting_brief)+'</p><h3>Narration</h3><p>'+html(p.proposal.script)+'</p>';
  let out='<ol class="steps">'+labels.map((name,i)=>'<li class="'+(i===step?'current':'')+'">'+html(name)+'</li>').join("")+'</ol>';
  out+=p.stage==="proposed"?panel("Brief de tournage",brief):'<details class="panel"><summary>Consulter le brief et les prises</summary>'+brief+'<p>'+p.clips.map((name,i)=>html((i+1)+". "+name)).join("<br>")+'</p></details>';
  if (p.stage==="proposed") out+=panel("Valider les prises",'<label>Ajouter des vidéos<input type="file" id="clips" accept="video/*" multiple></label><div id="takes"></div>'+button("J’ai visionné et je valide ces prises","footage"));
  if (p.stage==="footage_approved") out+=panel("Générer la narration",'<p>'+html(p.proposal.script)+'</p><label>Voix de référence personnelle (facultatif)<input id="reference" type="file" accept="audio/*"></label>'+button("Générer la voix Chatterbox","voice"));
  if (p.stage==="voice_generated") {
    const options=[["voice","Voix seule"],...p.choices.map(k=>[k,"Mix "+k+" · "+(k==="A"?"90":"120")+" BPM"])];
    out+=panel("Écouter la narration",options.map(([key,name])=>'<figure><figcaption>'+name+'</figcaption><audio controls preload="metadata" src="'+media(key)+'"></audio></figure>').join("")+
      (!p.choices.length?button("Préparer les deux mix","mixes"):"")+
      '<label>Bande-son à monter<select id="music">'+options.map(([key,name])=>'<option value="'+(key==="voice"?"":key)+'">'+name+'</option>').join("")+'</select></label>'+button("J’ai écouté et je valide la bande-son","listen-ok"));
  }
  if (p.stage==="voice_approved") out+=panel("Monter le Reel",'<p>Assembler les prises validées avec la bande-son et les sous-titres synchronisés.</p>'+button("Lancer le montage","render"));
  if (p.stage==="assembled" || p.stage==="export_approved") out+=panel(p.stage==="assembled"?"Revoir le montage":"Export validé",'<figure><video controls preload="metadata" src="'+media("video")+'"></video></figure>'+(p.stage==="assembled"?button("J’ai regardé et je valide le MP4","export-ok"):'<a class="download" download href="'+media("video")+'?download=true">Télécharger le MP4</a>'));
  if (!["proposed","export_approved"].includes(p.stage)) out+='<details><summary>Reprendre le tournage</summary><p>Les prises et la bande-son devront être validées à nouveau.</p><button data-action="retake">Revenir aux prises</button></details>';
  if (p.stage!=="export_approved") out+='<label>Nom pour les validations<input id="actor" autocomplete="given-name" value="'+html(localStorage.getItem("creator-name")||"")+'"></label>';
  space.innerHTML=out;
}
async function load() {
  const res=await fetch("/api/projects");
  if (!res.ok) throw Error("Impossible de charger les projets.");
  rows=await res.json();
}
const waiting={idea:"Le modèle prépare le brief…",voice:"Chatterbox génère la narration…",mixes:"Création des deux préécoutes…",render:"Alignement des sous-titres et montage du MP4…"};
async function action(key) {
  if (busy) return;
  const data=new FormData(), actor=$("#actor")?.value.trim();
  if (key==="idea") {
    const direction=$("#direction").value.trim() || "Un geste simple du quotidien";
    data.set("direction",direction); data.set("trend",$("#trend").value.trim());
  } else {
    data.set("version",project.updated_at);
    if (["footage","listen-ok","export-ok","retake"].includes(key) && !actor) {
      message("Indique ton nom pour enregistrer ta validation.",true); $("#actor").focus(); return;
    }
    if (actor) { data.set("actor",actor); localStorage.setItem("creator-name",actor); }
    if (key==="footage") {
      if (!takes.length) {message("Ajoute au moins une prise.",true); return;}
      takes.forEach(f=>data.append("clips",f));
    }
    if (key==="voice" && $("#reference").files.length) data.set("reference",$("#reference").files[0]);
    if (key==="listen-ok") data.set("music",$("#music").value);
  }
  busy=true; $("#workspace").inert=true; $("aside").inert=true; message(waiting[key]||"Enregistrement…");
  try {
    const url=key==="idea"?"/api/projects":"/api/projects/"+project.id+"/"+key;
    const res=await fetch(url,{method:"POST",headers:{"X-Creator-Studio":"1"},body:data});
    const result=await res.json();
    if (!res.ok) throw Error(typeof result.detail==="string"?result.detail:"Cette étape a échoué. Vérifie les fichiers et réessaie.");
    await load(); project=rows.find(p=>p.id===result.id); location.hash=result.id;
    clearUrls(); takes=[]; paint(); message("");
  } catch(e) {message(e.message,true);}
  finally {busy=false; $("#workspace").inert=false; $("aside").inert=false;}
}
document.addEventListener("click",e=>{
  const btn=e.target.closest("button"); if(!btn || busy) return;
  if(btn.id==="new") select("");
  else if(btn.dataset.id) select(btn.dataset.id);
  else if(btn.dataset.action) action(btn.dataset.action);
  else if(btn.hasAttribute("data-remove")) {takes.splice(+btn.dataset.remove,1); shots();}
  else if(btn.hasAttribute("data-move")) {const i=+btn.dataset.move; [takes[i-1],takes[i]]=[takes[i],takes[i-1]]; shots();}
});
document.addEventListener("change",e=>{if(e.target.id==="clips"){takes.push(...e.target.files); e.target.value=""; shots();}});
window.addEventListener("hashchange",()=>{if(!busy && location.hash.slice(1)!==(project?.id||"new"))select(location.hash.slice(1));});
load().then(()=>select(location.hash.slice(1))).catch(e=>message(e.message,true));
