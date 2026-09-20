"""Self-contained single-file panel (no external JS/CSS — platform-proxy-proof).

Served at /panel and as the SPA fallback for browser routes. Everything
(login, dashboard, wizard, instance pages, admin) is one HTML document with
inline CSS + JS talking to the existing JSON API.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter(include_in_schema=False)

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="dark">
<title>EMUNEL Console</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Cpath d='M16 2.6 27.8 9.4v13.2L16 29.4 4.2 22.6V9.4Z' fill='none' stroke='%23e2ddf6' stroke-width='2' stroke-linejoin='round'/%3E%3Cpath d='M12.4 10.6v10.8M12.4 10.6h8.2M12.4 16h5.4M12.4 21.4h8.2' stroke='%23e2ddf6' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E">
<style>
:root{--bg:#0a0c10;--bg2:#10131a;--sur:#12151c;--sur2:#171b24;--bd:#1e2430;--bd2:#2a3242;
--tx:#e7ebf3;--dim:#9aa4b8;--fnt:#5d6678;--acc:#e2ddf6;--accd:#0b0d11;--blu:#8d9bff;
--grn:#4ecb95;--amb:#e3b341;--red:#ef6b73;--mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;
--sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",sans-serif;--r:10px;--rs:7px}
*{box-sizing:border-box}html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--tx);font:14px/1.5 var(--sans);-webkit-font-smoothing:antialiased}
::selection{background:rgba(141,155,255,.25);color:var(--tx)}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-thumb{background:#232a38;border-radius:6px;border:2px solid var(--bg)}
::-webkit-scrollbar-track{background:transparent}
a{color:var(--blu);text-decoration:none}
.shell{display:grid;grid-template-columns:220px 1fr;min-height:100vh}
.sb{border-right:1px solid var(--bd);background:var(--bg2);padding:20px 12px;display:flex;flex-direction:column;gap:2px;position:sticky;top:0;height:100vh}
.brand{display:flex;align-items:center;gap:9px;padding:2px 8px 16px}
.bm{width:24px;height:24px;color:var(--acc);flex:none}
.bn{font-weight:650;font-size:15px;letter-spacing:.4px}
.ni{display:flex;align-items:center;gap:9px;padding:8px 10px;border-radius:var(--rs);color:var(--dim);font-weight:500;cursor:pointer;border:1px solid transparent;background:none;width:100%;text-align:left;font-size:13px;font-family:inherit}
.ni:hover{color:var(--tx);background:var(--sur)}
.ni.act{color:var(--tx);background:var(--sur2);border-color:var(--bd)}
.ni svg{width:15px;height:15px}
.sbft{margin-top:auto;padding-top:10px;border-top:1px solid var(--bd);display:flex;align-items:center;gap:8px}
.sbft .who{min-width:0}.sbft .who b{display:block;font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sbft .who span{font-size:11px;color:var(--fnt)}
.main{min-width:0}.ct{padding:26px 30px 70px;max-width:1100px;margin:0 auto}
.topbar{display:none}
.menu-btn{display:none}
.scrim{position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:55;display:none}
.scrim.on{display:block}
/* bottom nav is mobile-only: hidden by default (BEFORE the media query, so
   the cascade is correct) and shown inside it. Previously the overriding
   .bnav{display:none} sat AFTER the media block and killed the mobile nav
   entirely, leaving only the hamburger drawer. */
.bnav{display:none}
@media(max-width:840px){
  .shell{grid-template-columns:1fr}
  .menu-btn{display:inline-flex;margin-left:auto}
  .sb{display:flex;position:fixed;top:0;left:0;bottom:0;width:250px;z-index:60;
      transform:translateX(-105%);transition:transform .22s ease;box-shadow:none}
  .sb.open{transform:translateX(0);box-shadow:0 0 44px rgba(0,0,0,.55)}
.topbar{display:flex;align-items:center;gap:10px;position:sticky;top:0;z-index:30;background:rgba(10,12,16,.94);border-bottom:1px solid var(--bd);padding:12px 14px}
.ct{padding:16px 12px 90px}
.bnav{display:flex;position:fixed;bottom:0;left:0;right:0;z-index:30;background:rgba(13,16,22,.97);border-top:1px solid var(--bd);padding:6px 6px calc(6px + env(safe-area-inset-bottom))}
.bnav .ni{flex:1;flex-direction:column;gap:2px;font-size:10px;align-items:center;padding:6px 0;min-width:0;white-space:nowrap}
.bnav .ni svg{width:17px;height:17px}}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:7px;padding:8px 13px;border-radius:var(--rs);border:1px solid var(--bd2);background:var(--sur2);color:var(--tx);font:600 13px var(--sans);cursor:pointer;white-space:nowrap;transition:background .12s ease,border-color .12s ease,box-shadow .12s ease,transform .06s ease;box-shadow:inset 0 1px 0 rgba(255,255,255,.035)}
.btn:hover{background:#1c212c;border-color:#37415a}
.btn:active{transform:translateY(1px)}
.btn:focus-visible{outline:2px solid var(--blu);outline-offset:2px}
.btn:disabled{opacity:.45;cursor:not-allowed}
.btn.pri{background:var(--acc);border-color:var(--acc);color:var(--accd);box-shadow:0 2px 14px rgba(226,221,246,.10),inset 0 1px 0 rgba(255,255,255,.14)}
.btn.pri:hover{background:#edeafb;box-shadow:0 3px 18px rgba(226,221,246,.18)}
.btn.dng{color:var(--red);border-color:rgba(239,107,115,.35)}.btn.dng:hover{background:rgba(239,107,115,.09)}
.btn.sm{padding:5px 9px;font-size:12px}
.inp{width:100%;padding:9px 12px;background:var(--bg2);color:var(--tx);border:1px solid var(--bd2);border-radius:var(--rs);font:400 13.5px var(--sans);transition:border-color .12s ease,box-shadow .12s ease}
.inp:hover{border-color:#37415a}
.inp::placeholder{color:#4d5566}
.inp:focus{outline:none;border-color:var(--blu);box-shadow:0 0 0 3px rgba(141,155,255,.15)}
.inp:focus-visible{outline:none}
.fld{margin-bottom:14px}.fld label{display:block;font-size:12px;font-weight:600;color:var(--dim);margin-bottom:5px}
.card{background:var(--sur);border:1px solid var(--bd);border-radius:var(--r);padding:16px;box-shadow:inset 0 1px 0 rgba(255,255,255,.02)}
.card h3{margin:0 0 6px;font-size:13.5px}
.sgs{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:22px}
@media(max-width:840px){.sgs{grid-template-columns:repeat(2,1fr)}}
.sg{background:var(--sur);border:1px solid var(--bd);border-radius:var(--r);padding:12px 14px;box-shadow:inset 0 1px 0 rgba(255,255,255,.02)}
.sg .l{font-size:11px;text-transform:uppercase;letter-spacing:1px;color:var(--fnt)}
.sg .v{font:650 24px var(--mono);margin-top:2px}
.st{display:inline-flex;align-items:center;gap:6px;font-size:12px;font-weight:600}
.st .d{width:8px;height:8px;border-radius:50%;background:var(--fnt)}
.st.run .d{background:var(--grn);animation:pu 2.2s infinite}
.st.fail .d{background:var(--red)}.st.sto .d{background:var(--fnt)}.st.tr .d{background:var(--amb)}
@keyframes pu{0%{box-shadow:0 0 0 0 rgba(78,203,149,.45)}70%{box-shadow:0 0 0 6px rgba(78,203,149,0)}100%{box-shadow:0 0 0 0 rgba(78,203,149,0)}}
.ig{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:11px}
.ic{background:var(--sur);border:1px solid var(--bd);border-radius:var(--r);padding:14px;cursor:pointer;display:flex;flex-direction:column;gap:9px;transition:border-color .12s}
.ic:hover{border-color:var(--bd2);transform:translateY(-1px);box-shadow:0 6px 18px rgba(0,0,0,.28)}
.ic{transition:border-color .12s,transform .08s,box-shadow .12s}
.ic .t{display:flex;align-items:center;justify-content:space-between;gap:8px}
.ic .nm{font-size:14.5px;font-weight:650}
.ic .ep{font-family:var(--mono);font-size:11px;color:var(--dim);background:var(--bg2);border:1px solid var(--bd);padding:5px 7px;border-radius:var(--rs);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:100%}
.ic .mt{display:flex;gap:12px;color:var(--fnt);font-size:11.5px;flex-wrap:wrap}
.ph{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:20px;flex-wrap:wrap}
.ph h1{margin:0;font-size:20px}.ph .sub{color:var(--dim);margin-top:3px;font-size:13px}
.ha{display:flex;gap:7px;flex-wrap:wrap}
.tabs{display:flex;gap:2px;border-bottom:1px solid var(--bd);margin-bottom:18px;overflow-x:auto;scrollbar-width:none}
.tabs::-webkit-scrollbar{display:none}
.tab{padding:8px 12px;font-size:12.5px;font-weight:600;color:var(--fnt);border:none;background:none;cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-1px;white-space:nowrap;font-family:inherit;border-radius:6px 6px 0 0}
.tab:hover{color:var(--dim)}
.tab:focus-visible{outline:2px solid var(--blu);outline-offset:-2px}
.tab.act{color:var(--tx);border-bottom-color:var(--acc)}
.tbl{width:100%;border-collapse:collapse;font-size:12.5px}
.tbl th{text-align:left;font-size:10.5px;text-transform:uppercase;letter-spacing:1px;color:var(--fnt);padding:7px 10px;border-bottom:1px solid var(--bd)}
.tbl td{padding:9px 10px;border-bottom:1px solid var(--bd)}
.tbl tbody tr:hover td{background:rgba(255,255,255,.015)}
.tbl tr:last-child td{border-bottom:none}
.term{background:#07090c;border:1px solid var(--bd);border-radius:var(--r);font-family:var(--mono);font-size:11.5px;overflow:hidden}
.tb{display:flex;gap:7px;align-items:center;padding:7px 9px;border-bottom:1px solid var(--bd);background:var(--sur);flex-wrap:wrap}
.tb .sp{flex:1}
.tbody{height:400px;overflow:auto;padding:9px 11px}
.ll{white-space:pre-wrap;word-break:break-all}.ll .t{color:var(--fnt)}.ll .lv{font-weight:700}
.ll.info .lv{color:var(--blu)}.ll.warning .lv,.ll.warn .lv{color:var(--amb)}.ll.error .lv{color:var(--red)}.ll.ok .lv{color:var(--grn)}
.te{color:var(--fnt);padding:26px;text-align:center}
.empty{text-align:center;padding:44px 16px;color:var(--dim);border:1px dashed var(--bd2);border-radius:var(--r)}
.empty b{display:block;color:var(--tx);margin-bottom:3px}
.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px}
.kv .it{background:var(--bg2);border:1px solid var(--bd);border-radius:var(--rs);padding:9px 11px}
.kv .k{font-size:10.5px;text-transform:uppercase;letter-spacing:1px;color:var(--fnt)}
.kv .v{font-family:var(--mono);font-size:13.5px;margin-top:2px}
.optg{display:grid;grid-template-columns:1fr 1fr;gap:9px}
@media(max-width:640px){.optg{grid-template-columns:1fr}}
.opt{border:1px solid var(--bd2);border-radius:var(--rs);padding:11px 13px;cursor:pointer;background:var(--bg2);transition:border-color .12s,background .12s}
.opt:hover{border-color:var(--blu)}
.opt.sel{border-color:var(--acc);background:var(--sur2)}
.opt .t{font-weight:650;font-size:13px}.opt .d{font-size:11.5px;color:var(--fnt);margin-top:2px}
.mono{font-family:var(--mono);font-size:12px}
.mut{color:var(--dim)}.ftx{color:var(--fnt)}
.row{display:flex;align-items:center;gap:9px;flex-wrap:wrap}.grow{flex:1}
.chip{display:inline-flex;padding:1px 7px;border:1px solid var(--bd2);border-radius:999px;font-size:11px;color:var(--dim);font-family:var(--mono);background:rgba(255,255,255,.02)}
.tw{position:fixed;bottom:18px;right:18px;z-index:100;display:flex;flex-direction:column;gap:7px}
.to{background:var(--sur2);border:1px solid var(--bd2);border-left:3px solid var(--blu);border-radius:var(--rs);padding:9px 13px;min-width:220px;max-width:340px;font-size:12.5px;box-shadow:0 8px 24px rgba(0,0,0,.35);animation:tin .18s ease}
@keyframes tin{from{transform:translateY(8px);opacity:0}to{transform:none;opacity:1}}
.to.ok{border-left-color:var(--grn)}.to.err{border-left-color:var(--red)}
.sp1{width:15px;height:15px;border:2px solid var(--bd2);border-top-color:var(--acc);border-radius:50%;animation:sp .7s linear infinite;display:inline-block}
@keyframes sp{to{transform:rotate(360deg)}}
.lc{width:378px;max-width:100%;text-align:center}
.lc h2{margin:8px 0 0;font-size:21px}
.lc .p{color:var(--dim);font-size:13px;margin:8px 0 20px}
.lc .card{padding:24px 22px;text-align:left}
.fn{color:var(--fnt);font-size:11px;margin-top:14px}
.copy{border:none;background:none;color:var(--fnt);cursor:pointer;font-family:var(--mono);font-size:11px;padding:2px 4px}
.copy:hover{color:var(--tx)}
.qr-ov{position:fixed;inset:0;background:rgba(0,0,0,.72);display:flex;align-items:center;justify-content:center;z-index:200}
.qr-c{background:var(--sur);border:1px solid var(--bd2);border-radius:12px;padding:20px;text-align:center;max-width:340px}
.qr-c .qrbox svg{width:240px;height:240px;display:block;margin:8px auto;background:#fff;border-radius:8px}
.free{display:inline-flex;align-items:center;gap:6px;font-size:10.5px;letter-spacing:1px;font-weight:700;color:var(--grn);border:1px solid rgba(78,203,149,.4);border-radius:999px;padding:2px 9px;text-transform:uppercase}
.fdot{width:6px;height:6px;border-radius:50%;background:var(--grn);box-shadow:0 0 0 3px rgba(78,203,149,.15);flex:none}
.vmeter{height:9px;background:var(--bg2);border:1px solid var(--bd);border-radius:999px;overflow:hidden}
.vmeter>div{height:100%;background:var(--blu);border-radius:999px;transition:width .4s ease}
.vmeter.warn>div{background:var(--amb)}
.vmeter.crit>div{background:var(--red)}
.qch{display:inline-flex;align-items:center;padding:4px 11px;border:1px solid var(--bd2);border-radius:999px;background:var(--bg2);color:var(--dim);font:600 11.5px var(--sans);cursor:pointer;transition:border-color .12s,color .12s}
.qch:hover{border-color:var(--blu);color:var(--tx)}
.qch.on{border-color:var(--acc);color:var(--tx);background:var(--sur2)}
.lw{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:18px;background:radial-gradient(1100px 520px at 50% -12%,rgba(141,155,255,.07),transparent 60%),radial-gradient(720px 420px at 88% 112%,rgba(226,221,246,.05),transparent 55%),var(--bg)}
.lc{width:378px;max-width:100%}
.lhead{text-align:center;margin-bottom:18px}
.lt{width:58px;height:58px;margin:0 auto 13px;border-radius:15px;display:flex;align-items:center;justify-content:center;color:var(--acc);background:var(--sur);border:1px solid var(--bd2);box-shadow:inset 0 1px 0 rgba(255,255,255,.06),0 12px 32px rgba(0,0,0,.38)}
.lt svg{width:30px;height:30px}
.lhead h2{margin:0;font-size:22px;font-weight:700;letter-spacing:3px}
.lsub{font-size:9.5px;letter-spacing:4.5px;color:var(--fnt);margin-top:4px;text-transform:uppercase}
.lhead .p{color:var(--dim);font-size:13px;margin:12px 0 0;line-height:1.55}
.lcard{padding:24px 22px;text-align:left}
.lcard .lgo{width:100%;padding:10px}
.lmeta{margin-top:20px;display:flex;align-items:center;justify-content:center;gap:12px;color:var(--fnt);font-size:10px;letter-spacing:2.5px;text-transform:uppercase}
.dl{flex:1;height:1px;background:linear-gradient(90deg,transparent,var(--bd2),transparent)}
</style>
</head>
<body>
<div id="app"><div class="lw"><span class="sp1"></span></div></div>
<script>
(function(){"use strict";
// ───────────────────────────── helpers ─────────────────────────────
var CSRF="";
function $(s,el){return (el||document).querySelector(s)}
function esc(s){var d=document.createElement("div");d.textContent=s==null?"":String(s);return d.innerHTML}
function toast(msg,kind,ms){var w=$(".tw");if(!w){w=document.createElement("div");w.className="tw";document.body.appendChild(w)}
var e=document.createElement("div");e.className="to "+(kind||"");e.textContent=msg;w.appendChild(e);setTimeout(function(){e.remove()},ms||3500)}
function fmtBytes(n){if(n==null)return"—";if(n<1024)return n+" B";if(n<1048576)return(n/1024).toFixed(1)+" KB";if(n<1073741824)return(n/1048576).toFixed(2)+" MB";return(n/1073741824).toFixed(2)+" GB"}
function fmtUp(s){if(s==null)return"—";var d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);if(d>0)return d+"d "+h+"h";if(h>0)return h+"h "+m+"m";if(m>0)return m+"m "+s+"s";return s+"s"}
function ago(iso){if(!iso)return"—";var t=new Date(iso),df=(Date.now()-t.getTime())/1e3;if(df<60)return"just now";if(df<3600)return Math.floor(df/60)+"m ago";if(df<86400)return Math.floor(df/3600)+"h ago";return t.toLocaleDateString(undefined,{month:"short",day:"numeric"})}
function dur(ms){if(ms==null)return"—";if(ms<1e3)return ms+"ms";if(ms<6e4)return(ms/1e3).toFixed(1)+"s";return Math.floor(ms/6e4)+"m"}
var LBL={queued:"Queued",preparing:"Preparing",building:"Building",starting:"Starting",health_check:"Health check",running:"Running",failed:"Failed",stopping:"Stopping",stopped:"Stopped",deleted:"Deleted",online:"Running",offline:"Stopped",unknown:"—"};
var BUSY={queued:1,preparing:1,building:1,starting:1,health_check:1,stopping:1};
function stEl(s){var cls=s==="running"||s==="online"?"run":(s==="failed"?"fail":(s==="stopped"||s==="offline"||s==="deleted"?"sto":(s==="stopping"?"sto":"tr")));
var sp=document.createElement("span");sp.className="st "+cls;sp.innerHTML='<span class="d"></span>'+(LBL[s]||s);return sp}
function copyBtn(text){var b=document.createElement("button");b.className="copy";b.textContent="copy";
b.onclick=function(e){e.stopPropagation();if(navigator.clipboard){navigator.clipboard.writeText(text).then(function(){toast("Copied","ok",1200)})}else{toast("Copy not supported","err")}};return b}
// ───────────────────────────── api ─────────────────────────────
function api(method,path,body,retry){
  var h={"Content-Type":"application/json"};
  if(CSRF)h["X-EMUNEL-CSRF"]=CSRF;
  return fetch(path,{method:method,headers:h,credentials:"same-origin",body:body!==undefined?JSON.stringify(body):undefined})
  .catch(function(err){noteNetErr();throw err})
  .then(function(r){
    // 429 = too fast; wait what the server asks (or 2s) and retry silently
    if(r.status===429&&(retry||0)<3){
      var wait=parseInt(r.headers.get("Retry-After")||"2",10)||2;
      return new Promise(function(res){setTimeout(res,wait*1e3)}).then(function(){return api(method,path,body,(retry||0)+1)});
    }
    return r.json().catch(function(){return{}}).then(function(d){
    if(!r.ok){
      if(r.status===401&&!(retry)&&!path.startsWith("/auth")){
        // confirm the session really died before bouncing the user
        return fetch("/auth/me",{credentials:"same-origin"}).then(function(m){return m.json()}).then(function(me){
          if(me.authenticated){CSRF=me.csrf_token;return api(method,path,body,3)}
          USER=null;render();throw new Error("please sign in again");
        });
      }
      throw new Error((d&&d.detail)||("HTTP "+r.status));
    }
    return d})})}

// ───────────────────────────── icons ─────────────────────────────
function ic(n){var p={dash:'<path d="M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z"/>',
plus:'<path d="M12 5v14M5 12h14"/>',gear:'<path d="M12 3l8 4v5c0 5-3.5 8-8 9-4.5-1-8-4-8-9V7z"/>',
menu:'<path d="M4 7h16M4 12h16M4 17h16"/>',
eng:'<path d="M3 12h3.5l2.5-7 4 14 2.5-7H21"/>',
byp:'<path d="M12 3l7 3v5c0 4.5-3 7.7-7 9-4-1.3-7-4.5-7-9V6z"/><path d="M13 7l-3.2 4.6h2.4l-2 4.8 4.3-5.8h-2.3z"/>',
vol:'<path d="M12 3v18M8 7v10M16 7v10M20 10v4M4 10v4"/>',
gh:'<path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27s1.36.09 2 .27c1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8Z" fill="currentColor" stroke="none"/>',
tg:'<path d="M21.9 4.6 18.9 19c-.2 1-.8 1.2-1.7.8l-4.6-3.4-2.2 2.1c-.3.3-.5.5-1 .5l.4-4.7L18.6 6c.4-.3-.1-.5-.6-.2L7.3 12.4l-4.3-1.4c-.9-.3-.9-.9.2-1.3L20.7 3.3c.8-.3 1.5.2 1.2 1.3Z" fill="currentColor" stroke="none"/>'};
return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" style="width:15px;height:15px">'+p[n]+"</svg>"}
var MARK_IN='<path d="M16 2.6 27.8 9.4v13.2L16 29.4 4.2 22.6V9.4Z" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/><path d="M12.4 10.6v10.8M12.4 10.6h8.2M12.4 16h5.4M12.4 21.4h8.2" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>';
var MARK='<svg class="bm" viewBox="0 0 32 32" fill="none">'+MARK_IN+"</svg>";
var MARK_L='<svg viewBox="0 0 32 32" fill="none">'+MARK_IN+"</svg>";

// ───────────────────────────── shell/state ─────────────────────────────
var USER=null, cleanup=null, pollTimer=null, LINKS={github:"https://github.com/mehialadi-star/EMUNEL",telegram:""}, BUILD_STAMP="__EMUNEL_BUILD__";
function setCleanup(fn){if(cleanup)cleanup();cleanup=fn||null}
function stopPoll(){if(pollTimer){if(pollTimer.stop)pollTimer.stop();else clearInterval(pollTimer);pollTimer=null}}
// Resilient polling: skips overlapping runs, pauses while the tab is hidden
// and backs off exponentially (x2 up to 60s) while requests keep failing, so
// a struggling server never gets hammered by an open panel page. fn should
// return its promise for the overlap guard to work.
function poll(ms,fn){
  var stop=false,t=null,busy=false,fail=0;
  function run(){
    if(stop)return;
    if(document.hidden){t=setTimeout(run,1000);return}
    if(busy){t=setTimeout(run,600);return}
    busy=true;
    Promise.resolve().then(fn).then(
      function(){fail=0},
      function(){fail++}).then(function(){
        busy=false;
        var wait=fail>0?Math.min(ms*Math.pow(2,fail),60000):ms;
        t=setTimeout(run,wait)});
  }
  run();
  return {stop:function(){stop=true;if(t)clearTimeout(t)}}}
// Visible connection state: network-level fetch failures surface ONE toast
// per 30s instead of silently breaking the page ("random disconnects").
var _netErrAt=0;
function noteNetErr(){
  var n=Date.now();
  if(n-_netErrAt>30000){_netErrAt=n;toast("Connection problem — the panel keeps retrying in the background…","err",5000)}}
function shell(nav){
  stopPoll();setCleanup(null);
  var app=$("#app");
  app.innerHTML='<div class="shell"><aside class="sb">'+
    '<div class="brand">'+MARK+'<div><div class="bn">EMUNEL</div><div style="font-size:10px;color:var(--fnt);letter-spacing:1.2px">CONSOLE</div></div></div>'+
    '<button class="ni '+(nav==="dash"?"act":"")+'" data-nav="dash">'+ic("dash")+' Dashboard</button>'+
    '<button class="ni '+(nav==="new"?"act":"")+'" data-nav="new">'+ic("plus")+' Create Instance</button>'+
    (USER.is_admin?'<button class="ni '+(nav==="admin"?"act":"")+'" data-nav="admin">'+ic("gear")+' Admin</button>'+
      '<button class="ni '+(nav==="engines"?"act":"")+'" data-nav="engines">'+ic("eng")+' Engines</button>'+
      '<button class="ni '+(nav==="bypass"?"act":"")+'" data-nav="bypass">'+ic("byp")+' Bypass</button>':"")+
    (LINKS.github?'<a class="ni" href="'+LINKS.github+'" target="_blank" rel="noopener">'+ic("gh")+' GitHub</a>':"")+
    (LINKS.telegram?'<a class="ni" href="'+esc(LINKS.telegram)+'" target="_blank" rel="noopener">'+ic("tg")+' Telegram</a>':"")+
    '<div style="padding:6px 8px"><span class="free"><span class="fdot"></span>Free</span></div>'+
    '<div style="padding:0 8px 8px"><span class="ftx mono" style="font-size:9.5px;letter-spacing:.4px">build '+BUILD_STAMP+"</span></div>"+
    '<div class="sbft"><div class="who"><b>'+esc(USER.name||USER.login)+'</b><span>@'+esc(USER.login)+'</span></div>'+
    '<button class="btn sm" style="margin-left:auto" id="lg">Sign out</button></div></aside>'+
    '<div class="main"><div class="topbar">'+MARK+'<b style="font-size:14px">EMUNEL</b>'+
    '<button class="btn sm menu-btn" id="mb">'+ic("menu")+"</button>"+
    '<button class="btn sm" style="margin-left:auto" id="lgm">Sign out</button></div>'+
    '<div class="ct" id="view"></div>'+
    '<nav class="bnav"><button class="ni '+(nav==="dash"?"act":"")+'" data-nav="dash">'+ic("dash")+'<span>Home</span></button>'+
    '<button class="ni '+(nav==="new"?"act":"")+'" data-nav="new">'+ic("plus")+'<span>Create</span></button>'+
    (USER.is_admin?'<button class="ni '+(nav==="admin"?"act":"")+'" data-nav="admin">'+ic("gear")+'<span>Admin</span></button>'+
      '<button class="ni '+(nav==="engines"?"act":"")+'" data-nav="engines">'+ic("eng")+'<span>Engines</span></button>'+
      '<button class="ni '+(nav==="bypass"?"act":"")+'" data-nav="bypass">'+ic("byp")+'<span>Bypass</span></button>':"")+
    '</nav></div></div>';
  var lg=$("#lg");if(lg)lg.onclick=logout;
  var lgm=$("#lgm");if(lgm)lgm.onclick=logout;
  var sc=document.createElement("div");sc.className="scrim";document.body.appendChild(sc);
  var sb=$(".sb"),tg=$("#mb");
  if(tg){tg.onclick=function(){sb.classList.toggle("open");sc.classList.toggle("on",sb.classList.contains("open"))}}
  if(sc)sc.onclick=function(){sb.classList.remove("open");sc.classList.remove("on")};
  window.__closeDrawer=function(){sb.classList.remove("open");sc.classList.remove("on")};
  Array.prototype.forEach.call(document.querySelectorAll("[data-nav]"),function(b){
    b.onclick=function(){window.__closeDrawer();nav_(b.dataset.nav)}});
}
function nav_(name){stopPoll();setCleanup(null);
  if(name==="dash")viewDash();else if(name==="new")viewWizard();else if(name==="engines")viewEngines();else if(name==="bypass")viewBypass();else if(name==="admin")viewAdmin()}
function logout(){api("POST","/auth/logout").then(function(){render()})}
function closeDrawer(){var w=window.__closeDrawer;if(w)w()}
// ───────────────────────────── login ─────────────────────────────
function viewLogin(){
  stopPoll();setCleanup(null);
  $("#app").innerHTML='<div class="lw"><div class="lc">'+
    '<div class="lhead"><div class="lt">'+MARK_L+'</div><h2>EMUNEL</h2><div class="lsub">Console</div>'+
    '<p class="p">Deploy and manage multi-protocol<br>proxy instances — one panel, zero servers.</p></div>'+
    '<div class="card lcard">'+
    '<div class="fld"><label>Account name</label><input class="inp" id="u" placeholder="admin" autocomplete="username" autofocus></div>'+
    '<div class="fld"><label>Password</label><input class="inp" id="p" type="password" autocomplete="current-password"></div>'+
    '<button class="btn pri lgo" id="go">Sign in</button>'+
    '<p class="fn">Default account is <span class="mono">admin / admin</span> — change it in Admin → System.</p>'+
    '</div>'+
    '<div class="lmeta"><span class="dl"></span>EMUNEL<span class="dl"></span></div>'+
    '</div></div>';
  $("#go").onclick=function(){
    var b=$("#go");b.disabled=true;
    api("POST","/auth/login-password",{name:$("#u").value.trim()||"admin",password:$("#p").value})
    .then(function(){render()}).catch(function(e){b.disabled=false;toast(e.message,"err")});
  };
  $("#p").addEventListener("keydown",function(e){if(e.key==="Enter")$("#go").click()});
}
// ───────────────────────────── dashboard ─────────────────────────────
function viewDash(){
  shell("dash");
  var v=$("#view");
  v.innerHTML='<div class="ph"><div><h1>Dashboard</h1><div class="sub">Your EMUNEL instances at a glance.</div></div>'+
    '<div class="ha"><button class="btn pri" data-go="new">+ Create Instance</button></div></div>'+
    '<div class="sgs" id="sgs"></div><h3 style="margin:0 0 10px;font-size:13.5px">Instances</h3><div id="il"></div>'+
    '<div class="card" style="margin-top:22px"><h3>Recent activity</h3><div id="ac" class="mut">—</div></div>';
  Array.prototype.forEach.call(v.querySelectorAll("[data-go]"),function(b){b.onclick=function(){nav_(b.dataset.go)}});
  var t=null;
  function load(){
    return Promise.all([api("GET","/api/instances"),api("GET","/api/activity")]).then(function(rs){
      var list=rs[0].instances, act=rs[1].activity;
      var run=0,sto=0,fail=0;list.forEach(function(i){if(i.status==="running")run++;else if(i.status==="failed")fail++;else sto++});
      $("#sgs").innerHTML=sg("Active instances",list.length)+sg("Running",run,"var(--grn)")+sg("Stopped",sto)+sg("Failed",fail,fail?"var(--red)":null);
      var il=$("#il");
      if(!list.length){il.innerHTML='<div class="empty"><b>No instances yet</b>Deploy your first one in under a minute.<div style="margin-top:14px"><button class="btn pri" data-go="new">Create your first instance</button></div></div>'}
      else{il.innerHTML='<div class="ig">'+list.map(card).join("")+"</div>";
        Array.prototype.forEach.call(il.querySelectorAll("[data-id]"),function(c){c.onclick=function(){viewInst(c.dataset.id)}})}
      $("#ac").innerHTML=act.length?act.slice(0,8).map(function(a){return '<div style="display:flex;gap:10px;padding:8px 0;border-bottom:1px solid var(--bd)"><span class="ftx mono" style="width:64px;flex:none">'+ago(a.ts)+'</span><span class="mut">'+esc(a.message)+"</span></div>"}).join(""):"Nothing yet.";
      var go=v.querySelector(".empty [data-go]");if(go)go.onclick=function(){nav_("new")};
      return list;
    });
  }
  function sg(l,v,c){return '<div class="sg"><div class="l">'+l+'</div><div class="v" style="'+(c?"color:"+c:"")+'">'+v+"</div></div>"}
  function card(i){
    var ep=i.endpoint_url||(i.domain&&i.domain.indexOf("-")>0&&i.domain.length>30?null:null);
    return '<div class="ic" data-id="'+i.id+'"><div class="t"><span class="nm">'+esc(i.name)+"</span>"+stEl(i.status).outerHTML+"</div>"+
      (i.endpoint_url?'<div class="ep">'+esc(i.endpoint_url)+"</div>":'<div class="ep ftx">no endpoint yet</div>')+
      '<div class="mt"><span>'+esc(i.region)+"</span>"+(i.volume_limit_bytes?'<span class="chip">'+fmtBytes(i.volume_limit_bytes)+"</span>":"")+(i.volume_expires_at?'<span class="chip">'+(Math.ceil((new Date(i.volume_expires_at)-Date.now())/864e5)||1)+"d left</span>":"")+"<span>"+i.deployments_count+' deploys</span><span>created '+ago(i.created_at)+"</span></div></div>";
  }
  load().then(function(list){
    pollTimer=poll(6000,function(){
      if(list.some(function(i){return BUSY[i.status]}))return load();
    });
  });
}
// ───────────────────────────── wizard ─────────────────────────────
var PROTOS=[["vless-ws","VLESS over WebSocket","Widest client support (v2rayNG, NekoBox). Recommended."],
["trojan-ws","Trojan over WebSocket","TLS-like handshake, good under strict DPI."],
["shadowsocks","Shadowsocks AEAD","Lightweight AEAD (chacha20 / aes-gcm) over WebSocket."],
["xhttp-packet-up","VLESS xHTTP (packet-up)","HTTP-native transport. Requires an xHTTP-compatible client."],
["xhttp-stream-up","VLESS xHTTP (stream-up)","Streaming HTTP upload. Requires a compatible client and edge."],
["trojan-xhttp-packet-up","Trojan xHTTP (packet-up)","Trojan over HTTP-native packet uploads. Use an xHTTP-compatible Xray client."],
["trojan-xhttp-stream-up","Trojan xHTTP (stream-up)","Trojan over streaming HTTP. Requires a compatible client and edge."],
["vmess-ws","VMess AEAD over WebSocket","Requires an operator-installed, SHA256-pinned Xray executable on the worker."]];
function viewWizard(){
  shell("new");
  var m={name:"",region:"local",protocol:"vless-ws",protocols:["vless-ws"],cpu:0.5,mem:256,limit:"",unit:"GB",expiry:"",speed:"",ip:""},step=0;
  var v=$("#view");
  v.innerHTML='<div class="ph"><div><h1>Create Instance</h1><div class="sub">Name it, pick a protocol, deploy. No servers, no YAML.</div></div></div>'+
    '<div class="row" id="stb" style="gap:4px;margin-bottom:20px"></div><div class="card" id="sb"></div>'+
    '<div class="row" style="margin-top:18px"><button class="btn" id="bk">Back</button><div class="grow"></div><button class="btn pri" id="nx">Continue</button></div>';
  var steps=["Name","Region","Config","Networking","Limits","Review","Deploy"];
  function bar(){ $("#stb").innerHTML=steps.map(function(s,i){return '<div class="grow" style="height:3px;border-radius:2px;background:'+(i<step?"var(--acc)":i===step?"var(--blu)":"var(--bd)")+'"></div>'}).join("")}
  function show(){
    bar();var b=$("#sb");
    $("#bk").disabled=step===0;$("#nx").textContent=step===5?"Deploy":step===6?"Go to instance":"Continue";
    $("#nx").classList.toggle("pri",step!==6);
    if(step===0){b.innerHTML='<h3 style="margin:0 0 10px">Step 1 — Name</h3><div class="fld"><label>Instance name</label><input class="inp" id="f-n" maxlength="60" placeholder="e.g. Production" value="'+esc(m.name)+'"></div><div class="ftx" style="font-size:12px">Letters, numbers, dashes. Up to 25 instances per account.</div>';
      $("#f-n").oninput=function(e){m.name=e.target.value}}
    else if(step===1){b.innerHTML='<h3 style="margin:0 0 10px">Step 2 — Region</h3><div class="optg" id="rg"><div class="opt sel" data-id="local"><div class="t">Local node</div><div class="d">Default worker on this platform</div></div></div>';
      Array.prototype.forEach.call(b.querySelectorAll(".opt"),function(o){o.onclick=function(){m.region=o.dataset.id;Array.prototype.forEach.call(b.querySelectorAll(".opt"),function(x){x.classList.toggle("sel",x===o)})}})}
    else if(step===2){b.innerHTML='<h3 style="margin:0 0 10px">Step 3 — Protocols & resources</h3><div class="fld"><div class="optg">'+PROTOS.map(function(p){return '<div class="opt '+(m.protocols.indexOf(p[0])>=0?"sel":"")+'" data-id="'+p[0]+'"><div class="t">'+p[1]+'</div><div class="d">'+p[2]+"</div></div>"}).join("")+'</div><p class="ftx" style="font-size:11.5px;margin-top:7px">Pick one or more \u2014 each selected protocol gets its own config in the subscription.</p></div><div class="row"><div class="fld" style="width:160px;margin:0"><label>CPU (cores)</label><select class="inp" id="f-c">'+[0.25,0.5,1,2,4].map(function(x){return '<option value="'+x+'" '+(m.cpu===x?"selected":"")+">"+x+"</option>"}).join("")+'</select></div><div class="fld" style="width:160px;margin:0"><label>Memory</label><select class="inp" id="f-m">'+[128,256,512,1024,2048].map(function(x){return '<option value="'+x+'" '+(m.mem===x?"selected":"")+">"+x+" MB</option>"}).join("")+"</select></div></div>";
      Array.prototype.forEach.call(b.querySelectorAll(".opt"),function(o){o.onclick=function(){
        var i=m.protocols.indexOf(o.dataset.id);
        if(i>=0){if(m.protocols.length>1){m.protocols.splice(i,1);o.classList.remove("sel")}}
        else{m.protocols.push(o.dataset.id);o.classList.add("sel")}}});
      $("#f-c").onchange=function(e){m.cpu=parseFloat(e.target.value)};$("#f-m").onchange=function(e){m.mem=parseInt(e.target.value,10)}}
    else if(step===3){b.innerHTML='<h3 style="margin:0 0 10px">Step 4 — Networking</h3><div class="card" style="background:var(--bg2)"><div class="row"><span class="chip">https</span><span class="mono">&lt;console-host&gt;/i/&lt;private-token&gt;</span></div><p class="ftx" style="margin:9px 0 0;font-size:12.5px">WebSocket, xHTTP and all EMUNEL protocols work through this endpoint with automatic TLS. Ready on deploy.</p></div>'}
    else if(step===4){b.innerHTML='<h3 style="margin:0 0 10px">Step 5 — Traffic limits (optional)</h3><p class="mut" style="font-size:12.5px;margin:6px 0 14px">Applied to every config of this instance and enforced for real by the Core. Leave everything empty for the default — unlimited.</p>'+
      '<div class="row" style="flex-wrap:wrap"><div class="fld" style="width:170px;margin:0"><label>Traffic quota</label><input class="inp" id="f-l" type="number" min="0" step="any" placeholder="unlimited" value="'+esc(m.limit)+'"></div>'+
      '<div class="fld" style="width:90px;margin:0"><label>Unit</label><select class="inp" id="f-u">'+["KB","MB","GB","TB"].map(function(u){return '<option value="'+u+'" '+(m.unit===u?"selected":"")+">"+u+"</option>"}).join("")+"</select></div>"+
      '<div class="fld" style="width:150px;margin:0"><label>Expiry (days)</label><input class="inp" id="f-e" type="number" min="0" step="any" placeholder="never" value="'+esc(m.expiry)+'"></div>'+
      '<div class="fld" style="width:140px;margin:0"><label>Speed (Mbps)</label><input class="inp" id="f-s" type="number" min="0" step="any" placeholder="unlimited" value="'+esc(m.speed)+'"></div>'+
      '<div class="fld" style="width:130px;margin:0"><label>IP limit</label><input class="inp" id="f-i" type="number" min="0" step="1" placeholder="unlimited" value="'+esc(m.ip)+'"></div></div>'+
      '<div class="row" style="margin-top:10px;gap:6px" id="lp"></div>'+
      '<p class="fn" style="margin-top:10px">Tip: everything stays editable on the instance Config tab — changing a quota propagates without redeploying.</p>';
      var LP=[["100 MB",100,"MB"],["1 GB",1,"GB"],["5 GB",5,"GB"],["10 GB",10,"GB"],["50 GB",50,"GB"],["7 days",0,""]];
      $("#lp").innerHTML=LP.map(function(p,i){return '<button class="qch" data-i="'+i+'">'+p[0]+"</button>"}).join("");
      Array.prototype.forEach.call(b.querySelectorAll(".qch"),function(c){c.onclick=function(){var p=LP[+c.dataset.i];
        if(p[2]){$("#f-l").value=String(p[1]);$("#f-u").value=p[2]}else{$("#f-e").value="7"}
        Array.prototype.forEach.call(b.querySelectorAll(".qch"),function(x){x.classList.toggle("on",x===c)})}});
      $("#f-l").oninput=function(e){m.limit=e.target.value};$("#f-u").onchange=function(e){m.unit=e.target.value};
      $("#f-e").oninput=function(e){m.expiry=e.target.value};$("#f-s").oninput=function(e){m.speed=e.target.value};$("#f-i").oninput=function(e){m.ip=e.target.value}}
    else if(step===5){var lim=(m.limit?""+m.limit+" "+m.unit:"unlimited");b.innerHTML='<h3 style="margin:0 0 10px">Step 6 — Review</h3><table class="tbl"><tr><td style="color:var(--fnt);width:40%">Name</td><td class="mono">'+(esc(m.name)||"—")+"</td></tr><tr><td style='color:var(--fnt)'>Region</td><td class='mono'>"+esc(m.region)+"</td></tr><tr><td style='color:var(--fnt)'>Protocols</td><td class='mono'>"+esc(m.protocols.join(", "))+"</td></tr><tr><td style='color:var(--fnt)'>CPU / Memory</td><td class='mono'>"+m.cpu+" core / "+m.mem+" MB</td></tr><tr><td style='color:var(--fnt)'>Quota / Expiry</td><td class='mono'>"+esc(lim)+" · "+(m.expiry?esc(m.expiry)+" days":"never expires")+"</td></tr><tr><td style='color:var(--fnt)'>Speed / IP limit</td><td class='mono'>"+(m.speed?esc(m.speed)+" Mbps":"—")+" · "+(m.ip?esc(m.ip)+" IPs":"unlimited")+"</td></tr></table>"}
    else if(step===6){b.innerHTML='<h3 style="margin:0 0 10px">Step 7 — Deploy</h3><div class="kv"><div class="it"><div class="k">Status</div><div class="v" id="ds">Deploying…</div></div><div class="it"><div class="k">Deployment</div><div class="v" id="di">—</div></div></div><div class="term" style="margin-top:14px"><div class="tbody" id="dl" style="height:220px"><div class="ll"><span class="t">»</span> queued</div></div></div>'}
  }
  $("#bk").onclick=function(){if(step>0&&step!==6){step--;show()}};
  $("#nx").onclick=function(){
    if(step===0){if(m.name.trim().length<2){toast("Give the instance a name (2+ chars)","err");return}step=1}
    else if(step===4){
      var bad=null;
      if(m.limit!==""&&(!isFinite(+m.limit)||+m.limit<=0))bad="quota must be a positive number — or empty for unlimited";
      else if(m.expiry!==""&&(!isFinite(+m.expiry)||+m.expiry<=0))bad="expiry must be a positive number of days — or empty";
      else if(m.speed!==""&&(!isFinite(+m.speed)||+m.speed<=0))bad="speed must be a positive number — or empty";
      else if(m.ip!==""&&(!isFinite(+m.ip)||+m.ip<1||Math.floor(+m.ip)!==+m.ip))bad="IP limit must be a whole number ≥ 1 — or empty";
      if(bad){toast(bad,"err");return}step=5}
    else if(step===5){step=6;show();$("#nx").disabled=true;
      api("POST","/api/instances",{name:m.name,region:m.region,config:{protocol:m.protocols[0],protocols:m.protocols,cpu_limit:m.cpu,memory_mb:m.mem,
        limit:(m.limit===""?null:+m.limit),unit:m.unit,
        expiry_days:(m.expiry===""?null:+m.expiry),
        speed_mbps:(m.speed===""?null:+m.speed),
        ip_limit:(m.ip===""?null:parseInt(m.ip,10))}})
      .then(function(created){return api("POST","/api/instances/"+created.id+"/deploy").then(function(d){return{c:created,d:d}})})
      .then(function(r){
        $("#di").textContent=r.d.deployment_id.slice(0,8);var seen=0;
        pollTimer=poll(2000,function(){
          return Promise.all([api("GET","/api/instances/"+r.c.id+"/deployments/"+r.d.deployment_id+"/logs"),api("GET","/api/instances/"+r.c.id+"/deployments")])
          .then(function(rs){
            var logs=rs[0].logs;for(;seen<logs.length;seen++){var e=document.createElement("div");e.className="ll "+logs[seen].level;e.innerHTML='<span class="lv">'+logs[seen].level+"</span> "+esc(logs[seen].message);var dl=$("#dl");if(dl){dl.appendChild(e);dl.scrollTop=dl.scrollHeight}}
            var dep=(rs[1].deployments||[]).filter(function(d){return d.id===r.d.deployment_id})[0];
            if(dep){$("#ds").textContent=dep.status.replace("_"," ");
              if(dep.status==="running"){$("#ds").style.color="var(--grn)";stopPoll();$("#nx").disabled=false;$("#nx").textContent="Go to instance";$("#nx").onclick=function(){viewInst(r.c.id)};toast("Instance is running","ok")}
              else if(dep.status==="failed"){$("#ds").style.color="var(--red)";stopPoll();$("#nx").disabled=false;$("#nx").textContent="Retry";$("#nx").onclick=function(){viewInst(r.c.id)};toast("Deployment failed: "+(dep.error||"unknown"),"err",8000)}}
          }).catch(function(){});
        });
      }).catch(function(e){toast(e.message,"err",6000);step=5;show();$("#nx").disabled=false});
      return}
    else if(step<6)step+=1;
    show();
  };
  show();
}
// ───────────────────────────── instance page ─────────────────────────────
var TABS=["config","volume","overview","logs","networking","deployments","activity","settings"];
function viewInst(id){
  shell("dash");
  var v=$("#view");v.innerHTML='<div class="lw"><span class="sp1"></span></div>';
  var inst=null,tab="overview",logPaused=false,logBuf=[];
  function head(){
    var pd=(inst.domains||[]).filter(function(d){return d.kind==="path"})[0];
    v.innerHTML='<div class="ph"><div><div class="row" style="gap:11px"><h1>'+esc(inst.name)+"</h1>"+stEl(inst.status).outerHTML+'</div><div class="sub" id="ep"></div></div>'+
      '<div class="ha"><button class="btn" id="a-r">Restart</button><button class="btn" id="a-s">Stop</button><button class="btn" id="a-rd">Redeploy</button><button class="btn dng" id="a-d">Delete</button></div></div>'+
      '<div class="tabs">'+TABS.map(function(t){return '<button class="tab '+(t===tab?"act":"")+'" data-t="'+t+'">'+t[0].toUpperCase()+t.slice(1)+"</button>"}).join("")+'</div><div id="tb"></div>';
    var epEl=$("#ep");
    if(pd){var url=location.origin+"/i/"+pd.domain;
      epEl.innerHTML='<span class="mono">'+esc(url)+"</span> ";epEl.appendChild(copyBtn(url));var a=document.createElement("a");a.href=url;a.target="_blank";a.rel="noopener";a.textContent="open ↗";epEl.appendChild(a)}
    else epEl.textContent="no endpoint yet — deploy the instance";
    var busy=BUSY[inst.status];["a-r","a-s","a-rd","a-d"].forEach(function(x){var b=$("#"+x);if(b)b.disabled=!!busy});
    $("#a-r").onclick=function(){act("restart")};$("#a-s").onclick=function(){act("stop")};$("#a-rd").onclick=function(){act("redeploy")};
    $("#a-d").onclick=function(){if(confirm('Delete "'+inst.name+'"? This is permanent.')){api("DELETE","/api/instances/"+id).then(nav_("dash")).catch(function(e){toast(e.message,"err")})}};
    Array.prototype.forEach.call(v.querySelectorAll(".tab"),function(b){b.onclick=function(){tab=b.dataset.t;head();draw()}});
  }
  function act(k){api("POST","/api/instances/"+id+"/"+k).then(function(){toast({restart:"Restarting…",stop:"Stopping…",redeploy:"Redeploying…"}[k],"ok");refresh()}).catch(function(e){toast(e.message,"err")})}
  function refresh(){return api("GET","/api/instances/"+id).then(function(d){inst=d;head();draw()})}
  function draw(){
    var b=$("#tb");if(!b)return;
    if(tab==="config"){
      b.innerHTML='<div class="card"><div class="row" style="justify-content:space-between"><h3>Subscription <span class="free" style="margin-left:6px">Free</span></h3><button class="btn sm" id="cf-r">Refresh</button></div>'+
        '<p class="mut" style="font-size:12.5px;margin:6px 0 10px">One URL, <b>all 4 protocols</b> (VLESS, Trojan, Shadowsocks, xHTTP). Add it under Subscriptions in your client — it auto-updates.</p>'+
        '<div class="row"><div class="mono grow" id="suburl" style="background:var(--bg2);border:1px solid var(--bd);border-radius:7px;padding:8px 10px;word-break:break-all"></div><button class="btn sm pri" id="subc">Copy</button><a class="btn sm" id="subo" target="_blank" rel="noopener">Open</a></div>'+
        '<div class="row" style="margin-top:9px;gap:6px"><span class="ftx" style="font-size:11.5px">Formats:</span>'+
        '<button class="btn sm" id="sub-v2">v2ray/Clash Verge</button><button class="btn sm" id="sub-sb">sing-box</button><button class="btn sm" id="sub-cl">Clash Meta</button></div>'+
        '<div class="card" style="margin-top:14px"><div class="row" style="justify-content:space-between"><h3>Individual configs</h3><button class="btn sm" id="cf-r2">Refresh</button></div><div id="cf-b" class="mut">Loading…</div></div>'+
        '<div class="card" style="margin-top:14px"><h3>Add config</h3>'+
        '<p class="mut" style="font-size:12.5px;margin:6px 0 12px">A new config with its own quota, expiry, speed and IP limits — live immediately, no redeploy.</p>'+
        '<div class="row" style="flex-wrap:wrap"><div class="fld" style="width:170px;margin:0"><label>Protocol</label><select class="inp" id="na-p">'+PROTOS.map(function(p){return '<option value="'+p[0]+'">'+p[1]+"</option>"}).join("")+'</select></div>'+
        '<div class="fld grow" style="min-width:150px;margin:0"><label>Label (optional)</label><input class="inp" id="na-l" maxlength="80" placeholder="e.g. Friend iPhone"></div></div>'+
        '<div class="row" style="flex-wrap:wrap;margin-top:10px"><div class="fld" style="width:130px;margin:0"><label>Quota</label><input class="inp" id="na-q" type="number" min="0" step="any" placeholder="unlimited"></div>'+
        '<div class="fld" style="width:84px;margin:0"><label>Unit</label><select class="inp" id="na-u">'+["KB","MB","GB","TB"].map(function(u){return "<option"+(u==="GB"?" selected":"")+">"+u+"</option>"}).join("")+'</select></div>'+
        '<div class="fld" style="width:120px;margin:0"><label>Days</label><input class="inp" id="na-e" type="number" min="0" step="any" placeholder="never"></div>'+
        '<div class="fld" style="width:110px;margin:0"><label>Mbps</label><input class="inp" id="na-s" type="number" min="0" step="any" placeholder="unlimited"></div>'+
        '<div class="fld" style="width:100px;margin:0"><label>IPs</label><input class="inp" id="na-i" type="number" min="0" step="1" placeholder="unlimited"></div>'+
        '<div class="grow" style="align-self:flex-end"><button class="btn pri" id="na-ok">Create config</button></div></div></div>';
      function numv(el){var v=$(el).value.trim();return v===""?null:parseFloat(v)}
      $("#na-ok").onclick=function(){
        var body={protocol:$("#na-p").value,label:$("#na-l").value,limit:numv("#na-q"),unit:$("#na-u").value,expiry_days:numv("#na-e"),speed_mbps:numv("#na-s"),ip_limit:numv("#na-i")};
        api("POST","/api/instances/"+id+"/links",body).then(function(){toast("Config created","ok");loadCfg()}).catch(function(e){toast(e.message,"err")});
      };
      function linkRow(c,l,pubHost){
        var st=l?l.status:"active";
        var stMap={active:["Active","var(--grn)"],limited:["Limited","var(--amb)"],expired:["Expired","var(--red)"],disabled:["Disabled","var(--red)"]};
        var stL=stMap[st]||[st,"var(--fnt)"];
        var url=c&&c.share_url;
        if(url){var m=url.match(/^(vless|trojan):\/\/([^@]+)@([^\/?#]+)([^#]*)/);
          if(m){var proto=m[1],cred=m[2],inner=m[3],rest=m[4]||"";var innerHost=inner.split(":")[0];
            if(innerHost==="127.0.0.1"||innerHost==="localhost"||innerHost==="0.0.0.0"){url=proto+"://"+cred+"@"+pubHost+rest}}}
        var lim=l?l.limit_bytes:0,used=l?l.used_bytes:0,pct=l&&l.percent!=null?Math.min(100,l.percent):null;
        var meter=(l?
          (lim?'<div class="vmeter'+(l.exceeded?" crit":pct>80?" warn":"")+'" style="margin-top:8px"><div style="width:'+pct+'%"></div></div>'+
            '<div class="row" style="justify-content:space-between;margin-top:5px"><span class="ftx" style="font-size:11.5px">'+fmtBytes(used)+" of "+fmtBytes(lim)+'</span><span class="mono ftx" style="font-size:11.5px">'+fmtBytes(l.remaining_bytes||0)+" left · "+pct.toFixed(1)+"%</span></div>"
          :'<div class="row" style="justify-content:space-between;margin-top:8px"><span class="ftx" style="font-size:11.5px">'+fmtBytes(used)+" used</span>"+'<span class="chip">unlimited</span></div>'):"");
        var kv='<div class="kv" style="margin-top:8px">';
        if(l&&l.expires_at)kv+='<div class="it"><div class="k">Expires</div><div class="v mono">'+esc(String(l.expires_at).slice(0,16).replace("T"," "))+"</div></div>";
        if(l&&l.speed_limit_bytes)kv+='<div class="it"><div class="k">Speed</div><div class="v">'+Math.round(l.speed_limit_bytes*8/1048576)+' Mbps</div></div>';
        if(l&&l.ip_limit)kv+='<div class="it"><div class="k">IP limit</div><div class="v">'+l.ip_limit+"</div></div>";
        kv+="</div>";
        var uuid=l?l.uuid:(c?c.uuid:"");
        return '<div style="margin-top:14px;padding-top:14px;border-top:1px solid var(--bd)">'+
          '<div class="row" style="justify-content:space-between"><div class="row" style="gap:8px"><b style="font-size:12.5px">'+esc((l&&l.label)||(c&&c.label)||"Config")+'</b><span class="chip">'+esc((l&&l.protocol)||(c&&c.protocol)||"")+'</span><span class="chip" style="color:'+stL[1]+';border-color:'+stL[1]+'">'+stL[0]+'</span></div>'+
          '<div class="row" style="gap:5px"><button class="btn sm" data-edt="'+esc(uuid)+'">Edit</button><button class="btn sm" data-tgl="'+esc(uuid)+'">'+((l&&l.active===false)?"Enable":"Disable")+'</button><button class="btn sm" data-rst="'+esc(uuid)+'">Reset</button><button class="btn sm dng" data-del="'+esc(uuid)+'">Delete</button></div></div>'+
          meter+kv+
          (url?'<div class="mono" style="margin-top:7px;background:var(--bg2);border:1px solid var(--bd);border-radius:7px;padding:8px 10px;word-break:break-all;max-height:90px;overflow:auto">'+esc(url)+"</div>"+
          '<div class="row" style="margin-top:6px"><button class="btn sm pri" data-copy="'+esc(url)+'">Copy</button><button class="btn sm" data-qr="'+esc(url)+'">QR</button></div>'
          :'<p class="ftx" style="font-size:11.5px;margin:6px 0 0">No client link while this config is '+stL[0].toLowerCase()+" — enable it or raise its quota.</p>")+
          '<div id="ed-'+esc(uuid)+'" style="display:none;margin-top:10px;background:var(--bg2);border:1px solid var(--bd);border-radius:9px;padding:12px">'+
          '<div class="row" style="flex-wrap:wrap"><div class="fld" style="width:130px;margin:0"><label>Quota</label><input class="inp ed-q" type="number" min="0" step="any" placeholder="unlimited" value="'+(lim?(+(lim/UNIT_BYTES(edUnit(l)))).toString().slice(0,8):"")+'"></div>'+
          '<div class="fld" style="width:84px;margin:0"><label>Unit</label><select class="inp ed-u">'+["KB","MB","GB","TB"].map(function(u){return "<option"+(edUnit(l)===u?" selected":"")+">"+u+"</option>"}).join("")+'</select></div>'+
          '<div class="fld" style="width:120px;margin:0"><label>Days</label><input class="inp ed-e" type="number" min="0" step="any" placeholder="never" value="'+edDays(l)+'"></div>'+
          '<div class="fld" style="width:110px;margin:0"><label>Mbps</label><input class="inp ed-s" type="number" min="0" step="any" placeholder="unlimited" value="'+((l&&l.speed_limit_bytes)?Math.round(l.speed_limit_bytes*8/1048576):"")+'"></div>'+
          '<div class="fld" style="width:100px;margin:0"><label>IPs</label><input class="inp ed-i" type="number" min="0" step="1" placeholder="unlimited" value="'+((l&&l.ip_limit)||"")+'"></div>'+
          '<div class="grow" style="align-self:flex-end;display:flex;gap:6px"><button class="btn sm pri" data-save="'+esc(uuid)+'">Save</button><button class="btn sm" data-cxl="'+esc(uuid)+'">Cancel</button></div></div>'+
          '<p class="fn" style="margin-top:8px">Empty means unlimited. Saving propagates to the Core immediately — no redeploy.</p></div></div>';
      }
      function UNIT_BYTES(u){return {KB:1024,MB:1048576,GB:1073741824,TB:1099511627776}[u]||1073741824}
      function edUnit(l){if(!l||!l.limit_bytes)return "GB";var n=l.limit_bytes;if(n>=1073741824)return "GB";if(n>=1048576)return "MB";return "KB"}
      function edDays(l){if(!l||!l.expires_at)return "";var s=l.seconds_remaining;return s==null?"":+(s/86400).toFixed(3)}
      function loadCfg(){
        // tell the server the public host we're browsing on (edge hides it)
        api("POST","/api/instances/"+id+"/announce-host",{host:location.host}).catch(function(){});
        $("#cf-b").innerHTML='<span class="mut">Loading…</span>';
        Promise.all([api("GET","/api/instances/"+id+"/config"),api("GET","/api/instances/"+id+"/links").catch(function(){return{links:[],live:false}})])
        .then(function(rs){
          var d=rs[0],links=rs[1]&&rs[1].links?rs[1].links:[];
          var subUrl=location.origin+"/i/"+(d.endpoint_path||"").replace("/i/","")+"/sub";
          if(d.endpoint_path){$("#suburl").textContent=subUrl;
            $("#subc").onclick=function(){navigator.clipboard&&navigator.clipboard.writeText(subUrl).then(function(){toast("Subscription URL copied","ok",2500)})};
            $("#subo").href=subUrl+"?host="+location.host;
            var v2=location.origin+"/i/"+d.endpoint_path.split("/i/")[1]+"/sub?host="+location.host;
            $("#sub-v2").onclick=function(){navigator.clipboard&&navigator.clipboard.writeText(v2).then(function(){toast("v2ray sub URL copied","ok",2500)})};
            $("#sub-sb").onclick=function(){navigator.clipboard&&navigator.clipboard.writeText(v2+"&fmt=singbox").then(function(){toast("sing-box sub URL copied","ok",2500)})};
            $("#sub-cl").onclick=function(){navigator.clipboard&&navigator.clipboard.writeText(v2+"&fmt=clash").then(function(){toast("Clash sub URL copied","ok",2500)})};}
          var pubHost=location.host;
          var byUuid={};(d.configs||[]).forEach(function(c){if(c.uuid)byUuid[c.uuid]=c});
          var rows=[];var seen={};
          links.forEach(function(l){seen[l.uuid]=1;rows.push(linkRow(byUuid[l.uuid]||null,l,pubHost))});
          (d.configs||[]).forEach(function(c){if(!seen[c.uuid])rows.push(linkRow(c,null,pubHost))});
          if(!rows.length){
            $("#cf-b").innerHTML='<span class="ftx">'+esc(d.error||"No configs yet — if the instance shows Running, press Redeploy once (instances created before this fix get their links on redeploy).")+"</span>";
            return;
          }
          $("#cf-b").innerHTML='<p class="ftx" style="font-size:11.5px;margin:0 0 2px">'+(rs[1]&&rs[1].live?"Live usage from the Core":"Cached — Core unreachable")+"</p>"+rows.join("");
          Array.prototype.forEach.call($("#cf-b").querySelectorAll("[data-copy]"),function(btn){
            btn.onclick=function(){navigator.clipboard&&navigator.clipboard.writeText(btn.dataset.copy).then(function(){toast("Copied — v2rayNG: Import from clipboard","ok",4000)})}});
          Array.prototype.forEach.call($("#cf-b").querySelectorAll("[data-qr]"),function(btn){
            btn.onclick=function(){
              var ov=document.createElement("div");ov.className="qr-ov";
              ov.innerHTML='<div class="qr-c"><b style="font-size:13px">Scan with your client</b><div class="qrbox" style="margin:10px 0"><span class="sp1"></span></div><button class="btn sm" id="qrx">Close</button></div>';
              document.body.appendChild(ov);
              ov.onclick=function(e){if(e.target===ov)ov.remove()};
              $("#qrx",ov).onclick=function(){ov.remove()};
              api("POST","/api/instances/"+id+"/qr",{text:btn.dataset.qr}).then(function(svg){
                $(".qrbox",ov).innerHTML=svg}).catch(function(e){ov.remove();toast(e.message,"err")});
            }});
          Array.prototype.forEach.call($("#cf-b").querySelectorAll("[data-edt]"),function(btn){
            btn.onclick=function(){var ed=$("#ed-"+btn.dataset.edt);if(ed)ed.style.display=ed.style.display==="none"?"block":"none"}});
          Array.prototype.forEach.call($("#cf-b").querySelectorAll("[data-cxl]"),function(btn){
            btn.onclick=function(){var ed=$("#ed-"+btn.dataset.cxl);if(ed)ed.style.display="none"}});
          Array.prototype.forEach.call($("#cf-b").querySelectorAll("[data-save]"),function(btn){
            btn.onclick=function(){
              var ed=$("#ed-"+btn.dataset.save);if(!ed)return;
              function v(sel){var x=ed.querySelector(sel);var s=x?x.value.trim():"";return s===""?null:parseFloat(s)}
              var u=ed.querySelector(".ed-u").value;
              var body={limit:v(".ed-q"),unit:u,expiry_days:v(".ed-e"),speed_mbps:v(".ed-s"),ip_limit:v(".ed-i")};
              api("PATCH","/api/instances/"+id+"/links/"+btn.dataset.save,body)
                .then(function(l){toast("Saved — quota "+(l.limit_bytes?fmtBytes(l.limit_bytes):"unlimited"),"ok");loadCfg()})
                .catch(function(e){toast(e.message,"err")});
            }});
          Array.prototype.forEach.call($("#cf-b").querySelectorAll("[data-tgl]"),function(btn){
            btn.onclick=function(){
              var want=btn.textContent.trim().toLowerCase()==="enable";
              api("PATCH","/api/instances/"+id+"/links/"+btn.dataset.tgl,{active:want})
                .then(function(){toast(want?"Config enabled":"Config disabled","ok");loadCfg()})
                .catch(function(e){toast(e.message,"err")});
            }});
          Array.prototype.forEach.call($("#cf-b").querySelectorAll("[data-rst]"),function(btn){
            btn.onclick=function(){
              api("POST","/api/instances/"+id+"/links/"+btn.dataset.rst+"/reset")
                .then(function(){toast("Usage counter reset","ok");loadCfg()})
                .catch(function(e){toast(e.message,"err")});
            }});
          Array.prototype.forEach.call($("#cf-b").querySelectorAll("[data-del]"),function(btn){
            btn.onclick=function(){
              if(!confirm("Delete this config? Clients using it stop working immediately."))return;
              api("DELETE","/api/instances/"+id+"/links/"+btn.dataset.del)
                .then(function(){toast("Config deleted","ok");loadCfg()})
                .catch(function(e){toast(e.message,"err")});
            }});
        }).catch(function(e){$("#cf-b").innerHTML='<span class="ftx">'+esc(e.message)+"</span>"});
      }
      $("#cf-r").onclick=loadCfg;var cf2=$("#cf-r2");if(cf2)cf2.onclick=loadCfg;loadCfg();
    }
    else if(tab==="volume"){
      b.innerHTML='<div class="card" style="max-width:620px"><div class="row" style="justify-content:space-between"><h3>'+ic("vol")+' Volume & time</h3><button class="btn sm" id="vrf">Refresh</button></div>'+
        '<p class="mut" style="font-size:12.5px;margin:6px 0 16px">Usage and caps for this instance. Leave a limit empty for the default — unlimited.</p>'+
        '<div id="vt"></div></div>'+
        '<div class="card" style="max-width:620px;margin-top:14px"><h3>Limits</h3>'+
        '<div class="row" style="margin-top:12px;flex-wrap:wrap"><div class="fld" style="width:190px;margin:0"><label>Volume limit (GB)</label><input class="inp" id="vg" type="number" min="0.001" step="0.1" placeholder="unlimited"></div>'+
        '<div class="fld" style="width:190px;margin:0"><label>Time limit (days)</label><input class="inp" id="vtd" type="number" min="0.0007" step="any" placeholder="unlimited"></div><div class="grow"></div></div>'+
        '<div class="row" style="margin-top:10px;gap:6px" id="vp"></div>'+
        '<div class="row" style="margin-top:6px;gap:6px" id="vp2"></div>'+
        '<div class="row" style="margin-top:16px"><button class="btn pri" id="vs">Save limits</button><button class="btn" id="vr">Reset usage counter</button></div>'+
        '<p class="fn" id="vn"></p></div>';
      var PRE=[10,50,100,250,500,0];
      var TPRE=[7,30,90,180,365,0];
      $("#vp").innerHTML=PRE.map(function(g){return '<button class="qch" data-g="'+g+'">'+(g?g+" GB":"Unlimited")+"</button>"}).join("");
      $("#vp2").innerHTML=TPRE.map(function(d){return '<button class="qch" data-d="'+d+'">'+(d?d+" days":"Unlimited")+"</button>"}).join("");
      function fmtLeft(s){if(s==null||s<0)return"—";var d=Math.floor(s/86400),h=Math.floor(s%86400/3600);return d>=1?d+"d "+h+"h":h>=1?h+"h "+Math.floor(s%3600/60)+"m":Math.floor(s/60)+"m"}
      Array.prototype.forEach.call(b.querySelectorAll(".qch"),function(c){c.onclick=function(){
        if(c.dataset.g!==undefined){$("#vg").value=c.dataset.g==="0"?"":c.dataset.g}
        else{$("#vtd").value=c.dataset.d==="0"?"":c.dataset.d}
        Array.prototype.forEach.call(b.querySelectorAll(".qch"),function(x){x.classList.toggle("on",x===c)})}});
      function loadV(){
        api("GET","/api/instances/"+id+"/volume").then(function(v){
          var t=$("#vt");if(!t)return;
          var lim=v.limit_bytes,pct=v.percent==null?null:Math.min(100,v.percent);
          t.innerHTML=(lim?
            '<div class="vmeter'+(v.exceeded?" crit":pct>80?" warn":"")+'"><div style="width:'+pct+'%"></div></div>'+
            '<div class="row" style="justify-content:space-between;margin-top:8px"><b class="mono" style="font-size:16px">'+fmtBytes(v.used_bytes)+' <span class="ftx" style="font-size:12px">of '+fmtBytes(lim)+'</span></b><span class="mono ftx">'+pct.toFixed(1)+"%</span></div>"
            :'<div class="row" style="justify-content:space-between"><b class="mono" style="font-size:16px">'+fmtBytes(v.used_bytes)+'</b><span class="chip">default · unlimited</span></div>')+
            '<div class="kv" style="margin-top:16px">'+
            '<div class="it"><div class="k">Limit</div><div class="v">'+(lim?fmtBytes(lim):"Unlimited")+"</div></div>"+
            '<div class="it"><div class="k">Used</div><div class="v">'+fmtBytes(v.used_bytes)+(v.live?"":" · cached")+"</div></div>"+
            '<div class="it"><div class="k">Remaining</div><div class="v">'+(v.remaining_bytes==null?"—":fmtBytes(v.remaining_bytes))+"</div></div>"+
            '<div class="it"><div class="k">Source</div><div class="v">'+(v.live?"live core":"last known")+"</div></div>"+
            '<div class="it"><div class="k">Time limit</div><div class="v">'+(v.time_limit_days!=null?(+v.time_limit_days)+" days":"Unlimited")+"</div></div>"+
            '<div class="it"><div class="k">Expires in</div><div class="v">'+(v.expires_at?fmtLeft(v.seconds_remaining)+" · "+v.expires_at.slice(0,10):"—")+"</div></div></div>"+
            (v.exceeded?'<p style="color:var(--red);font-size:12.5px;margin:12px 0 0">Volume limit reached — the instance is stopped. Raise or clear the limit, then deploy again.</p>':"")+
            (v.expired?'<p style="color:var(--red);font-size:12.5px;margin:12px 0 0">Time limit reached — the instance is stopped. Extend or clear the limit, then deploy again.</p>':"");
          $("#vg").value=lim?String(+(lim/1073741824).toFixed(3)):"";
          $("#vtd").value=v.time_limit_days!=null?String(+v.time_limit_days):"";
          $("#vn").textContent=v.used_at?("Usage last refreshed "+ago(v.used_at)+"."):"Usage refreshes while the instance runs.";
        }).catch(function(e){$("#vt").innerHTML='<span class="ftx">'+esc(e.message)+"</span>"});
      }
      $("#vrf").onclick=loadV;
      $("#vs").onclick=function(){
        var raw=$("#vg").value.trim(),gb=null;
        if(raw!==""){gb=parseFloat(raw);if(!isFinite(gb)||gb<=0){toast("Enter a positive number of GB — or leave it empty for unlimited","err");return}}
        var traw=$("#vtd").value.trim(),td=null;
        if(traw!==""){td=parseFloat(traw);if(!isFinite(td)||td<=0){toast("Enter a positive number of days — or leave it empty for unlimited","err");return}}
        api("PUT","/api/instances/"+id+"/volume",{limit_gb:gb,time_limit_days:td}).then(function(v){
          toast("Saved — "+(v.limit_bytes?("volume "+fmtBytes(v.limit_bytes)):"volume unlimited")+" · "+(v.expires_at?("time "+(+v.time_limit_days)+"d"):"time unlimited"),"ok");loadV()})
        .catch(function(e){toast(e.message,"err")})};
      $("#vr").onclick=function(){if(!confirm("Reset the usage counter? Everything transferred so far stops counting against the limit."))return;
        api("POST","/api/instances/"+id+"/volume/reset").then(function(){toast("Usage counter reset","ok");loadV()}).catch(function(e){toast(e.message,"err")})};
      loadV();
    }
    else if(tab==="overview"){
      b.innerHTML='<div class="kv" id="okv"></div><div class="card" style="margin-top:16px"><h3>Latest deployment</h3><div id="odp" class="mut">—</div></div>';
      Promise.all([api("GET","/api/instances/"+id+"/status"),api("GET","/api/instances/"+id+"/metrics")]).then(function(rs){
        var st=rs[0],mt=rs[1],ld=inst.latest_deployment;
        $("#okv").innerHTML=
          '<div class="it"><div class="k">Status</div><div class="v">'+(LBL[inst.status]||inst.status)+"</div></div>"+
          '<div class="it"><div class="k">Uptime</div><div class="v">'+fmtUp(st.core_health&&st.core_health.uptime)+"</div></div>"+
          '<div class="it"><div class="k">Connections</div><div class="v">'+(st.core_health?st.core_health.connections:"—")+"</div></div>"+
          '<div class="it"><div class="k">Version</div><div class="v">'+esc(st.core_health&&st.core_health.version||"—")+"</div></div>"+
          '<div class="it"><div class="k">Region</div><div class="v">'+esc(inst.region)+"</div></div>"+
          '<div class="it"><div class="k">Health</div><div class="v" style="color:'+(st.healthy?"var(--grn)":"var(--fnt)")+'">'+(st.healthy?"healthy":"n/a")+"</div></div>"+
          '<div class="it"><div class="k">Volume</div><div class="v">'+((inst.volume&&inst.volume.limit_bytes)?fmtBytes(inst.volume.limit_bytes):"unlimited")+"</div></div>"+
          '<div class="it"><div class="k">Time limit</div><div class="v">'+((inst.volume&&inst.volume.expires_at)?inst.volume.expires_at.slice(0,10):"unlimited")+"</div></div>";
        $("#odp").innerHTML=ld?'<div class="row">'+stEl(ld.status).outerHTML+'<span class="chip">v'+ld.version+'</span><span class="ftx">started '+ago(ld.started_at)+" · "+dur(ld.duration_ms)+"</span></div>"+(ld.error?'<p style="color:var(--red);font-size:12px;margin:7px 0 0">'+esc(ld.error)+"</p>":""):"—";
      }).catch(function(){});
    }
    else if(tab==="logs"){
      b.innerHTML='<div class="term"><div class="tb"><button class="btn sm" id="lp">Pause</button><div class="sp"></div><button class="btn sm" id="lc">Copy</button><button class="btn sm" id="ld">Download</button></div><div class="tbody" id="lb"><div class="te">Waiting for logs…</div></div></div>';
      $("#lp").onclick=function(e){logPaused=!logPaused;e.target.textContent=logPaused?"Resume":"Pause";e.target.classList.toggle("pri",logPaused)};
      $("#lc").onclick=function(){navigator.clipboard&&navigator.clipboard.writeText(logBuf.map(function(l){return l.level+" "+l.message}).join("\n")).then(function(){toast("Copied","ok",1200)})};
      $("#ld").onclick=function(){var blob=new Blob([logBuf.map(function(l){return new Date(l.ts*1e3).toISOString()+" "+l.level+" "+l.message}).join("\n")],{type:"text/plain"});var a=document.createElement("a");a.href=URL.createObjectURL(blob);a.download=inst.slug+"-logs.txt";a.click()};
      pollLogs();
    }
    else if(tab==="networking"){
      var list=(inst.domains||[]);
      b.innerHTML='<div class="card"><div class="row" style="justify-content:space-between"><h3>Endpoints</h3><button class="btn" id="rg">Regenerate</button></div><div id="dl2"></div></div>'+
        '<div class="card" style="margin-top:14px"><h3>Protocol paths</h3><table class="tbl"><tr><td>VLESS</td><td class="mono">/ws/&lt;uuid&gt; · /xhttp-siz10/…</td></tr><tr><td>Trojan</td><td class="mono">/trojan-ws · /txhttp-siz10/…</td></tr><tr><td>Shadowsocks</td><td class="mono">/ss-ws (AEAD)</td></tr></table><p class="ftx" style="font-size:12px;margin:9px 0 0">WebSocket upgrade, keep-alive and long-lived connections supported end-to-end. TLS at the edge.</p></div>';
      $("#dl2").innerHTML=list.length?list.map(function(d){var url=d.kind==="path"?(location.origin+"/i/"+d.domain):("https://"+d.domain);
        return '<div class="row" style="margin-top:9px"><span class="chip">'+d.kind+'</span><span class="mono" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:70%">'+esc(url)+"</span></div>"}).join(""):'<p class="mut">No endpoints yet.</p>';
      $("#rg").onclick=function(){if(!confirm("Regenerate endpoints? Old links stop working."))return;
        api("POST","/api/instances/"+id+"/domains").then(function(){toast("Endpoints regenerated","ok");refresh()}).catch(function(e){toast(e.message,"err")})};
    }
    else if(tab==="deployments"){
      b.innerHTML='<div class="card" style="padding:0"><table class="tbl"><thead><tr><th>Version</th><th>Status</th><th>Started</th><th>Took</th><th>Error</th></tr></thead><tbody id="dt"></tbody></table></div>';
      api("GET","/api/instances/"+id+"/deployments").then(function(d){
        $("#dt").innerHTML=d.deployments.length?d.deployments.map(function(x){return "<tr><td class='mono'>v"+x.version+"</td><td>"+stEl(x.status).outerHTML+"</td><td class='ftx'>"+ago(x.started_at)+"</td><td>"+dur(x.duration_ms)+"</td><td class='ftx' style='max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap'>"+esc(x.error||"")+"</td></tr>"}).join(""):'<tr><td colspan="5" class="ftx" style="text-align:center;padding:20px">No deployments yet.</td></tr>'});
    }
    else if(tab==="activity"){
      b.innerHTML='<div class="card"><h3>Activity</h3><div id="af"></div></div>';
      api("GET","/api/instances/"+id+"/activity").then(function(d){
        $("#af").innerHTML=d.activity.length?d.activity.map(function(a){return '<div style="display:flex;gap:10px;padding:8px 0;border-bottom:1px solid var(--bd)"><span class="ftx mono" style="width:64px;flex:none">'+ago(a.ts)+'</span><span class="mut">'+esc(a.message)+"</span></div>"}).join(""):'<span class="ftx">Nothing yet.</span>'});
    }
    else if(tab==="settings"){
      b.innerHTML='<div class="card" style="max-width:520px"><h3>Danger zone</h3><p class="mut" style="font-size:12.5px">Rotate credentials (endpoint token + internal token) and redeploy, or delete this instance.</p><div class="row" style="margin-top:14px"><button class="btn" id="s-rt">Rotate credentials</button><button class="btn dng" id="s-del">Delete instance</button></div></div>';
      $("#s-rt").onclick=function(){if(!confirm("Rotate credentials and redeploy? Clients must re-import the link."))return;
        api("POST","/api/instances/"+id+"/domains").then(function(){return api("POST","/api/instances/"+id+"/redeploy")}).then(function(){toast("Rotated — redeploying","ok");refresh()}).catch(function(e){toast(e.message,"err")})};
      $("#s-del").onclick=function(){if(confirm('Delete "'+inst.name+'"? This is permanent.')){api("DELETE","/api/instances/"+id).then(nav_("dash")).catch(function(e){toast(e.message,"err")})}};
    }
  }
  function pollLogs(){
    api("GET","/api/instances/"+id+"/logs?tail=200").then(function(d){
      if(!logPaused&&d.logs&&d.logs.length){logBuf=logBuf.concat(d.logs).slice(-600);var el=$("#lb");
        if(el){el.innerHTML=logBuf.map(function(l){var lv=(l.level||"info").toLowerCase();return '<div class="ll '+lv+'"><span class="t">'+new Date(l.ts*1e3).toLocaleTimeString()+"</span> <span class='lv'>"+lv.toUpperCase()+"</span> "+esc(l.message)+"</div>"}).join("");el.scrollTop=el.scrollHeight}}})
    .catch(function(){});
  }
  refresh().then(function(){
    pollTimer=poll(5000,function(){
      if(tab==="logs")return pollLogs();
      else if(BUSY[inst.status]||tab==="overview")return refresh();
    });
  });
  setCleanup(function(){});
}
// ───────────────────────────── admin ─────────────────────────────
function viewAdmin(){
  shell("admin");
  var v=$("#view");
  v.innerHTML='<div class="ph"><div><h1>Admin</h1><div class="sub">Platform-wide state. Actions are audited.</div></div></div><div class="sgs" id="as"></div><div id="ab"></div>';
  var tab="instances";
  function stats(){api("GET","/api/admin/overview").then(function(s){
    $("#as").innerHTML='<div class="sg"><div class="l">Users</div><div class="v">'+s.users+'</div></div><div class="sg"><div class="l">Instances</div><div class="v">'+s.instances+'</div></div><div class="sg"><div class="l">Running</div><div class="v" style="color:var(--grn)">'+s.instances_running+'</div></div><div class="sg"><div class="l">Workers online</div><div class="v">'+s.workers_online+"</div></div>"})
  .catch(function(e){if(e.message.indexOf("admin")>=0)nav_("dash")})}
  function draw(){
    var b=$("#ab");
    if(tab==="instances"){
      api("GET","/api/admin/instances").then(function(d){
        b.innerHTML='<div class="card" style="padding:0;overflow-x:auto"><table class="tbl"><thead><tr><th>Instance</th><th>Owner</th><th>Status</th><th>Actions</th></tr></thead><tbody>'+
        d.instances.map(function(i){return '<tr data-id="'+i.id+'"><td><b>'+esc(i.name)+'</b> <span class="ftx mono">'+esc(i.slug)+'</span></td><td>@'+esc(i.owner_login)+"</td><td>"+stEl(i.status).outerHTML+
        '</td><td><div class="row" style="gap:5px"><button class="btn sm" data-a="restart">Restart</button><button class="btn sm" data-a="stop">Stop</button><button class="btn sm dng" data-a="del">Delete</button></div></td></tr>'}).join("")+"</tbody></table></div>";
        Array.prototype.forEach.call(b.querySelectorAll("tr[data-id] .btn"),function(btn){btn.onclick=function(){
          var id=btn.closest("tr").dataset.id,a=btn.dataset.a;
          var p=a==="del"?(confirm("Delete this instance?")?api("DELETE","/api/admin/instances/"+id):Promise.resolve())
            :api("POST","/api/admin/instances/"+id+"/actions/"+a);
          Promise.resolve(p).then(function(){toast(a+" done","ok");draw()}).catch(function(e){toast(e.message,"err")})}});
      });
    }
    else if(tab==="users"){
      api("GET","/api/admin/users").then(function(d){
        b.innerHTML='<div class="card" style="padding:0;overflow-x:auto"><table class="tbl"><thead><tr><th>User</th><th>Instances</th><th>Flags</th><th>Last login</th><th>Actions</th></tr></thead><tbody>'+
        d.users.map(function(u){return '<tr data-id="'+u.id+'"><td><b>'+esc(u.name||u.login)+"</b> <span class='ftx'>@"+esc(u.login)+"</span></td><td>"+u.instance_count+
        "</td><td>"+(u.is_admin?'<span class="chip">admin</span> ':"")+(u.is_disabled?'<span class="chip" style="color:var(--red)">disabled</span>':"")+"</td><td class='ftx'>"+ago(u.last_login_at)+
        '</td><td><div class="row" style="gap:5px"><button class="btn sm" data-a="adm">'+(u.is_admin?"Revoke admin":"Make admin")+'</button><button class="btn sm '+(u.is_disabled?"":"dng")+'" data-a="dis">'+(u.is_disabled?"Enable":"Disable")+"</button></div></td></tr>"}).join("")+"</tbody></table></div>";
        Array.prototype.forEach.call(b.querySelectorAll("tr[data-id] .btn"),function(btn){btn.onclick=function(){
          var id=btn.closest("tr").dataset.id,a=btn.dataset.a;
          var patch=a==="adm"?{is_admin:btn.textContent.indexOf("Make")===0}:{is_disabled:btn.textContent!=="Enable"};
          api("PATCH","/api/admin/users/"+id,patch).then(function(){toast("Updated","ok");draw()}).catch(function(e){toast(e.message,"err")})}});
      });
    }
    else if(tab==="workers"){
      api("GET","/api/admin/workers").then(function(d){
        b.innerHTML='<div class="card" style="padding:0;overflow-x:auto"><table class="tbl"><thead><tr><th>Node</th><th>Region</th><th>Status</th><th>CPU</th><th>Memory</th><th>Capacity</th><th>Heartbeat</th></tr></thead><tbody>'+
        (d.workers.length?d.workers.map(function(w){return "<tr><td class='mono'>"+esc(w.node_id)+"</td><td>"+esc(w.region)+"</td><td>"+stEl(w.status).outerHTML+"</td><td>"+(w.cpu_percent!=null?w.cpu_percent.toFixed(0)+"%":"—")+"</td><td>"+(w.mem_used_mb!=null?w.mem_used_mb+" / "+w.mem_total_mb+" MB":"—")+"</td><td>"+(w.instances||0)+" / "+(w.capacity||"?")+"</td><td class='ftx'>"+ago(w.last_heartbeat)+"</td></tr>"}).join(""):'<tr><td colspan="7" class="ftx" style="text-align:center;padding:20px">No workers reported yet.</td></tr>')+"</tbody></table></div>"});
    }
    else if(tab==="backup"){
      b.innerHTML='<div class="card"><h3>Configuration backup & restore</h3><p class="ftx">Sensitive plaintext backup: includes password hashes and endpoint secrets. Store encrypted offline. Core runtime state, Shadowsocks passwords and worker registry are NOT included.</p><p class="ftx">Import never overwrites existing records. Imported instances are stopped, domains inactive and workers disabled. Target EMUNEL_SECRET_KEY must match. Read the validation warnings before importing.</p><button class="btn" id="backup-export">Download configuration</button><hr><div class="fld"><label>Backup JSON (maximum 16 MiB)</label><input type="file" id="backup-file" accept="application/json,.json"></div><div class="row"><button class="btn" id="backup-check" disabled>Validate</button><button class="btn dng" id="backup-restore" disabled>Import configuration</button></div><pre id="backup-result" style="white-space:pre-wrap;overflow-wrap:anywhere"></pre></div>';
      var backupText=null,backupBusy=false;
      function backupCall(action,body){return fetch('/api/admin/backup/'+action,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-EMUNEL-CSRF':CSRF||'','X-EMUNEL-Backup-Confirm':action==='restore'?'import':''},body:body}).then(function(r){return r.json().then(function(d){if(!r.ok)throw new Error(typeof d.detail==='string'?d.detail:JSON.stringify(d.detail||d));return d})})}
      function backupShow(d){$('#backup-result').textContent=JSON.stringify(d,null,2)}
      $('#backup-export').onclick=async function(){if(backupBusy)return;backupBusy=true;try{var d=await backupCall('export');var url=URL.createObjectURL(new Blob([JSON.stringify(d)],{type:'application/json'}));var a=document.createElement('a');a.href=url;a.download='emunel-configuration-backup-v1.json';document.body.appendChild(a);a.click();a.remove();setTimeout(function(){URL.revokeObjectURL(url)},1000);toast('Configuration downloaded; keep it private','ok')}catch(e){toast(e.message,'err')}finally{backupBusy=false}};
      $('#backup-file').onchange=async function(e){backupText=null;$('#backup-check').disabled=true;$('#backup-restore').disabled=true;var f=e.target.files[0];if(!f)return;if(f.size>16777216){toast('Backup exceeds 16 MiB','err');return}try{backupText=await f.text();$('#backup-check').disabled=false;backupShow({status:'File loaded. Validate before importing.'})}catch(err){toast(err.message,'err')}};
      $('#backup-check').onclick=async function(){if(backupBusy||!backupText)return;backupBusy=true;$('#backup-restore').disabled=true;try{var d=await backupCall('validate',backupText);backupShow(d);$('#backup-restore').disabled=!d.can_restore}catch(e){backupShow({error:e.message})}finally{backupBusy=false}};
      $('#backup-restore').onclick=async function(){if(backupBusy||!backupText)return;if(!confirm('Import this configuration? It contains accounts and credentials. Runtime state is NOT restored. Existing records will never be overwritten.'))return;backupBusy=true;$('#backup-restore').disabled=true;try{backupShow(await backupCall('restore',backupText));toast('Configuration imported; runtime recovery remains manual','ok');backupText=null;$('#backup-check').disabled=true}catch(e){backupShow({error:e.message})}finally{backupBusy=false}};
    }
    else if(tab==="system"){
      Promise.all([api("GET","/api/admin/system"),api("GET","/auth/me")]).then(function(rs){
        b.innerHTML='<div class="card" style="max-width:540px"><h3>Change your password</h3>'+
        '<div class="fld"><label>Current password</label><input class="inp" id="cp" type="password"></div>'+
        '<div class="fld"><label>New password (min 8 chars)</label><input class="inp" id="np" type="password"></div>'+
        '<button class="btn pri" id="cpb">Update password</button></div>'+
        '<div class="card" style="max-width:540px;margin-top:14px"><h3>System</h3><table class="tbl">'+
        '<tr><td style="color:var(--fnt);width:45%">Database</td><td>'+(rs[0].database.ok?"PostgreSQL/SQLite OK":"down")+"</td></tr>"+
        '<tr><td style="color:var(--fnt)">GitHub OAuth</td><td>'+(rs[0].github_oauth?"configured":"not configured (password login)")+"</td></tr>"+
        '<tr><td style="color:var(--fnt)">Railway provider</td><td>'+(rs[0].provider.railway?"configured":"not configured")+"</td></tr></table></div>";
        $("#cpb").onclick=function(){
          api("POST","/auth/change-password",{current_password:$("#cp").value,new_password:$("#np").value})
          .then(function(){toast("Password updated","ok");$("#cp").value="";$("#np").value=""})
          .catch(function(e){toast(e.message,"err")});
        };
      });
    }
  }
  function tabs(){
    v.innerHTML=v.innerHTML.replace(/<div id="ab"><\/div>[\s\S]*$/,'<div class="tabs" id="atb"></div><div id="ab"></div>');
  }
  // simpler: re-render header tabs each time
  function rebuild(extra){
    var old=$("#ab");var head=v.querySelector(".ph"),sgs=$("#as");
    v.innerHTML="";v.appendChild(head);v.appendChild(sgs);
    var tb=document.createElement("div");tb.className="tabs";tb.id="atb";
    ["instances","users","workers","backup","system"].forEach(function(t){var btn=document.createElement("button");btn.className="tab "+(t===tab?"act":"");btn.textContent=t[0].toUpperCase()+t.slice(1);btn.onclick=function(){tab=t;rebuild();draw()};tb.appendChild(btn)});
    var ab=document.createElement("div");ab.id="ab";v.appendChild(tb);v.appendChild(ab);
    draw();
  }
  stats();rebuild();
}
// ───────────────────────────── engines ─────────────────────────────
function viewEngines(){
  shell("engines");
  var v=$("#view");
  v.innerHTML='<div class="ph"><div><h1>Engine Settings</h1>'+
    '<div class="sub">Traffic engines — a plugin layer over the gateway hop, subscription feeds and Core dials. The proxy Core itself is never modified.</div></div>'+
    '<div class="ha"><button class="btn sm" id="eg-st">Run selftest</button><button class="btn sm pri" id="eg-rf">Refresh</button></div></div>'+
    '<div id="eg-w"></div><div class="sgs" id="eg-s"></div><div class="ig" id="eg-c"></div><div class="card" style="margin-top:16px" id="eg-k"></div>';
  function egSg(l,val,c){var s=String(val);
    return '<div class="sg"><div class="l">'+l+'</div><div class="v" style="font-size:'+(s.length>18?"13px":"18px")+';'+(c?"color:"+c:"")+'">'+esc(s)+"</div></div>"}
  function egChip(e){
    if(e.active)return '<span class="chip" style="color:var(--grn);border-color:var(--grn)">Active</span>';
    if(e.reason&&e.reason.indexOf("disabled by env")===0)return '<span class="chip" style="color:var(--red);border-color:var(--red)">Env-off</span>';
    if(e.reason&&e.reason.indexOf("disabled by operator")===0)return '<span class="chip" style="color:var(--amb);border-color:var(--amb)">Off</span>';
    return '<span class="chip">Inactive</span>'}
  function egKv(o,max){
    var ks=Object.keys(o||{}).slice(0,max||6);
    if(!ks.length)return "";
    return '<div class="kv" style="margin-top:6px">'+ks.map(function(k){
      var val=String(o[k]);if(val.length>34)val=val.slice(0,33)+"...";
      return '<div class="it" style="padding:6px 9px"><div class="k" style="font-size:10.5px">'+esc(k)+'</div><div class="v mono" style="font-size:11px">'+esc(val)+"</div></div>"}).join("")+"</div>"}
  function egCard(e){
    return '<div class="card" data-en="'+esc(e.name)+'">'+
      '<div class="row" style="justify-content:space-between;align-items:flex-start;gap:8px"><div><b>'+esc(e.name)+'</b>'+
      '<div class="ftx" style="font-size:11.5px;margin-top:2px">'+esc(e.title||"")+"</div></div>"+egChip(e)+"</div>"+
      (e.reason?'<p class="ftx" style="font-size:11.5px;margin:9px 0 0;border-left:2px solid var(--bd2);padding-left:8px">'+esc(e.reason)+"</p>":"")+
      (Object.keys(e.params||{}).length?'<div style="margin-top:10px"><span class="ftx" style="font-size:10px;letter-spacing:1px">PARAMS</span>'+egKv(e.params,6)+"</div>":"")+
      '<div style="border-top:1px solid var(--bd);margin-top:10px;padding-top:8px"><span class="ftx" style="font-size:10px;letter-spacing:1px">METRICS</span>'+
      (egKv(e.metrics,6)||'<div class="ftx" style="font-size:11.5px;margin-top:5px">no activity yet</div>')+"</div>"+
      '<div class="row" style="gap:6px;margin-top:12px"><button class="btn sm'+(e.active?"":" pri")+'" data-eg="'+(e.active?"disable":"enable")+'">'+(e.active?"Disable":"Enable")+'</button><button class="btn sm" data-eg="logs">Logs</button></div></div>'}
  function egModal(title,body){
    var ov=document.createElement("div");ov.className="qr-ov";
    ov.innerHTML='<div class="qr-c" style="max-width:560px;width:92vw"><b style="font-size:13px">'+esc(title)+'</b>'+
      '<pre style="white-space:pre-wrap;overflow-wrap:anywhere;max-height:56vh;overflow:auto;font-size:11.5px;line-height:1.55;text-align:left;margin:12px 0;color:var(--dim)">'+esc(body||"—")+"</pre>"+
      '<button class="btn sm" id="egx">Close</button></div>';
    document.body.appendChild(ov);
    ov.onclick=function(e){if(e.target===ov)ov.remove()};
    $("#egx",ov).onclick=function(){ov.remove()};
    return ov}
  // Group engines so "why is it not active" is answerable at a glance.
  function egSection(title,sub,es){
    if(!es.length)return "";
    return '<h3 style="margin:18px 0 10px;font-size:13.5px">'+esc(title)+' <span class="ftx" style="font-size:11px;font-weight:400">('+es.length+")</span></h3>"+
      '<p class="ftx" style="font-size:11.5px;margin:-4px 0 10px">'+esc(sub)+"</p>"+
      '<div class="ig">'+es.map(egCard).join("")+"</div>"}
  function egGroups(es){
    var con=[],core=[],off=[];
    es.forEach(function(e){
      var hosts=e.host||[];
      var envOff=e.reason&&e.reason.indexOf("disabled by env")===0;
      var opOff=e.reason&&(e.reason.indexOf("disabled by operator")===0||e.reason.indexOf("off by default")===0);
      if(hosts.indexOf("core")>=0&&hosts.indexOf("console")<0&&!(envOff||opOff))core.push(e);
      else if((envOff||opOff)||(hosts.indexOf("console")<0&&hosts.indexOf("core")<0))off.push(e);
      else con.push(e)});
    return egSection("Runs on this deployment","Active on the panel/gateway process. Toggle freely — your choices survive restarts.",con)+
      egSection("Runs inside each proxy instance (Core)","These activate per running instance — see their live status in the Core-side section below.",core)+
      egSection("Off / needs configuration","Either turned off (press Enable) or waiting for operator configuration — each card shows exactly what it needs.",off)}
  function load(){
    api("GET","/api/engines").then(function(d){
      var es=d.engines||[],act=0;es.forEach(function(e){if(e.active)act++});
      $("#eg-w").innerHTML=d.volume_warning?'<div class="card" style="border-color:var(--amb);margin-bottom:14px"><h3 style="margin:0 0 6px;color:var(--amb)">Engine state is not persisting</h3><p class="mut" style="margin:0;font-size:12.5px">'+esc(d.volume_warning)+"</p></div>":"";
      var po=(d.pipeline_order||[]).join(" → ")||"—";
      $("#eg-s").innerHTML=egSg("Engines active",act+" / "+es.length,act?"var(--grn)":"")+egSg("Pipeline order",po)+
        egSg("Engine data",d.data_dir||"—")+egSg("Uptime",fmtUp(Math.round(d.uptime||0)));
      $("#eg-c").innerHTML=es.length?egGroups(es):'<div class="card"><span class="mut">No engines registered.</span></div>';
      var cs=d.cores||[];
      $("#eg-k").innerHTML='<h3 style="margin:0 0 8px">Core-side engines (per running instance)</h3>'+
        (cs.length?cs.map(function(c){
          var on=(c.engines||[]).filter(function(e){return e.active});
          return '<div style="padding:8px 0;border-top:1px solid var(--bd)"><b>'+esc(c.instance_name||c.instance_id)+"</b> "+
            (on.length?on.map(function(e){return '<span class="chip">'+esc(e.name)+"</span>"}).join(" "):'<span class="ftx" style="font-size:11.5px">none active</span>')+"</div>"}).join("")
        :'<p class="ftx" style="font-size:12px">No running instance has reported core-side engine status yet — embedded cores report through the worker proxy.</p>');
      var cEl=$("#eg-c");
      Array.prototype.forEach.call(cEl.querySelectorAll("[data-eg]"),function(btn){
        btn.onclick=function(){
          var name=btn.closest("[data-en]").dataset.en,a=btn.dataset.eg;
          if(a==="logs"){
            api("GET","/api/engines/logs?name="+encodeURIComponent(name)).then(function(r){
              var ls=r.logs||[];
              if(!ls.length){toast("No engine logs yet","ok");return}
              egModal(name+" — recent log",ls.slice(-60).join("\n"))})
            .catch(function(e){toast(e.message,"err")});
            return}
          btn.disabled=true;
          api("POST","/api/engines/"+encodeURIComponent(name)+"/"+a).then(function(r){
            toast(r&&r.ok?name+" "+(a==="enable"?"enabled":"disabled"):((r&&r.message)||"done"),"ok");load()})
          .catch(function(e){btn.disabled=false;toast(e.message,"err")})}});
    }).catch(function(e){
      var msg=String(e.message||"");
      $("#eg-s").innerHTML="";
      $("#eg-c").innerHTML='<div class="card"><b>Engines unavailable</b><p class="mut" style="font-size:12.5px">'+
        (msg.indexOf("404")>=0?"The engines layer is disabled on this deployment (EMUNEL_ENGINES_ENABLED=0). Remove that variable and redeploy to get this page back.":esc(msg))+"</p></div>";
      $("#eg-k").innerHTML=""});
  }
  $("#eg-rf").onclick=load;
  $("#eg-st").onclick=function(){
    var b=$("#eg-st");b.disabled=true;
    api("POST","/api/engines/selftest").then(function(r){
      b.disabled=false;
      egModal("Engine selftest — "+(r&&r.all_ok?"all checks passed":"review results"),JSON.stringify(r,null,2))})
    .catch(function(e){b.disabled=false;toast(e.message,"err")})};
  load();
  pollTimer=poll(20000,function(){return load()});
}
// ───────────────────────────── bypass (SNI + REALITY) ─────────────────────────────
function viewBypass(){
  shell("bypass");
  var v=$("#view");
  v.innerHTML='<div class="ph"><div><h1>Bypass</h1>'+
    '<div class="sub">Iran-bypass tooling — SNI Spoofing runs on client devices through the downloadable helper; REALITY keys and configs are generated here and can run on a pinned Xray runtime. The proxy Core is never modified.</div></div>'+
    '<div class="ha"><button class="btn sm pri" id="bp-rf">Refresh</button></div></div>'+
    '<div id="bp-w"></div><div class="sgs" id="bp-s"></div>'+
    '<div class="card" id="bp-sni"></div><div class="card" style="margin-top:14px" id="bp-rea"></div>'+
    '<div class="card" style="margin-top:14px" id="bp-run"></div>';
  function bpSg(l,val,c){var s=String(val);
    return '<div class="sg"><div class="l">'+l+'</div><div class="v" style="font-size:'+(s.length>18?"13px":"18px")+';'+(c?"color:"+c:"")+'">'+esc(s)+"</div></div>"}
  function bpChip(ok,label){return '<span class="chip" style="color:'+(ok?"var(--grn)":"var(--fnt)")+';border-color:'+(ok?"var(--grn)":"var(--bd2)")+'">'+(label||(ok?"Active":"Inactive"))+"</span>"}
  function bpModal(title,body,mono){
    var ov=document.createElement("div");ov.className="qr-ov";
    ov.innerHTML='<div class="qr-c" style="max-width:620px;width:92vw"><b style="font-size:13px">'+esc(title)+'</b>'+
      '<pre style="white-space:pre-wrap;overflow-wrap:anywhere;max-height:56vh;overflow:auto;font-size:11.5px;line-height:1.55;text-align:left;margin:12px 0;color:var(--dim)'+(mono===false?"":"")+'">'+esc(body||"—")+"</pre>"+
      '<div class="row" style="gap:6px"><button class="btn sm" id="bpc">Copy</button><button class="btn sm pri" id="bpx">Close</button></div></div>';
    document.body.appendChild(ov);
    ov.onclick=function(e){if(e.target===ov)ov.remove()};
    $("#bpx",ov).onclick=function(){ov.remove()};
    $("#bpc",ov).onclick=function(){
      try{navigator.clipboard.writeText(body||"");toast("Copied","ok")}catch(e){toast("Copy failed","err")}};
    return ov}
  function loadSni(){
    api("GET","/api/engines/sni/status").then(function(d){
      var p=d.profile||{};
      $("#bp-sni").innerHTML='<div class="row" style="justify-content:space-between;align-items:flex-start"><div><h3 style="margin:0">SNI Spoofing — client-side bypass profile</h3>'+
        '<p class="ftx" style="font-size:11.5px;margin:4px 0 0">Spoofing executes on the user device: the panel generates the profile and serves the helper script. Point the client at 127.0.0.1:'+esc(p.listen_port||40443)+'.</p></div>'+bpChip(true,"Generator")+"</div>"+
        '<div class="kv" style="margin-top:12px">'+
        '<div class="it"><div class="k">Method</div><div class="v mono">'+esc(p.method||"—")+'</div></div>'+
        '<div class="it"><div class="k">Fragment strategy</div><div class="v mono">'+esc(p.fragment_strategy||"—")+'</div></div>'+
        '<div class="it"><div class="k">Delay</div><div class="v mono">'+esc(p.fragment_delay||0)+'s</div></div>'+
        '<div class="it"><div class="k">TTL trick</div><div class="v mono">'+(p.ttl_trick?("ttl="+esc(p.ttl_value)):"off")+'</div></div>'+
        '<div class="it"><div class="k">Fake SNI</div><div class="v mono">'+esc(p.fake_sni||"—")+'</div></div>'+
        '<div class="it"><div class="k">SNI pool</div><div class="v mono">'+((p.sni_pool||[]).length+" hosts")+'</div></div></div>'+
        '<details style="margin-top:10px"><summary class="ftx" style="font-size:11.5px;cursor:pointer">Edit profile</summary>'+
        '<div class="kv" style="margin-top:8px">'+
        '<div class="it"><div class="k">Method</div><select id="bp-m" class="inp" style="width:100%">'+["fragment","fake_sni","combined"].map(function(m){return '<option '+(m===p.method?"selected":"")+'>'+m+"</option>"}).join("")+"</select></div>"+
        '<div class="it"><div class="k">Strategy</div><select id="bp-st" class="inp" style="width:100%">'+["sni_split","half","multi","tls_record_frag"].map(function(m){return '<option '+(m===p.fragment_strategy?"selected":"")+'>'+m+"</option>"}).join("")+"</select></div>"+
        '<div class="it"><div class="k">Delay (s)</div><input id="bp-d" class="inp mono" style="width:100%" value="'+esc(p.fragment_delay||0)+'"></div>'+
        '<div class="it"><div class="k">TTL (0-8, 0=off)</div><input id="bp-t" class="inp mono" style="width:100%" value="'+esc(p.ttl_value||1)+'"></div>'+
        '<div class="it" style="grid-column:1/-1"><div class="k">Fake SNI</div><input id="bp-f" class="inp mono" style="width:100%" value="'+esc(p.fake_sni||"")+'"></div>'+
        '<div class="it" style="grid-column:1/-1"><div class="k">SNI pool (comma separated)</div><input id="bp-p" class="inp mono" style="width:100%" value="'+esc((p.sni_pool||[]).join(","))+'"></div></div>'+
        '<div class="row" style="gap:6px;margin-top:10px"><button class="btn sm pri" id="bp-save">Save profile</button><button class="btn sm" id="bp-test">Run test</button><button class="btn sm" id="bp-dl">Download helper</button><button class="btn sm" id="bp-cmd">Show usage</button></div></details>';
      $("#bp-save").onclick=function(){
        var b=$("#bp-save");b.disabled=true;
        api("POST","/api/engines/sni/config",{
          method:$("#bp-m").value,strategy:$("#bp-st").value,
          delay:parseFloat($("#bp-d").value)||0,
          ttl_value:parseInt($("#bp-t").value,10)||0,
          fake_sni:$("#bp-f").value,
          sni_pool:$("#bp-p").value
        }).then(function(r){b.disabled=false;toast("Profile saved","ok");loadSni()})
        .catch(function(e){b.disabled=false;toast(e.message,"err")})};
      $("#bp-test").onclick=function(){
        var b=$("#bp-test");b.disabled=true;
        api("POST","/api/engines/sni/test").then(function(r){
          b.disabled=false;
          bpModal("SNI bypass plan test — "+(r.ok?"valid":"INVALID"),JSON.stringify(r,null,2))})
        .catch(function(e){b.disabled=false;toast(e.message,"err")})};
      $("#bp-dl").onclick=function(){
        fetch("/api/engines/sni/helper?download=1",{credentials:"same-origin"})
          .then(function(r){if(!r.ok)throw new Error("download failed ("+r.status+")");return r.text()})
          .then(function(text){
            var blob=new Blob([text],{type:"text/x-python"}),a=document.createElement("a");
            a.href=URL.createObjectURL(blob);a.download="emunel_sni_helper.py";a.click();
            setTimeout(function(){URL.revokeObjectURL(a.href)},4000);
            toast("Helper downloaded — run it next to your client","ok")})
          .catch(function(e){toast(e.message,"err")})};
      $("#bp-cmd").onclick=function(){
        api("GET","/api/engines/sni/status").then(function(d2){
          bpModal("Helper usage (client device)","python3 emunel_sni_helper.py "+d2.helper_usage.replace(/^python\S*\s*/,"")+
            "\n\nthen point your browser / proxy client at 127.0.0.1:"+(d2.profile||{}).listen_port)})};
    }).catch(function(e){
      $("#bp-sni").innerHTML='<h3 style="margin:0 0 6px">SNI Spoofing</h3><p class="mut" style="font-size:12.5px">'+esc(e.message)+"</p>"});
  }
  function loadReality(){
    api("GET","/api/engines/reality/status").then(function(d){
      var p=d.profile||{},rt=d.runtime||{};
      $("#bp-rea").innerHTML='<div class="row" style="justify-content:space-between;align-items:flex-start"><div><h3 style="margin:0">REALITY — TLS camouflage</h3>'+
        '<p class="ftx" style="font-size:11.5px;margin:4px 0 0">The client borrows a real target handshake (e.g. blubank.com); the server proves itself with an X25519 keypair. Keys and configs are generated here.</p></div>'+
        (d.keypair_present?bpChip(true,"Keys ready"):bpChip(false,"No keypair"))+"</div>"+
        '<div class="kv" style="margin-top:12px">'+
        '<div class="it"><div class="k">Target</div><div class="v mono">'+esc(p.target||"—")+'</div></div>'+
        '<div class="it"><div class="k">XHTTP target</div><div class="v mono">'+esc(p.xhttp_target||"—")+'</div></div>'+
        '<div class="it"><div class="k">Server names</div><div class="v mono" style="font-size:10.5px">'+esc((p.server_names||[]).join(", ")||"—")+'</div></div>'+
        '<div class="it"><div class="k">Fingerprint</div><div class="v mono">'+esc(p.fingerprint||"—")+'</div></div>'+
        '<div class="it"><div class="k">Listen port</div><div class="v mono">'+esc(p.listen_port||"—")+'</div></div>'+
        '<div class="it" style="grid-column:1/-1"><div class="k">Public key (pbk)</div><div class="v mono" style="font-size:10.5px;overflow-wrap:anywhere">'+esc(d.public_key||"—")+"</div></div></div>"+
        '<details style="margin-top:10px"><summary class="ftx" style="font-size:11.5px;cursor:pointer">Edit profile</summary>'+
        '<div class="kv" style="margin-top:8px">'+
        '<div class="it"><div class="k">Target (host:port)</div><input id="bp-rt" class="inp mono" style="width:100%" value="'+esc(p.target||"")+'"></div>'+
        '<div class="it"><div class="k">XHTTP target</div><input id="bp-rx" class="inp mono" style="width:100%" value="'+esc(p.xhttp_target||"")+'"></div>'+
        '<div class="it" style="grid-column:1/-1"><div class="k">Server names (comma separated)</div><input id="bp-rn" class="inp mono" style="width:100%" value="'+esc((p.server_names||[]).join(","))+'"></div></div>'+
        '<div class="row" style="gap:6px;margin-top:10px"><button class="btn sm pri" id="bp-rsave">Save profile</button><button class="btn sm dng" id="bp-keys">Generate new keypair</button>'+
        '<span class="ftx" style="font-size:11px;align-self:center">Iran tip: prefer domestic heavy-traffic targets (banks, marketplaces); avoid google/microsoft.</span></div></details>'+
        '<div class="row" style="gap:6px;margin-top:12px"><span class="ftx" style="font-size:10px;letter-spacing:1px;align-self:center">GENERATE CLIENT CONFIG</span>'+
        ["raw","xhttp","grpc"].map(function(t){return '<button class="btn sm" data-gen="'+t+'">'+t.toUpperCase()+"</button>"}).join("")+"</div>";
      $("#bp-rsave").onclick=function(){
        var b=$("#bp-rsave");b.disabled=true;
        api("POST","/api/engines/reality/config",{
          target:$("#bp-rt").value,xhttp_target:$("#bp-rx").value,
          server_names:$("#bp-rn").value
        }).then(function(r){b.disabled=false;toast("REALITY profile saved","ok");loadReality()})
        .catch(function(e){b.disabled=false;toast(e.message,"err")})};
      $("#bp-keys").onclick=function(){
        var b=$("#bp-keys");b.disabled=true;
        api("POST","/api/engines/reality/keys").then(function(r){
          b.disabled=false;loadReality();
          bpModal("New X25519 keypair — private key shown ONCE",
            "private_key: "+r.private_key+"\npublic_key:  "+r.public_key+"\nclient_uuid: "+r.client_uuid+
            "\n\nStore the private key safely (env REALITY_PRIVATE_KEY or the engine state file). "+
            "Existing clients must re-import the new public key.")})
        .catch(function(e){b.disabled=false;toast(e.message,"err")})};
      Array.prototype.forEach.call($("#bp-rea").querySelectorAll("[data-gen]"),function(btn){
        btn.onclick=function(){
          btn.disabled=true;
          api("POST","/api/engines/reality/generate",{transport:btn.dataset.gen}).then(function(r){
            btn.disabled=false;
            bpModal("REALITY "+r.transport.toUpperCase()+" — client import",
              r.share_url+"\n\n—— client outbound JSON ——\n"+JSON.stringify(r.outbound,null,2)+
              "\n\n—— server inbound JSON (run on your Xray) ——\n"+JSON.stringify(r.inbound,null,2))})
          .catch(function(e){btn.disabled=false;toast(e.message,"err")})}});
      var runCard=$("#bp-run");
      if(rt.configured){
        runCard.innerHTML='<div class="row" style="justify-content:space-between;align-items:flex-start"><div><h3 style="margin:0">REALITY runtime (pinned Xray)</h3>'+
          '<p class="ftx" style="font-size:11.5px;margin:4px 0 0">listener '+esc(rt.listen||"—")+' — expose it with a Railway TCP Proxy pointing at this port.</p></div>'+
          (rt.running?bpChip(true,"Running"):bpChip(false,"Stopped"))+"</div>"+
          '<div class="row" style="gap:6px;margin-top:12px"><button class="btn sm pri" id="bp-rr">'+(rt.running?"Restart runtime":"Start runtime")+'</button></div>';
        $("#bp-rr").onclick=function(){
          var b=$("#bp-rr");b.disabled=true;
          api("POST","/api/engines/reality/restart").then(function(r){
            b.disabled=false;toast(r.runtime&&r.runtime.running?"Runtime started":"Runtime not running — check engine logs","ok");loadReality()})
          .catch(function(e){b.disabled=false;toast(e.message,"err")})};
      }else{
        runCard.innerHTML='<h3 style="margin:0 0 6px">REALITY runtime — not configured</h3>'+
          '<p class="mut" style="font-size:12.5px;margin:0">Key and config generation work everywhere. To also RUN the VLESS+REALITY listener inside this deployment: install an Xray release, set <span class="mono">EMUNEL_XRAY_BINARY</span> (absolute path) + <span class="emunel mono">EMUNEL_XRAY_SHA256</span> (digest pin), expose <span class="mono">EMUNEL_REALITY_LISTEN_PORT</span> through a Railway TCP Proxy — full guide in <span class="mono">docs/RAILWAY.md</span>. Until then the buttons above generate everything you need for a self-hosted Xray.</p>';
      }
    }).catch(function(e){
      $("#bp-rea").innerHTML='<h3 style="margin:0 0 6px">REALITY</h3><p class="mut" style="font-size:12.5px">'+esc(e.message)+"</p>";
      $("#bp-run").innerHTML="";});
  }
  function loadHead(){
    api("GET","/api/engines").then(function(d){
      $("#bp-w").innerHTML=d.volume_warning?'<div class="card" style="border-color:var(--amb);margin-bottom:14px"><h3 style="margin:0 0 6px;color:var(--amb)">Engine state is not persisting</h3><p class="mut" style="margin:0;font-size:12.5px">'+esc(d.volume_warning)+"</p></div>":"";
      var act=(d.engines||[]).filter(function(e){return /sni|reality/i.test(e.name)&&e.active});
      $("#bp-s").innerHTML=bpSg("Bypass engines",act.length+" / 2",act.length?"var(--grn)":"")+
        bpSg("Pipeline",((d.pipeline_order||[]).length)+" engines")+
        bpSg("Engine data",d.data_dir||"—")+bpSg("Uptime",fmtUp(Math.round(d.uptime||0)));
    }).catch(function(){$("#bp-s").innerHTML=""});
  }
  $("#bp-rf").onclick=function(){loadHead();loadSni();loadReality()};
  loadHead();loadSni();loadReality();
  pollTimer=poll(20000,function(){loadHead();return loadReality()});
}
// ───────────────────────────── boot ─────────────────────────────
function render(){
  api("GET","/auth/me").then(function(me){
    if(!me.authenticated){viewLogin();return}
    USER=me.user;CSRF=me.csrf_token;
    if(me.links){LINKS.github=me.links.github||LINKS.github;LINKS.telegram=me.links.telegram||""}
    viewDash();
  }).catch(function(e){
    $("#app").innerHTML='<div class="lw"><div class="lc"><div class="card"><b>EMUNEL Console failed to load</b><p class="mut">'+esc(e.message)+"</p></div></div></div>";
  });
}
render();
})();
</script>
</body>
</html>
"""

def _build_stamp() -> str:
    """Deployment build identifier baked into the served page."""
    import re

    from .version import build as _build

    return re.sub(r"[^A-Za-z0-9 .:+_-]", "", _build())[:48] or "dev"


PAGE = PAGE.replace("__EMUNEL_BUILD__", _build_stamp())

router.add_api_route("/panel", lambda: HTMLResponse(PAGE), methods=["GET"], include_in_schema=False)
