"""Password-protected dashboard: a static HTML page whose data is AES-256-GCM encrypted (PBKDF2-SHA256, 600k rounds).
Without the password the page contains only ciphertext. Use a long passphrase (5+ random words)."""
from __future__ import annotations

import base64
import json
import os
from datetime import timedelta

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from . import editorial as ed
from .util import iso

ITER = 600_000


def build_data(conn, cfg, now) -> dict:
    since = iso(now - timedelta(days=14))
    stories = [dict(r) for r in conn.execute(
        "SELECT id,headline,source_org,original_url,primary_url,published_at,detected_at,category,score,verification_status,"
        "editorial_status,publication_status,evidence_ok,lead_only,package_path,last_item_at,breaking,breaking_until,article_id,video_id FROM stories "
        "WHERE last_item_at>=? OR editorial_status IN ('verifying','ready','approved') ORDER BY score DESC LIMIT 400", (since,))]
    src = [dict(r) for r in conn.execute("SELECT * FROM source_health ORDER BY consecutive_failures DESC, source_id")]
    runs = [dict(r) for r in conn.execute(
        "SELECT bot, MAX(CASE WHEN status IN ('ok','partial') THEN finished_at END) last_success, "
        "MAX(finished_at) last_run, COUNT(*) total FROM runs GROUP BY bot")]
    recent = [dict(r) for r in conn.execute("SELECT bot,status,finished_at,items_seen,items_new,detail,env FROM runs ORDER BY id DESC LIMIT 25")]
    month = iso(now)[:7]
    usage = [dict(r) for r in conn.execute("SELECT service, SUM(CASE WHEN day=? THEN calls ELSE 0 END) today, SUM(calls) month "
                                           "FROM api_usage WHERE day LIKE ? GROUP BY service", (iso(now)[:10], month + "%"))]
    costs = cfg["settings"]["infrastructure_costs"]
    for s in stories:
        s["uid"] = ed.uid(s["id"])
        s["blocked"] = conn.execute("SELECT COUNT(*) c FROM claims WHERE story_id=? AND status='UNVERIFIED'", (s["id"],)).fetchone()["c"]
        vs = ed.latest_versions(conn, s["id"])
        s["drafts"] = ", ".join(f"{v['kind']} v{v['version']}{'*' if ed._is_approved(conn, v) else ''}" for v in vs)
    rows = lambda q, *a: [dict(r) for r in conn.execute(q, a)]  # noqa: E731
    editorial = {
        "leads": rows("SELECT * FROM leads ORDER BY id DESC LIMIT 200"),
        "corrections": rows("SELECT c.*, s.headline FROM corrections c JOIN stories s ON s.id=c.story_id ORDER BY c.id DESC LIMIT 200"),
        "approvals": rows("SELECT a.*, s.headline FROM approvals a JOIN stories s ON s.id=a.story_id ORDER BY a.id DESC LIMIT 300"),
        "live": rows("SELECT l.*, s.headline FROM live_coverage l JOIN stories s ON s.id=l.story_id ORDER BY l.id DESC"),
        "live_updates": rows("SELECT * FROM live_updates ORDER BY id DESC LIMIT 300"),
        "evidence": ed.evidence_rows(conn)[:1500],
        "latency": ed.latency_metrics(conn),
        "links": rows("SELECT * FROM story_links ORDER BY id DESC LIMIT 300"),
    }
    from . import cards as cd
    card_rows = conn.execute("SELECT * FROM stories WHERE lead_only=0 AND (last_item_at>=? OR editorial_status IN ('verifying','ready','approved')) "
                             "ORDER BY score DESC LIMIT 150", (since,)).fetchall()
    cards = [cd.card(conn, r, now) for r in card_rows]
    top5 = [c["id"] for c in cd.top_stories(conn, now)]
    return {"cards": cards, "top5": top5, "editorial": editorial, "bot_note": "Audience and commercial data are not connected yet (needs YouTube, Search Console and Shopify access).", "generated_at": iso(now), "threshold": cfg["settings"]["alert_threshold"], "stories": stories, "sources": src,
            "runs": runs, "recent_runs": recent, "usage": usage, "limits": cfg["settings"]["limits"],
            "costs": costs, "cost_total": round(sum(costs.values()), 2)}


