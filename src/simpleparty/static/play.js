const video=document.getElementById("video");
const nextUrl=SP.next;
const prevUrl=SP.prev;
const browseUrl=SP.browse;
const overlay=document.getElementById("video-overlay");
const speedSel=document.getElementById("speed-select");
const speeds=[0.5,0.75,1,1.25,1.5,2,3];
let overlayTimer;
function flash(txt){overlay.textContent=txt;overlay.style.opacity="1";clearTimeout(overlayTimer);overlayTimer=setTimeout(()=>{overlay.style.opacity="0"},600)}
function skip(s){video.currentTime=Math.max(0,Math.min(video.duration||0,video.currentTime+s));flash((s>0?"+":"")+s+"s")}
function setSpeed(v){v=parseFloat(v);video.playbackRate=v;speedSel.value=v;flash(v+"x")}
function cycleSpeed(dir){const i=speeds.indexOf(video.playbackRate);const ni=Math.max(0,Math.min(speeds.length-1,i+dir));setSpeed(speeds[ni])}
let autoplay=localStorage.getItem("sp-autoplay")!=="false";
let repeat=localStorage.getItem("sp-repeat")||"off";
const btnAuto=document.getElementById("btn-autoplay");
const btnRepeat=document.getElementById("btn-repeat");
function updateAutoBtn(){btnAuto.classList.toggle("active",autoplay);btnAuto.textContent=autoplay?"Autoplay: On":"Autoplay: Off";btnAuto.setAttribute("aria-pressed",autoplay?"true":"false")}
function updateRepeatBtn(){var on=repeat!=="off";btnRepeat.classList.toggle("active",on);btnRepeat.textContent=repeat==="one"?"Repeat: One":repeat==="all"?"Repeat: All":"Repeat: Off";btnRepeat.setAttribute("aria-pressed",on?"true":"false")}
btnAuto.addEventListener("click",()=>{autoplay=!autoplay;localStorage.setItem("sp-autoplay",autoplay);updateAutoBtn();flash(autoplay?"Autoplay on":"Autoplay off")});
btnRepeat.addEventListener("click",()=>{repeat=repeat==="off"?"all":repeat==="all"?"one":"off";localStorage.setItem("sp-repeat",repeat);updateRepeatBtn();flash(repeat==="off"?"Repeat off":repeat==="one"?"Repeat one":"Repeat all")});
updateAutoBtn();updateRepeatBtn();
const btnStar=document.getElementById("btn-star");
if(btnStar){btnStar.addEventListener("click",async()=>{
  const cur=btnStar.dataset.starred==="1";const next=!cur;
  const fd=new FormData();fd.set("dir",btnStar.dataset.dir);fd.set("name",btnStar.dataset.video);fd.set("starred",next?"1":"0");
  btnStar.disabled=true;
  try{const r=await fetch("/star-update",{method:"POST",body:new URLSearchParams(fd)});
    if(!r.ok)throw new Error("HTTP "+r.status);
    btnStar.dataset.starred=next?"1":"0";
    btnStar.classList.toggle("active",next);
    btnStar.setAttribute("aria-pressed",next?"true":"false");
    flash(next?"Starred":"Unstarred");
  }catch(e){flash("Star failed")}
  finally{btnStar.disabled=false}
})}
video.addEventListener("ended",()=>{if(repeat==="one"){video.currentTime=0;video.play()}else if(repeat==="all"||autoplay){window.location.href=nextUrl}});
video.play().catch(()=>{});
(function(){var x0=0,y0=0,t0=0,lastTap=0,lastSide=0;
video.addEventListener("touchstart",function(e){if(e.touches.length!==1){t0=0;return}var t=e.touches[0];var r=video.getBoundingClientRect();if(t.clientY>r.bottom-48){t0=0;return}x0=t.clientX;y0=t.clientY;t0=Date.now()},{passive:true});
video.addEventListener("touchend",function(e){if(!t0)return;var t=e.changedTouches[0];var dx=t.clientX-x0,dy=t.clientY-y0,dt=Date.now()-t0;t0=0;
  if(dt<700&&Math.abs(dx)>60&&Math.abs(dx)>Math.abs(dy)*1.7){window.location.href=dx<0?nextUrl:prevUrl;return}
  if(dt<350&&Math.abs(dx)<24&&Math.abs(dy)<24){var r2=video.getBoundingClientRect();var side=t.clientX>r2.left+r2.width/2?1:-1;var now=Date.now();
    if(now-lastTap<320&&side===lastSide){skip(side*10);lastTap=0}else{lastTap=now;lastSide=side}}},{passive:true});
})();
document.addEventListener("keydown",e=>{
  var tn=e.target.tagName;
  if(tn==="INPUT"||tn==="SELECT"||tn==="TEXTAREA"||e.target.isContentEditable)return;
  if(e.target.closest&&e.target.closest("a,button,summary,[tabindex]"))return;
  if(e.ctrlKey||e.metaKey||e.altKey)return;
  switch(e.key){
    case"n":case"ArrowRight":window.location.href=nextUrl;break;
    case"p":case"ArrowLeft":window.location.href=prevUrl;break;
    case" ":e.preventDefault();video.paused?video.play():video.pause();break;
    case"f":e.preventDefault();document.fullscreenElement?document.exitFullscreen():video.requestFullscreen();break;
    case"m":video.muted=!video.muted;break;
    case"j":e.preventDefault();skip(-10);break;
    case"l":e.preventDefault();skip(10);break;
    case"J":e.preventDefault();skip(-30);break;
    case"L":e.preventDefault();skip(30);break;
    case"<":e.preventDefault();cycleSpeed(-1);break;
    case">":e.preventDefault();cycleSpeed(1);break;
    case"Escape":window.location.href=browseUrl;break;
    case"d":document.querySelector("#delete-form button")?.click();break;
    case"a":btnAuto.click();break;
    case"r":btnRepeat.click();break;
  }
});
