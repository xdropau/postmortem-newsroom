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
    return {"editorial": editorial, "bot_note": "Audience and commercial data are not connected yet (needs YouTube, Search Console and Shopify access).", "generated_at": iso(now), "threshold": cfg["settings"]["alert_threshold"], "stories": stories, "sources": src,
            "runs": runs, "recent_runs": recent, "usage": usage, "limits": cfg["settings"]["limits"],
            "costs": costs, "cost_total": round(sum(costs.values()), 2)}


def encrypt(payload: dict, password: str, iterations: int = ITER) -> str:
    salt, iv = os.urandom(16), os.urandom(12)
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations).derive(password.encode())
    ct = AESGCM(key).encrypt(iv, json.dumps(payload).encode(), None)
    return base64.b64encode(salt + iv + ct).decode()


TEMPLATE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="robots" content="noindex,nofollow">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>The Post-Mortem Newsroom</title>
<style>body{font-family:system-ui,sans-serif;margin:0;background:#111;color:#eee}header{padding:14px 18px;border-bottom:1px solid #333}
h1{font-size:18px;margin:0}main{padding:14px 18px}input,button{font-size:16px;padding:8px}nav button{margin:2px;background:#222;color:#eee;border:1px solid #444;border-radius:4px}
nav button.on{background:#b3202a}table{border-collapse:collapse;width:100%;font-size:14px}td,th{border-bottom:1px solid #2a2a2a;padding:6px;text-align:left;vertical-align:top}
a{color:#7db7ff}.s{font-weight:bold}.bad{color:#ff7a7a}.ok{color:#7be29a}small{color:#999}</style></head><body>
<header><h1>THE POST-MORTEM &middot; Newsroom <small>Documents. Money. Evidence.</small></h1></header>
<main><div id="lock"><p>Enter dashboard password</p><input id="pw" type="password" autocomplete="current-password"> <button id="go">Unlock</button><p id="err" class="bad"></p></div>
<div id="app" hidden><nav id="tabs"></nav><p><small id="meta"></small></p><div id="view"></div></div></main>
<script>
const B64="__BLOB__",ITER=__ITER__;
const $=id=>document.getElementById(id);
function td(t,h){const c=document.createElement(h?'th':'td');if(t instanceof Node)c.appendChild(t);else c.textContent=t==null?'':String(t);return c}
function link(u,t){if(!/^https?:\\/\\//i.test(u||''))return document.createTextNode(t||'');const a=document.createElement('a');a.href=u;a.target='_blank';a.rel='noopener noreferrer';a.textContent=t||u;return a}
async function unlock(){try{const raw=Uint8Array.from(atob(B64),c=>c.charCodeAt(0));const salt=raw.slice(0,16),iv=raw.slice(16,28),ct=raw.slice(28);
const km=await crypto.subtle.importKey('raw',new TextEncoder().encode($('pw').value),'PBKDF2',false,['deriveKey']);
const key=await crypto.subtle.deriveKey({name:'PBKDF2',salt,iterations:ITER,hash:'SHA-256'},km,{name:'AES-GCM',length:256},false,['decrypt']);
const pt=await crypto.subtle.decrypt({name:'AES-GCM',iv},key,ct);render(JSON.parse(new TextDecoder().decode(pt)))}catch(e){$('err').textContent='Wrong password or corrupted data.'}}
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
const nav=$('tabs'),view=$('view');Object.keys(tabs).forEach((n,i)=>{const b=document.createElement('button');b.textContent=n;b.onclick=()=>{[...nav.children].forEach(x=>x.classList.remove('on'));b.classList.add('on');view.replaceChildren(tabs[n]())};nav.appendChild(b);if(i===0)b.click()})}
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