def encrypt(payload: dict, password: str, iterations: int = ITER) -> str:
    salt, iv = os.urandom(16), os.urandom(12)
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations).derive(password.encode())
    ct = AESGCM(key).encrypt(iv, json.dumps(payload).encode(), None)
    return base64.b64encode(salt + iv + ct).decode()


TEMPLATE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="robots" content="noindex,nofollow">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>The Post-Mortem Newsroom</title>
<style>
:root{--ink:#0B0B0C;--paper:#F4F0E8;--red:#B51F2A;--stone:#A59B8C}
*{box-sizing:border-box}body{font-family:Georgia,"Source Serif 4",serif;margin:0;background:var(--paper);color:var(--ink);line-height:1.45}
header{padding:14px 20px;border-bottom:2px solid var(--ink)}h1{font-size:19px;margin:0;letter-spacing:.08em}h1 small{font:12px system-ui;letter-spacing:0;color:#555;margin-left:8px}
main{max-width:1240px;margin:0 auto;padding:14px 18px 60px}h2{font-size:15px;letter-spacing:.06em;text-transform:uppercase;border-bottom:1px solid var(--stone);padding-bottom:4px;margin:22px 0 10px}
h2 small{font:12px system-ui;text-transform:none;letter-spacing:0;color:#666}
input,button,select{font:15px system-ui;padding:7px 9px;border:1px solid var(--stone);background:#fff;color:var(--ink);border-radius:2px}
button{cursor:pointer}button.on{background:var(--ink);color:var(--paper);border-color:var(--ink)}
.card{background:#fff;border:1px solid var(--stone);border-left:4px solid var(--red);padding:11px 14px;margin:0 0 12px}
.card.small{padding:9px 11px;font-size:14px}.ch{display:flex;align-items:flex-start;gap:8px}
.score{background:var(--red);color:#fff;font:600 13px system-ui;padding:2px 7px;border-radius:2px;min-width:30px;text-align:center}
.hl{font-weight:700;font-size:17px;color:var(--ink);text-decoration:none;border-bottom:1px solid var(--stone)}.small .hl{font-size:15px}
.sum,.why,.hook,.meta,.cmds,.pills{margin:6px 0 0}.meta{font:13px system-ui;color:#555}.pills .pill,.pill{font:12px system-ui;border:1px solid var(--stone);padding:1px 8px;border-radius:10px;margin-right:4px;color:#333;display:inline-block}
.pill.ok{background:var(--ink);border-color:var(--ink);color:var(--paper)}.pill.warn{border-color:var(--red);color:var(--red)}
code{font:13px ui-monospace,Consolas,monospace;background:var(--paper);border:1px solid var(--stone);padding:1px 6px;cursor:pointer;border-radius:2px;white-space:nowrap}code.copied{background:var(--ink);color:var(--paper)}
.cols{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}@media(max-width:900px){.cols{grid-template-columns:1fr}}
.filters{display:flex;gap:10px;flex-wrap:wrap;align-items:center;font:14px system-ui;margin:10px 0}.empty{color:#666;font:14px system-ui;padding:8px 0}
details{margin-top:26px;border-top:2px solid var(--ink);padding-top:8px}summary{font:600 14px system-ui;letter-spacing:.06em;text-transform:uppercase;cursor:pointer;padding:6px 0}
nav button{margin:2px 3px 2px 0}table{border-collapse:collapse;width:100%;font:13px system-ui;background:#fff}td,th{border-bottom:1px solid #e3ddd0;padding:6px;text-align:left;vertical-align:top}
a{color:var(--ink)}.bad{color:var(--red)}small{color:#666}
.how{background:#fff;border:1px solid var(--ink);padding:6px 18px 12px;margin:10px 0}.how h2{border:0;margin:10px 0 4px}.how ol{margin:0;padding-left:20px;font:15px/1.5 system-ui}
.ticks{font:13px system-ui;margin:6px 0 0}.ticks span{margin-right:10px;white-space:nowrap}.miss{font:13px system-ui;color:var(--red);margin:4px 0 0}
.card.brk{border-left-color:#000;background:#fff6f6}.card.live{border-left-color:#222}
</style></head><body>
<header><h1>THE POST-MORTEM <small>Newsroom review desk. Nothing here publishes anything.</small></h1></header>
<main><div id="lock"><p>Enter dashboard password</p><input id="pw" type="password" autocomplete="current-password"> <button id="go">Unlock</button><p id="err" class="bad"></p></div>
<div id="app" hidden><p><small id="meta"></small></p>
<section class="how" aria-labelledby="howh"><h2 id="howh">How this works</h2><ol>
<li>The robot reads news feeds all day and gives every story a score.</li>
<li>The best stories wait in <b>To review</b> below, and arrive as cards in Telegram.</li>
<li>Each card has five yes/no checks. <b>Approve</b> only works when all five are yes.</li>
<li>Tap <b>Approve</b> in Telegram: the story and its sources reach the website in about 5 minutes. You can undo for 10 minutes.</li>
<li><b>Breaking</b> shows a red banner on the site. <b>Live</b> shows updates you approve one by one. Nothing goes out without your tap.</li></ol></section>
<h2>Today <small>one screen: what needs you, what is breaking, what is live</small></h2>
<div class="cols" id="today"><section><h2>To review</h2><div id="tdReview"></div></section>
<section><h2>Breaking</h2><div id="tdBreaking"></div></section>
<section><h2>Live</h2><div id="tdLive"></div></section></div>
<details id="full"><summary>Full list (top 5, filters, all stages)</summary>
<h2>Top 5 today <small>highest score in the last 24 hours</small></h2><div id="top5"></div>
<div class="filters" id="filters"></div>
<div class="cols"><section><h2>New and worth a look</h2><div id="colNew"></div></section>
<section><h2>Needs your review</h2><div id="colReview"></div></section>
<section><h2>Ready to approve</h2><div id="colReady"></div></section></div></details>
<details id="more"><summary>More</summary><nav id="tabs"></nav><div id="view"></div></details></div></main>
<script>
const B64="__BLOB__",ITER=__ITER__;
const $=id=>document.getElementById(id);
function td(t,h){const c=document.createElement(h?'th':'td');if(t instanceof Node)c.appendChild(t);else c.textContent=t==null?'':String(t);return c}
function link(u,t){if(!/^https?:\\/\\//i.test(u||''))return document.createTextNode(t||'');const a=document.createElement('a');a.href=u;a.target='_blank';a.rel='noopener noreferrer';a.textContent=t||u;return a}
async function unlock(){try{const raw=Uint8Array.from(atob(B64),c=>c.charCodeAt(0));const salt=raw.slice(0,16),iv=raw.slice(16,28),ct=raw.slice(28);
const km=await crypto.subtle.importKey('raw',new TextEncoder().encode($('pw').value),'PBKDF2',false,['deriveKey']);
const key=await crypto.subtle.deriveKey({name:'PBKDF2',salt,iterations:ITER,hash:'SHA-256'},km,{name:'AES-GCM',length:256},false,['decrypt']);
const pt=await crypto.subtle.decrypt({name:'AES-GCM',iv},key,ct);render(JSON.parse(new TextDecoder().decode(pt)))}catch(e){$('err').textContent='Wrong password or corrupted data.'}}
function el(tag,cls,txt){const e=document.createElement(tag);if(cls)e.className=cls;if(txt!=null)e.textContent=txt;return e}
function cmd(x){const c=el('code',null,x);c.title='Click to copy';c.onclick=()=>{try{navigator.clipboard.writeText(x)}catch(e){}c.classList.add('copied')};return c}
function secPill(s){if(!s||s.overall==='NOT RUN')return el('span','pill','2nd check: not run');
return el('span','pill '+(s.overall==='PASS'?'ok':'warn'),'2nd check: '+s.overall+(s.fail+s.unclear?' ('+(s.fail+s.unclear)+' open)':''))}
function pills(c){const p=el('p','pills');p.appendChild(el('span','pill',c.is_primary?'Official source':'News report'));p.appendChild(secPill(c.second));p.appendChild(el('span','pill',c.status_word));
if(c.alert)p.appendChild(el('span','pill warn','Priority 80+'));return p}
function cmds(list){const p=el('p','cmds');p.appendChild(document.createTextNode('Telegram: '));list.forEach(x=>{p.appendChild(cmd(x));p.appendChild(document.createTextNode(' '))});return p}
function bigCard(c){const d=el('article','card');const h=el('div','ch');h.appendChild(el('span','score',c.score));const a=link(c.link,c.headline);a.className='hl';h.appendChild(a);d.appendChild(h);
d.appendChild(el('p','sum',c.summary));const w=el('p','why');w.appendChild(el('b',null,'Why it matters: '));w.appendChild(document.createTextNode(c.why));d.appendChild(w);
d.appendChild(el('p','meta',c.age+' | '+c.category+' | '+c.verification));d.appendChild(pills(c));
const hk=el('p','hook');hk.appendChild(el('b',null,'Suggested hook: '));hk.appendChild(document.createTextNode(c.hook));d.appendChild(hk);
d.appendChild(cmds([c.cmd_open,c.cmd_second,c.cmd_approve]));return d}
function smallCard(c,next){const d=el('article','card small');const h=el('div','ch');h.appendChild(el('span','score',c.score));const a=link(c.link,c.headline);a.className='hl';h.appendChild(a);d.appendChild(h);
d.appendChild(el('p','meta',c.age+' | '+c.category));d.appendChild(pills(c));d.appendChild(cmds(next));return d}
function fill(id,list,mk){const box=$(id);box.replaceChildren();if(!list.length){box.appendChild(el('p','empty','Nothing here right now.'));return}list.slice(0,12).forEach(c=>box.appendChild(mk(c)))}
function ticks(c){const p=el('p','ticks');const L={sources:'Sources',facts:'Facts',dates:'Dates',second:'2nd check',links:'Links'};
Object.keys(L).forEach(k=>p.appendChild(el('span',null,(c.checks[k]?'\\u2705 ':'\\u274C ')+L[k])));return p}
function todayCard(c,cls){const d=el('article','card small '+(cls||''));const h=el('div','ch');h.appendChild(el('span','score',c.score));const a=link(c.link,c.headline);a.className='hl';h.appendChild(a);d.appendChild(h);
d.appendChild(el('p','meta',c.uid+' | '+c.age+' | '+c.category));d.appendChild(ticks(c));
d.appendChild(el('p',c.can_approve?'meta':'miss',c.can_approve?'All five checks are yes. Tap Approve in Telegram.':'Missing: '+c.checks_missing));return d}
function renderToday(D){const open=D.cards.filter(c=>['new','verifying','ready'].includes(c.status)&&c.score>=45);
fill('tdReview',open.sort((a,b)=>(b.can_approve-a.can_approve)||b.score-a.score),c=>todayCard(c));
fill('tdBreaking',D.cards.filter(c=>c.breaking),c=>todayCard(c,'brk'));
const live=D.editorial.live.filter(l=>l.state!=='CONCLUDED'),box=$('tdLive');box.replaceChildren();
if(!live.length)box.appendChild(el('p','empty','Nothing here right now.'));
live.forEach(l=>{const ups=D.editorial.live_updates.filter(u=>u.coverage_id===l.id);const ok=ups.filter(u=>u.status==='approved'&&u.approved_hash===u.content_hash).length;const dr=ups.filter(u=>u.status==='draft').length;
const d=el('article','card small live');d.appendChild(el('span','hl',l.title));d.appendChild(el('p','meta',l.story_id+' | '+l.state));
d.appendChild(el('p','meta',ok+' update(s) approved and public | '+dr+' waiting for your tap in Telegram'));box.appendChild(d)})}
function renderHome(D){renderToday(D);const byId={};D.cards.forEach(c=>byId[c.id]=c);const F=$('filters');
const cats=[...new Set(D.cards.map(c=>c.category))].sort();
function sel(label,opts){const l=el('label',null,label+' ');const s=el('select');opts.forEach(o=>{const x=el('option',null,o[1]);x.value=o[0];s.appendChild(x)});s.onchange=draw;l.appendChild(s);F.appendChild(l);return s}
const fc=sel('Category',[['','All'],...cats.map(x=>[x,x])]),fs=sel('Score',[['0','Any'],['40','40+'],['60','60+'],['80','80+ (alerts)']]),
fst=sel('Status',[['','All'],['new','New'],['verifying','Needs review'],['ready','Ready to approve'],['approved','Approved']]);
function ok(c){return(!fc.value||c.category===fc.value)&&c.score>=+fs.value&&(!fst.value||c.status===fst.value)}
function draw(){const all=D.cards.filter(ok);
fill('top5',D.top5.map(i=>byId[i]).filter(c=>c&&ok(c)),bigCard);
fill('colNew',all.filter(c=>c.status==='new'&&c.score>=45),c=>smallCard(c,[c.cmd_open,c.cmd_second]));
fill('colReview',all.filter(c=>c.status==='verifying'||(c.status==='ready'&&c.second.overall!=='PASS')),c=>smallCard(c,[c.cmd_open,c.cmd_second]));
fill('colReady',all.filter(c=>c.status==='ready'&&c.second.overall==='PASS'),c=>smallCard(c,[c.cmd_approve,c.cmd_open]))}
draw()}
function table(cols,rows){const t=document.createElement('table');const h=t.insertRow();cols.forEach(c=>h.appendChild(td(c[0],1)));
rows.forEach(r=>{const tr=t.insertRow();cols.forEach(c=>tr.appendChild(td(c[1](r))))});return t}
function render(D){$('lock').hidden=true;$('app').hidden=false;$('meta').textContent='Updated '+D.generated_at+' (UTC). Refreshes about every 30 min.';
const day=Date.parse(D.generated_at)-864e5;const st=D.stories.filter(s=>!s.lead_only);
const sc=[['Score',s=>s.score],['Headline',s=>link(s.primary_url||s.original_url,s.headline)],['Org',s=>s.source_org],['Category',s=>s.category],
['PM ID',s=>s.uid],['Published',s=>s.published_at||'unknown'],['Detected',s=>s.detected_at],['Verification',s=>s.verification_status],['Editorial',s=>s.editorial_status],['#',s=>s.id]];
const tabs={'Breaking':()=>table(sc,st.filter(s=>s.score>=D.threshold&&s.evidence_ok&&Date.parse(s.last_item_at)>=day&&['new','verifying'].includes(s.editorial_status))),
'New':()=>table(sc,st.filter(s=>s.editorial_status==='new')),'Under verification':()=>table(sc,st.filter(s=>s.editorial_status==='verifying')),
'Ready for approval':()=>table(sc,st.filter(s=>s.editorial_status==='ready')),'Approved':()=>table(sc,st.filter(s=>s.editorial_status==='approved')),
'Published':()=>table(sc,st.filter(s=>s.editorial_status==='published')),'Rejected':()=>table(sc,st.filter(s=>s.editorial_status==='rejected')),
'Trending leads':()=>table([['Lead',s=>s.headline],['Detected',s=>s.detected_at],['Link',s=>link(s.original_url,'open')]],D.stories.filter(s=>s.lead_only)),
'Bot health':()=>{const d=document.createElement('div');d.appendChild(table([['Bot',r=>r.bot],['Last success',r=>r.last_success||'never'],['Last run',r=>r.last_run],['Runs',r=>r.total]],D.runs));
d.appendChild(document.createElement('hr'));d.appendChild(table([['Source',r=>r.source_id],['Last success',r=>r.last_success||'never'],['Failures in a row',r=>r.consecutive_failures],['Last error',r=>r.last_error||'']],D.sources));
d.appendChild(document.createElement('hr'));d.appendChild(table([['Bot',r=>r.bot],['Status',r=>r.status],['Finished',r=>r.finished_at],['Seen',r=>r.items_seen],['New',r=>r.items_new],['Env',r=>r.env]],D.recent_runs));return d},
'API usage':()=>table([['Service',r=>r.service],['Today',r=>r.today],['This month',r=>r.month],['Daily limit',r=>({telegram:D.limits.telegram_per_day,youtube_units:D.limits.youtube_units_per_day,llm:D.limits.llm_calls_per_day})[r.service]||'n/a']],D.usage),
'Infrastructure costs':()=>{const rows=Object.entries(D.costs).map(([k,v])=>({k,v}));rows.push({k:'TOTAL (USD)',v:D.cost_total});return table([['Item',r=>r.k],['USD / month',r=>r.v.toFixed(2)]],rows)}};
const T=(cols,rows)=>table(cols,rows),E=D.editorial;
tabs['Breaking']=()=>T([['Flagged until',s=>s.breaking_until],...sc],st.filter(s=>s.breaking&&Date.parse(s.breaking_until)>Date.parse(D.generated_at)));
tabs['Candidates (score 80+)']=()=>T(sc,st.filter(s=>s.score>=D.threshold&&s.evidence_ok&&Date.parse(s.last_item_at)>=day&&['new','verifying'].includes(s.editorial_status)));
tabs['Live coverage']=()=>{const d=document.createElement('div');d.appendChild(T([['Story',r=>r.story_id],['Event',r=>r.title],['State',r=>r.state],['Since',r=>r.state_changed_at]],E.live));d.appendChild(document.createElement('hr'));
d.appendChild(T([['Update #',r=>r.id],['Story',r=>r.story_id],['Update',r=>r.text],['Class',r=>r.classification],['Source',r=>link(r.source_url,'source')],['Status',r=>r.status==='approved'&&r.approved_hash===r.content_hash?'APPROVED':r.status==='approved'?'EDITED - needs re-approval':'draft']],E.live_updates));return d};
tabs['Investigative leads']=()=>T([['#',r=>r.id],['Lead',r=>r.title],['Status',r=>r.status],['Originality',r=>r.originality],['Documents',r=>r.documents],['Why it matters',r=>r.significance],['Alternative explanations',r=>r.alternatives],['Missing evidence',r=>r.missing_evidence],['Next steps',r=>r.next_steps],['Parties to approach',r=>r.parties]],E.leads);
tabs['Verification blocked']=()=>T([['Blocking claims',s=>s.blocked],...sc],st.filter(s=>s.blocked>0&&['verifying','new'].includes(s.editorial_status)&&s.package_path));
tabs['Website / video drafts']=()=>T([['Drafts (*=approved)',s=>s.drafts],['Article',s=>s.article_id||''],['Video',s=>s.video_id||''],...sc],st.filter(s=>s.drafts));
tabs['Corrections']=()=>T([['#',r=>r.id],['Story',r=>r.story_id],['Headline',r=>r.headline],['Correction',r=>r.text],['Kind',r=>r.severity],['Status',r=>r.status],['Logged',r=>r.created_at]],E.corrections);
tabs['Approval history']=()=>T([['When',r=>r.at],['Story',r=>r.story_id],['Item',r=>r.kind+(r.version?' v'+r.version:'')],['Action',r=>r.action],['Destination',r=>r.destination],['Still valid',r=>r.action==='approve'?(r.valid?'yes':'VOIDED: '+(r.invalidated_reason||'')):''],['Note',r=>r.reason]],E.approvals);
tabs['Evidence Library']=()=>T([['Story',r=>r.story_id],['Document',r=>link(r.url,r.title)],['Issuer',r=>r.issuer],['Date',r=>r.date],['Relevance',r=>r.relevance],['Class',r=>r.classification],['Opened by system',r=>r.opened_by_system?'yes':'no'],['Public-eligible',r=>r.public_eligible?'yes':'no']],E.evidence);
tabs['Latency']=()=>{const d=document.createElement('div');const o=E.latency.overall;const p=document.createElement('p');p.textContent='Source to detection, all sources: n='+o.n+', median '+o.median_min+' min, 90th percentile '+o.p90_min+' min (only items whose feed supplied a real publication time).';d.appendChild(p);
d.appendChild(T([['Source organisation',r=>r.org],['Items',r=>r.n],['Median (min)',r=>r.median_min],['90th pct (min)',r=>r.p90_min]],E.latency.source_to_detection));
d.appendChild(document.createElement('hr'));d.appendChild(T([['Stage',r=>r[0]],['Samples',r=>r[1].n],['Median (min)',r=>r[1].median]],Object.entries(E.latency.stages)));return d};
tabs['Audience & Commercial']=()=>{const p=document.createElement('p');p.textContent=D.bot_note+' Nothing is shown here rather than invented figures.';return p};
const nav=$('tabs'),view=$('view');Object.keys(tabs).forEach((n,i)=>{const b=document.createElement('button');b.textContent=n;b.onclick=()=>{[...nav.children].forEach(x=>x.classList.remove('on'));b.classList.add('on');view.replaceChildren(tabs[n]())};nav.appendChild(b);if(i===0)b.click()});renderHome(D)}
$('go').onclick=unlock;$('pw').addEventListener('keydown',e=>{if(e.key==='Enter')unlock()});
</script></body></html>"""


def write_dashboard(conn, cfg, now, out_dir: str, password: str | None = None) -> str:
    pw = password if password is not None else os.environ.get("DASHBOARD_PASSWORD", "")
    if len(pw) < 12:
        raise SystemExit("DASHBOARD_PASSWORD missing or shorter than 12 characters; refusing to publish.")
    os.makedirs(out_dir, exist_ok=True)
    html = TEMPLATE.replace("__BLOB__", encrypt(build_data(conn, cfg, now), pw)).replace("__ITER__", str(ITER))
    path = os.path.join(out_dir, "index.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    with open(os.path.join(out_dir, ".nojekyll"), "w") as f:
        f.write("")
    return path
