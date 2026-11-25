import base64
import json
import os
import re
import subprocess

import pytest

from conftest import NOW, FakeWeb, Resp
from newsroom import briefing, dashboard, tools
from newsroom.ingest import ingest, rescore_recent
from newsroom.package import CLOSING, build_package, make_hooks
from newsroom.verify import can_approve, extract_figures, figure_in_text, open_sources, seed_claims
from test_pipeline import BIG, item


def _story(conn, cfg):
    ingest(conn, [item(BIG, "https://doj.gov/p1")], NOW, cfg)
    rescore_recent(conn, NOW, cfg)
    seed_claims(conn, 1, cfg, NOW)


def test_figure_matching():
    assert figure_in_text("$1.2 billion", "fined US$1.2 billion today")
    assert figure_in_text("$1.2 billion", "a 1.2bn penalty")
    assert not figure_in_text("$1.2 billion", "a 12 billion penalty")
    assert extract_figures("Fined $1.2 billion in 2024, 45% rise")[:2] == ["$1.2 billion", "45%"]


def test_open_sources_verifies_only_what_is_found(conn, cfg):
    _story(conn, cfg)
    doc = b"<html><body>" + b"filler " * 80 + b"The penalty of $1.2 billion was announced.</body></html>"
    out = open_sources(conn, 1, NOW, get=FakeWeb({"https://doj.gov/p1": Resp(doc)}))
    assert out["opened"] == ["https://doj.gov/p1"]
    st = {r["claim_type"]: r["status"] for r in conn.execute("SELECT claim_type, status FROM claims")}
    assert st["source"] == "VERIFIED" and st["figure"] == "VERIFIED"
    assert st["names"] == "UNVERIFIED"                       # manual claims are never auto-verified
    ok, out = can_approve(conn, 1)
    assert not ok and len(out) >= 5


def test_unopenable_source_is_never_claimed_opened(conn, cfg):
    _story(conn, cfg)
    open_sources(conn, 1, NOW, get=FakeWeb())               # everything 404s
    assert conn.execute("SELECT status FROM claims WHERE claim_type='source'").fetchone()["status"] == "UNVERIFIED"
    assert conn.execute("SELECT COUNT(*) c FROM source_checks WHERE error IS NOT NULL").fetchone()["c"] >= 1
    assert conn.execute("SELECT COUNT(*) c FROM source_checks WHERE http_status=200").fetchone()["c"] == 0


def test_package_contents(conn, cfg, tmp_path):
    _story(conn, cfg)
    rel = build_package(conn, 1, str(tmp_path), NOW)
    files = sorted(os.listdir(tmp_path / rel))
    assert files == ["01_fact_register.md", "02_sources.md", "03_hooks.txt", "04_drafting_brief.md", "05_part_e_report.md"]
    brief = (tmp_path / rel / "04_drafting_brief.md").read_text()
    assert CLOSING in brief and "untrusted_source" in brief and "170-190" in brief
    partE = (tmp_path / rel / "05_part_e_report.md").read_text()
    for k in ("FACTS", "SOURCES", "DEFAMATION", "COPYRIGHT", "FAIRNESS & CONTEXT", "TITLE & THUMBNAIL", "AI / PLATFORM DISCLOSURES", "NOT an approval"):
        assert k in partE
    assert make_hooks(BIG)[0].startswith("$1.2 billion")


# ---------- briefing ----------
def test_briefing_sent_once_per_sydney_day_and_not_before_7(conn, cfg, tg):
    from datetime import timedelta
    n, t = tg
    _story(conn, cfg)
    assert briefing.maybe_send(conn, cfg, n, NOW - timedelta(hours=2)) == "not-due"     # 05:00 Sydney
    assert briefing.maybe_send(conn, cfg, n, NOW) == "sent"                              # 07:00 Sydney
    assert briefing.maybe_send(conn, cfg, n, NOW + timedelta(hours=1)) == "duplicate"    # second cron slot
    assert "overnight briefing" in t.sent[0] and "Top stories" in t.sent[0]


# ---------- dashboard ----------
def test_dashboard_requires_strong_password(conn, cfg, tmp_path):
    with pytest.raises(SystemExit):
        dashboard.write_dashboard(conn, cfg, NOW, str(tmp_path), password="short")


def test_dashboard_is_encrypted_and_decryptable_with_webcrypto(conn, cfg, tmp_path):
    _story(conn, cfg)
    pw = "correct horse battery staple 42"
    path = dashboard.write_dashboard(conn, cfg, NOW, str(tmp_path), password=pw)
    html = open(path).read()
    assert "indicted" not in html and "Sydney" not in html.split("<script>")[0] and pw not in html   # no plaintext data
    blob = re.search(r'const B64="([^"]+)"', html).group(1)
    js = """
    const b=Buffer.from(process.argv[1],'base64');const pw=process.argv[2];
    (async()=>{const km=await crypto.subtle.importKey('raw',new TextEncoder().encode(pw),'PBKDF2',false,['deriveKey']);
    const key=await crypto.subtle.deriveKey({name:'PBKDF2',salt:b.subarray(0,16),iterations:600000,hash:'SHA-256'},km,{name:'AES-GCM',length:256},false,['decrypt']);
    try{const pt=await crypto.subtle.decrypt({name:'AES-GCM',iv:b.subarray(16,28)},key,b.subarray(28));console.log(Buffer.from(pt).toString())}
    catch(e){console.log('DECRYPT_FAILED')}})()"""
    good = subprocess.run(["node", "-e", js, blob, pw], capture_output=True, text=True).stdout
    assert json.loads(good)["stories"][0]["headline"] == BIG
    bad = subprocess.run(["node", "-e", js, blob, "wrong password here 123"], capture_output=True, text=True).stdout
    assert "DECRYPT_FAILED" in bad


def test_dashboard_js_parses(tmp_path, conn, cfg):
    path = dashboard.write_dashboard(conn, cfg, NOW, str(tmp_path), password="correct horse battery staple 42")
    script = re.search(r"<script>(.*)</script>", open(path).read(), re.S).group(1)
    (tmp_path / "s.js").write_text(script)
    r = subprocess.run(["node", "--check", str(tmp_path / "s.js")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# ---------- acceptance gate ----------
def test_acceptance_fails_on_empty_system(conn, cfg):
    ok, rows = tools.acceptance(conn, cfg, NOW)
    assert not ok and sum(1 for r in rows if r[1]) <= 2   # only 'costs documented' (+maybe workflow) can pass on an empty DB


# ---------- repo hygiene ----------
def test_no_secrets_or_tokens_in_repo():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pat = re.compile(r"(\d{8,10}:[A-Za-z0-9_-]{30,}|ghp_[A-Za-z0-9]{20,}|sk-ant-[A-Za-z0-9-]{10,})")
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d not in (".git", "__pycache__", ".pytest_cache", "data")]
        for f in fn:
            if f.endswith((".py", ".json", ".yml", ".md", ".txt")):
                assert not pat.search(open(os.path.join(dp, f), encoding="utf-8").read()), f
