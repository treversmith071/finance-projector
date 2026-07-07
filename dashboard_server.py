#!/usr/bin/env python3
"""
Local bridge server for the finance dashboard (standard library only).

A web page is sandboxed by the browser: it can't launch programs or read local
files. This tiny server (bound to 127.0.0.1 only) is the bridge — it serves the
dashboard and exposes endpoints the page can call to trigger the Empower
net-worth fetch and read the result.

  GET  /                 -> the generated dashboard HTML (or the upload page if none yet)
  GET  /networth.json    -> last fetched net worth (404 if never synced)
  GET  /api/config       -> machine-local baseline config ({} if not set yet)
  POST /api/config       -> save baseline config (from the dashboard onboarding UI)
  POST /api/ingest       -> save pasted/dropped transactions and rebuild the dashboard
  POST /api/reset        -> clear baseline config + net worth + Empower session
                            (start over); add ?all=1 to also wipe the loaded
                            transactions + prior-year archive (full reset)
  POST /api/sync         -> run empower_playwright.py in the background
  GET  /api/sync-status  -> {state: idle|running|done|error, networth, fetched_at, message}

Run with the venv Python so the fetch subprocess finds Playwright:
    .venv/bin/python dashboard_server.py

Presentation: the frozen standalone app shows the dashboard in its own native
window (pywebview / WKWebView); run from source it opens your default browser
instead. Force either with FINPROJECT_WINDOW=1/0. Ctrl+C (or closing the window)
stops it. Nothing is exposed off your machine.

Data hygiene: run-local personal financial data (the ingested transactions,
the rendered dashboard, and the cached Empower login session) is wiped on both
startup and shutdown, so nothing sensitive lingers on disk while the app isn't
running. The prior_years/ archive, the reusable baseline config
(finance_config.json), and the last-synced net worth (networth.json) survive.
See purge_session_data().
"""
import contextlib
import glob
import http.server
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import date
from urllib.parse import parse_qs, urlparse

import app_paths
import empower_playwright
import project

HERE = os.path.dirname(os.path.abspath(__file__))
_DATA = app_paths.data_dir()       # per-user writable state (repo dir in dev)
_RES = app_paths.resource_dir()    # read-only bundled assets (repo dir in dev)
DASHBOARD = os.path.join(_DATA, "monthly_savings_2026.html")
DOLPHIN = os.path.join(_RES, "dolphin.png")       # logo asset
FAVICON = os.path.join(_RES, "favicon.png")       # square tab icon (PNG)
FAVICON_ICO = os.path.join(_RES, "favicon.ico")   # square tab icon (multi-size ICO)
NETWORTH = os.path.join(_DATA, "networth.json")
CONFIG = os.path.join(_DATA, "finance_config.json")
# Baseline keys the onboarding/settings UI is allowed to persist (never trust the
# page blindly — only these are written to the config file). "group_buckets" is
# the user's transaction-mapping (group→spending/savings/income); it drives
# classify() rather than the client-side recompute, so a change to it requires a
# server-side dashboard rebuild (see do_POST /api/config).
CONFIG_KEYS = ("rent", "biweekly_deposit", "k401_annual",
               "receive_bonuses", "bonus_threshold", "group_buckets")
CLASSIFY_KEYS = ("group_buckets",)
INPUT = os.path.join(_DATA, "transactions.csv")   # raw ingested export (gitignored)
# Historical archive: one <YYYY>_transactions.csv per prior year. Survives the
# data-hygiene purge (see purge_session_data) and drives the per-year tabs +
# seasonal projection. Must match project.py's DEFAULT_PRIOR_YEARS_DIR.
PRIOR_YEARS = os.path.join(_DATA, "prior_years")
TMP_INPUT = "/tmp/finproject-input.csv"          # raw paste written by the /finproject command
EMPOWER_PROFILE = os.path.join(_DATA, "empower_profile")  # cached Empower login session
HOST = "127.0.0.1"
PORT = int(os.environ.get("FINPROJECT_PORT") or 8000)

# The native app window (pywebview), when running in standalone-window mode.
_window = None


def _self_cmd(*extra: str) -> list:
    """Command to re-invoke *this program* with extra args. Frozen: the app
    binary itself (sys.executable). From source: the interpreter + this file."""
    if app_paths.is_frozen():
        return [sys.executable, *extra]
    return [sys.executable, os.path.abspath(__file__), *extra]
# Friendly hostname shown in the browser URL. Only used if /etc/hosts maps it to
# loopback (add:  127.0.0.1 finance-projector). Falls back to 127.0.0.1 otherwise,
# so a fresh clone without the hosts entry still works.
HOSTNAME_ALIAS = "finance-projector"


def browse_url() -> str:
    """Prefer http://finance-projector:8000/ when the alias resolves to loopback."""
    import socket
    try:
        if socket.gethostbyname(HOSTNAME_ALIAS).startswith("127."):
            return f"http://{HOSTNAME_ALIAS}:{PORT}/"
    except OSError:
        pass
    return f"http://{HOST}:{PORT}/"


_lock = threading.Lock()
_sync = {"state": "idle", "message": "", "networth": None, "components": None,
         "fetched_at": None}


def _set_sync(**kw):
    with _lock:
        _sync.update(kw)


def _run_fetch():
    _set_sync(state="running", message="Opening Empower…", networth=None,
              components=None, fetched_at=None)
    # The fetch drives a real Chrome via Playwright, so it needs its own process
    # (own event loop, GUI browser). Re-invoke ourselves with --run-fetch, which
    # routes to empower_playwright.fetch_and_save(). Works both from source and
    # as a frozen bundle (there's no standalone python to call in a bundle).
    try:
        proc = subprocess.run(_self_cmd("--run-fetch"), capture_output=True,
                              text=True, timeout=600)
    except subprocess.TimeoutExpired:
        _set_sync(state="error", message="Timed out (login not completed?).")
        return
    except Exception as e:  # pragma: no cover - defensive
        _set_sync(state="error", message=str(e))
        return

    if proc.returncode == 0 and os.path.exists(NETWORTH):
        try:
            with open(NETWORTH) as f:
                data = json.load(f)
            _set_sync(state="done", message="",
                      networth=data.get("networth"),
                      components=data.get("components"),
                      fetched_at=data.get("fetched_at"))
            return
        except Exception as e:  # pragma: no cover
            _set_sync(state="error", message=f"Read error: {e}")
            return
    tail = (proc.stderr or proc.stdout or "fetch failed").strip().splitlines()
    _set_sync(state="error", message=(tail[-1] if tail else "fetch failed"))


def _bucket_by_year(body: str):
    """Split an uploaded export into current-year vs prior-year rows by each
    transaction's own date — so a single file can carry both. Reuses project's
    line splitter/date parser (so every accepted CSV/TSV/Excel format works) and
    preserves each raw line byte-for-byte.

    Returns (current_year, current_lines, prior_map, current_count) where
    prior_map is {year: [raw_line, ...]}. Header/undated lines go to the
    current-year bucket (project.load() skips them there) and are not counted.
    """
    current_year = date.today().year
    current_lines, prior_map, current_count = [], {}, 0
    for raw in body.splitlines():
        fields = project._split_line(raw)
        d = project._parse_date(fields[0]) if fields else None
        if d is None:
            current_lines.append(raw)      # header/undated -> live file, skipped by loader
            continue
        if d.year == current_year:
            current_lines.append(raw)
            current_count += 1
        else:
            prior_map.setdefault(d.year, []).append(raw)
    return current_year, current_lines, prior_map, current_count


def _rebuild_dashboard(input_path=None, extra_args=()):
    """(Re)generate the dashboard by calling project.main() in-process. Defaults to
    the current-year INPUT; pass input_path/extra_args (e.g. --as-of) to build from
    a different year. Returns (ok, err). In-process (vs a subprocess) so it works in
    a frozen bundle where there's no standalone python to run project.py."""
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = project.main([input_path or INPUT, "--html", DASHBOARD, *extra_args])
    except SystemExit as e:  # argparse or explicit exit
        rc = e.code if isinstance(e.code, int) else 1
    except Exception as e:  # pragma: no cover - defensive
        return False, str(e)
    if rc == 0 and os.path.exists(DASHBOARD):
        return True, ""
    tail = (err.getvalue() or out.getvalue() or "generation failed").strip().splitlines()
    return False, (tail[-1] if tail else "generation failed")


def _current_groups():
    """Payee/destination groups for the live dataset, for the mapping screen.
    Empty list if there's nothing loaded or grouping fails."""
    if not os.path.exists(INPUT):
        return []
    try:
        return project.build_groups(INPUT)
    except Exception:
        return []


def _latest_archive_year():
    """Most recent year with a prior_years/<YYYY>_transactions.csv file, or None."""
    years = []
    for p in glob.glob(os.path.join(PRIOR_YEARS, "*_transactions.csv")):
        stem = os.path.basename(p).split("_", 1)[0]
        if stem.isdigit():
            years.append(int(stem))
    return max(years) if years else None


# Welcome page shown at GET / when no dashboard has been built yet. Ends with a
# required upload area (drop or paste) — the only way to proceed to the dashboard.
UPLOAD_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>FinProject — Welcome</title>
<link rel="icon" href="/favicon.ico" sizes="any">
<link rel="icon" type="image/png" href="/favicon.png">
<link rel="apple-touch-icon" href="/favicon.png"><style>
:root{--bg:#0f1117;--card:#1a1d27;--text:#e6e8ee;--muted:#9aa0ad;--accent:#7c5cff;--accent2:#39d3bb}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:flex;align-items:center;
justify-content:center;background:var(--bg);color:var(--text);padding:32px;
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:600px;width:100%}h1{font-size:30px;margin:0 0 6px}
.sub{color:var(--muted);margin:0 0 26px;font-size:15px;line-height:1.5}
.feat{display:flex;flex-direction:column;gap:12px;margin-bottom:28px}
.feat .item{display:flex;gap:12px;align-items:flex-start;background:var(--card);
border:1px solid #262a36;border-radius:12px;padding:14px 16px}
.feat .dot{width:10px;height:10px;border-radius:50%;margin-top:5px;flex:none}
.feat .t{font-size:14px}.feat .t b{display:block;margin-bottom:2px}
.feat .t span{color:var(--muted);font-size:13px;line-height:1.4}
.card{background:var(--card);border:1px solid #2a2f40;border-radius:14px;padding:20px 22px}
.card h2{font-size:16px;margin:0 0 4px}.card .h{color:var(--muted);font-size:13px;margin:0 0 14px}
.drop{border:2px dashed #33384a;border-radius:12px;padding:34px 18px;text-align:center;
color:var(--muted);cursor:pointer;background:var(--bg);transition:border-color .15s,color .15s}
.drop.over{border-color:var(--accent);color:var(--text)}
.drop.has-file{border-style:solid;border-color:var(--accent);color:var(--text)}
.filechip{display:inline-flex;align-items:center;gap:11px;background:var(--card);
border:1px solid #33384a;border-radius:10px;padding:11px 15px;max-width:100%}
.filechip svg{flex:none}
.fc-name{font-size:14px;font-weight:600;color:var(--text);word-break:break-all;text-align:left}
.fc-hint{margin-top:11px;font-size:12px;color:var(--muted)}
textarea{width:100%;margin-top:12px;background:var(--bg);border:1px solid #262a36;border-radius:10px;
color:var(--text);padding:11px;font-size:12px;font-family:ui-monospace,Menlo,monospace;resize:vertical}
textarea:focus{outline:none;border-color:var(--accent)}
.row{display:flex;gap:10px;align-items:center;margin-top:14px}
.btn{border-radius:9px;padding:10px 20px;font-size:14px;font-weight:600;cursor:pointer;
border:1px solid var(--accent);background:var(--accent);color:#fff}.btn[disabled]{opacity:.6;cursor:default}
.msg{color:var(--muted);font-size:13px;margin-left:auto}
.power-btn{position:fixed;top:20px;left:20px;z-index:50;width:44px;height:44px;border-radius:50%;
background:var(--card);border:1px solid #262a36;color:var(--muted);cursor:pointer;display:flex;
align-items:center;justify-content:center;transition:color .15s,border-color .15s}
.power-btn:hover{color:#ff6b6b;border-color:#ff6b6b}.power-btn svg{width:20px;height:20px}
.reset-btn{position:fixed;top:20px;left:74px;z-index:50;width:44px;height:44px;border-radius:50%;
background:var(--card);border:1px solid #262a36;color:var(--muted);cursor:pointer;display:flex;
align-items:center;justify-content:center;transition:color .15s,border-color .15s}
.reset-btn:hover{color:#f0a500;border-color:#f0a500}.reset-btn svg{width:20px;height:20px}</style></head><body>
<button class="power-btn" id="powerBtn" title="Shut down &amp; close" aria-label="Shut down and close">
<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="3" x2="12" y2="12"/><path d="M6.4 6.4a9 9 0 1 0 11.2 0"/></svg></button>
<button class="reset-btn" id="resetBtn" title="Reset — clear all data" aria-label="Reset all data">
<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/></svg></button>
<div class="wrap">
<div style="display:flex;align-items:center;gap:13px;margin-bottom:6px">
<img src="/dolphin.png" alt="" style="height:46px;width:auto;flex:none" onerror="this.remove()">
<h1 style="margin:0">Welcome to FinProject</h1></div>
<p class="sub">Turn your account export into a live savings &amp; spending projection, a
synced net-worth view, and a Monte Carlo-style retirement outlook.</p>
<div class="feat">
<div class="item"><span class="dot" style="background:var(--accent)"></span><div class="t"><b>Save / Spend</b>
<span>Year-to-date savings &amp; spending vs. budget, with a seasonal projection to year-end.</span></div></div>
<div class="item"><span class="dot" style="background:var(--accent2)"></span><div class="t"><b>Net worth</b>
<span>Sync your net worth and its cash / investments / credit breakdown from Empower.</span></div></div>
<div class="item"><span class="dot" style="background:#ff5fa2"></span><div class="t"><b>Retirement</b>
<span>Historical-cycles simulation (1871&ndash;2025) of your plan's success rate.</span></div></div>
</div>
<div class="card">
<h2>Get started</h2>
<p class="h">Load your hub-account export to build the dashboard &mdash; required to continue.</p>
<div class="drop" id="drop"><span id="dropText">Drop CSV / Excel export here, or click to choose</span></div>
<input type="file" id="file" accept=".csv,.tsv,.txt" hidden>
<textarea id="paste" rows="5" placeholder="…or paste rows here (Date  Time  Amount  Type  Description)"></textarea>
<div class="row"><button class="btn" id="go">Build dashboard</button><span class="msg" id="msg"></span></div>
</div>
</div><script>
(function(){var pb=document.getElementById('powerBtn');if(!pb)return;
pb.addEventListener('click',function(){pb.disabled=true;
fetch('/api/shutdown',{method:'POST'}).catch(function(){}).finally(function(){
window.close();
setTimeout(function(){document.documentElement.innerHTML='<body style="margin:0;min-height:100vh;'
+'display:flex;align-items:center;justify-content:center;background:#0f1117;color:#9aa0ad;'
+'font:15px -apple-system,BlinkMacSystemFont,sans-serif">Server stopped — you can close this tab.</body>';},300);
});});})();
(function(){var rb=document.getElementById('resetBtn');if(!rb)return;
rb.addEventListener('click',function(){
if(!confirm('Reset everything? This permanently clears your loaded transactions, prior-year history, net worth, retirement projection, and baseline settings. This cannot be undone.'))return;
rb.disabled=true;
var done=function(){try{localStorage.clear();}catch(e){}location.href='/';};
fetch('/api/reset?all=1',{method:'POST'}).then(done).catch(done);
});})();
let pending='';
const drop=document.getElementById('drop'),file=document.getElementById('file'),
paste=document.getElementById('paste'),go=document.getElementById('go'),msg=document.getElementById('msg');
function esc(s){return s.replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function extLabel(name){const m=/\\.([a-z0-9]+)$/i.exec(name);let e=m?m[1].toUpperCase():'FILE';return e.slice(0,4);}
function showFile(name){drop.classList.add('has-file');
drop.innerHTML='<div class="filechip">'
+'<svg width="30" height="36" viewBox="0 0 30 36" fill="none" aria-hidden="true">'
+'<path d="M6.6 0H19l9 9v24.4A2.6 2.6 0 0 1 25.4 36H6.6A2.6 2.6 0 0 1 4 33.4V2.6A2.6 2.6 0 0 1 6.6 0Z" fill="#7c5cff" fill-opacity=".16" stroke="#7c5cff" stroke-width="1.6"/>'
+'<path d="M19 0v7a2 2 0 0 0 2 2h7" stroke="#7c5cff" stroke-width="1.6" fill="none"/>'
+'<text x="16" y="26" text-anchor="middle" font-size="7.5" font-weight="700" fill="#7c5cff" font-family="ui-monospace,Menlo,monospace">'+esc(extLabel(name))+'</text>'
+'</svg><div class="fc-name">'+esc(name)+'</div></div>'
+'<div class="fc-hint">Click to choose a different file</div>';}
function readFile(f){const r=new FileReader();r.onload=()=>{pending=r.result;showFile(f.name);msg.textContent=f.name+' ready';};r.readAsText(f);}
drop.onclick=()=>file.click();
file.onchange=e=>{if(e.target.files[0])readFile(e.target.files[0]);};
['dragover','dragenter'].forEach(ev=>drop.addEventListener(ev,e=>{e.preventDefault();drop.classList.add('over');}));
['dragleave','drop'].forEach(ev=>drop.addEventListener(ev,e=>{e.preventDefault();drop.classList.remove('over');}));
drop.addEventListener('drop',e=>{if(e.dataTransfer.files[0])readFile(e.dataTransfer.files[0]);});
go.onclick=()=>{const text=pending||paste.value.trim();
if(!text){msg.textContent='Drop a file or paste rows first.';return;}
go.disabled=true;msg.textContent='Building…';
fetch('/api/ingest',{method:'POST',headers:{'Content-Type':'text/plain'},body:text})
.then(r=>r.json()).then(d=>{if(d.ok){msg.textContent=(d.summary||('Loaded '+d.rows+' rows'))+(d.needs_current?'':' — opening…');
if(d.needs_current){go.disabled=false;}else{setTimeout(()=>location.href='/',900);}}
else{go.disabled=false;msg.textContent='Error: '+(d.error||'failed');}})
.catch(()=>{go.disabled=false;msg.textContent='Server error.';});};
</script></body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        try:
            self.wfile.write(b)
        except BrokenPipeError:  # pragma: no cover
            pass

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            if not os.path.exists(DASHBOARD):
                # No dashboard yet -> show the drag-and-drop upload page.
                return self._send(200, UPLOAD_PAGE, "text/html; charset=utf-8")
            with open(DASHBOARD, "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if path in ("/dolphin.png", "/favicon.png", "/favicon.ico"):
            asset = {"/dolphin.png": DOLPHIN, "/favicon.png": FAVICON,
                     "/favicon.ico": FAVICON_ICO}[path]
            ctype = "image/x-icon" if path.endswith(".ico") else "image/png"
            if not os.path.exists(asset):
                return self._send(404, "not found", "text/plain")
            with open(asset, "rb") as f:
                return self._send(200, f.read(), ctype)
        if path == "/networth.json":
            if not os.path.exists(NETWORTH):
                return self._send(404, json.dumps({"error": "not synced"}))
            with open(NETWORTH, "rb") as f:
                return self._send(200, f.read())
        if path == "/api/sync-status":
            with _lock:
                return self._send(200, json.dumps(_sync))
        if path == "/api/groups":
            return self._send(200, json.dumps({"groups": _current_groups()}))
        if path == "/api/config":
            if not os.path.exists(CONFIG):
                return self._send(200, json.dumps({}))
            with open(CONFIG, "rb") as f:
                return self._send(200, f.read())
        return self._send(404, "not found", "text/plain")

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/config":
            try:
                length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(data, dict):
                    raise ValueError("expected an object")
            except (ValueError, json.JSONDecodeError) as e:
                return self._send(400, json.dumps({"error": str(e)}))
            # Merge into any existing config so keys the onboarding UI doesn't
            # manage — e.g. the personal account identifiers classify() reads
            # (account_holder_name, savings_acct_suffixes, spending_acct_suffix)
            # — survive a settings save instead of being dropped.
            existing = {}
            if os.path.exists(CONFIG):
                try:
                    with open(CONFIG, "rb") as f:
                        loaded = json.load(f)
                    if isinstance(loaded, dict):
                        existing = loaded
                except (ValueError, json.JSONDecodeError):
                    existing = {}
            saved = {k: data[k] for k in CONFIG_KEYS if k in data}
            # Did any classify()-affecting identifier actually change? If so the
            # dashboard must be rebuilt server-side, since the client can't
            # re-classify already-bucketed rows.
            classify_changed = any(
                k in saved and saved[k] != existing.get(k) for k in CLASSIFY_KEYS)
            existing.update(saved)
            with open(CONFIG, "w") as f:
                json.dump(existing, f, indent=2)
                f.write("\n")
            regenerated = False
            if classify_changed and os.path.exists(INPUT):
                regenerated, _ = _rebuild_dashboard()
            return self._send(200, json.dumps(
                {"ok": True, "saved": saved, "regenerated": regenerated}))
        if path == "/api/ingest":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
            if not body.strip():
                return self._send(400, json.dumps({"ok": False, "error": "empty upload"}))

            current_year, cur_lines, prior_map, cur_count = _bucket_by_year(body)

            # Archive each prior year present (one file per year; replaces that
            # year's data, leaves untouched years alone).
            archived = {}
            if prior_map:
                os.makedirs(PRIOR_YEARS, exist_ok=True)
                for yr, lines in prior_map.items():
                    with open(os.path.join(PRIOR_YEARS, f"{yr}_transactions.csv"), "w") as f:
                        f.write("\n".join(lines) + "\n")
                    archived[yr] = len(lines)

            # Only overwrite the live dataset when this upload actually carried
            # current-year rows; otherwise keep whatever's already loaded (so an
            # "add my history" upload doesn't wipe the current year).
            if cur_count > 0:
                with open(INPUT, "w") as f:
                    f.write("\n".join(cur_lines) + "\n")

            ok, err = _rebuild_dashboard()
            shown_year = current_year if ok else None
            if not ok:
                # No current-year data to build a live dashboard. Rather than
                # dead-end, fall back to the most recent archived year so a
                # prior-year-only upload still produces a viewable dashboard.
                latest = _latest_archive_year()
                if latest is not None:
                    lf = os.path.join(PRIOR_YEARS, f"{latest}_transactions.csv")
                    ok, err = _rebuild_dashboard(input_path=lf,
                                                 extra_args=["--as-of", f"{latest}-12-31"])
                    shown_year = latest if ok else None
                if not ok:
                    return self._send(500, json.dumps({"ok": False, "error": err}))

            arch = ", ".join(f"{y} ({n})" for y, n in sorted(archived.items()))
            cur_txt = f"{cur_count} row{'' if cur_count == 1 else 's'} for {current_year}"
            if cur_count > 0 and archived:
                summary = f"Loaded {cur_txt} · archived {arch}"
            elif cur_count > 0:
                summary = f"Loaded {cur_txt}"
            elif shown_year is not None and shown_year != current_year:
                summary = (f"Archived {arch}. Showing {shown_year} — add {current_year} "
                           f"data for a live current-year view.")
            elif archived:
                summary = f"Archived {arch}"
            else:
                summary = "Rebuilt dashboard"
            return self._send(200, json.dumps({
                "ok": True, "rows": cur_count, "archived": archived,
                "summary": summary, "groups": _current_groups()}))
        if path == "/api/reset":
            # ?all=1 is a full wipe: also clears the loaded transactions (current
            # year) and the prior-year archive. Without it, only the baseline
            # config + net worth + Empower session are cleared (transactions kept).
            full = parse_qs(urlparse(self.path).query).get("all", ["0"])[0] == "1"
            removed = []
            targets = [CONFIG, NETWORTH] + ([INPUT] if full else [])
            for p in targets:
                if os.path.exists(p):
                    try:
                        os.remove(p)
                        removed.append(os.path.basename(p))
                    except OSError:
                        pass
            if full and os.path.isdir(PRIOR_YEARS):
                for p in glob.glob(os.path.join(PRIOR_YEARS, "*.csv")):
                    try:
                        os.remove(p)
                        removed.append("prior_years/" + os.path.basename(p))
                    except OSError:
                        pass
            if os.path.isdir(EMPOWER_PROFILE):
                shutil.rmtree(EMPOWER_PROFILE, ignore_errors=True)
                removed.append("empower_profile")
            _set_sync(state="idle", message="", networth=None, components=None,
                      fetched_at=None)
            # Rebuild so the dashboard re-enters onboarding (setup mode). If there's
            # no data to rebuild from, drop the HTML so GET / shows the upload page.
            if os.path.exists(INPUT):
                _rebuild_dashboard()
            elif os.path.exists(DASHBOARD):
                try:
                    os.remove(DASHBOARD)
                except OSError:
                    pass
            return self._send(200, json.dumps({"ok": True, "removed": removed}))
        if path == "/api/sync":
            with _lock:
                already = _sync["state"] == "running"
            if not already:
                threading.Thread(target=_run_fetch, daemon=True).start()
            return self._send(200, json.dumps({"state": "running"}))
        if path == "/api/shutdown":
            # Ack first, then stop the app just after the response flushes.
            self._send(200, json.dumps({"ok": True}))

            def _stop():
                if _window is not None:
                    # Window mode: closing the native window ends the GUI loop,
                    # which unwinds to the same cleanup as Ctrl+C.
                    try:
                        _window.destroy()
                        return
                    except Exception:
                        pass
                # Browser mode: SIGTERM routes through the finally-block cleanup.
                os.kill(os.getpid(), signal.SIGTERM)
            threading.Timer(0.3, _stop).start()
            return
        return self._send(404, "not found", "text/plain")

    def log_message(self, *args):  # keep the console quiet
        pass


def purge_session_data():
    """Remove run-local personal financial data from disk.

    Called on both new-instance startup and graceful shutdown so nothing
    sensitive lingers while the app isn't running. Everything cleared here is
    regenerated on the next run from a fresh export. Deliberately left untouched:
    the sanctioned historical archive in prior_years/, the reusable baseline
    config (finance_config.json), the last-synced net worth (networth.json) so
    the Net-worth card still shows your latest figure after a restart, and the
    Empower browser profile (empower_profile/) — retaining it keeps Empower's
    device-trust cookie so net-worth sync doesn't force a new-device MFA every
    launch. It's a bearer credential (gitignored, kept chmod 600); "Reset
    everything" still wipes it on demand.
    """
    for p in (INPUT, DASHBOARD, TMP_INPUT):
        try:
            os.remove(p)
        except OSError:
            pass


def _on_sigterm(signum, frame):
    # Make `kill`/SIGTERM (how bootstrap stops the bridge) unwind through the
    # same path as Ctrl+C so the finally-block cleanup always runs.
    raise KeyboardInterrupt


def _use_window() -> bool:
    """Whether to show the dashboard in a native window (pywebview) vs the browser.

    FINPROJECT_WINDOW=1/0 forces it. Otherwise a native window is used only by the
    frozen standalone app; the dev bridge (run from source, e.g. via /finproject)
    keeps opening the browser so it doesn't pop both a window AND a tab.
    """
    forced = os.environ.get("FINPROJECT_WINDOW")
    if forced == "0":
        return False
    if forced != "1" and not app_paths.is_frozen():
        return False
    try:
        import webview  # noqa: F401
        return True
    except Exception:
        return False


def _present(url: str) -> None:
    """Show the dashboard, then block the main thread until the app should quit.

    Native window: pywebview requires its GUI loop on the main thread, and it
    returns when the window is closed. Browser: open it and idle until a shutdown
    signal (SIGTERM from the power button / Ctrl+C) unwinds the caller.
    """
    global _window
    if _use_window():
        import webview
        _window = webview.create_window("FinProject", url,
                                        width=1280, height=880, min_size=(920, 640))
        webview.start()          # blocks until the window is closed
        return
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    while True:
        time.sleep(0.5)


def main() -> int:
    # Diagnostic: report whether bundled modules import (handy for the frozen app).
    if "--selfcheck" in sys.argv[1:]:
        for mod in ("app_paths", "project", "empower_playwright", "playwright", "webview"):
            try:
                __import__(mod)
                print(f"{mod}: OK")
            except Exception as e:  # pragma: no cover
                print(f"{mod}: FAIL — {e}")
        print(f"frozen: {app_paths.is_frozen()} · window mode: {_use_window()}")
        return 0

    # Worker mode: `--run-fetch` means we were re-invoked by _run_fetch() to do
    # the Playwright net-worth fetch in an isolated process. Do only that.
    if "--run-fetch" in sys.argv[1:]:
        return empower_playwright.fetch_and_save(fresh=False, save=True)

    # New instance: sweep any data a previous session left behind (covers a
    # crash or kill -9 where the shutdown cleanup below never got to run).
    purge_session_data()
    server = http.server.ThreadingHTTPServer((HOST, PORT), Handler)
    url = browse_url()
    print(f"Finance dashboard running at {url}  (close the window or Ctrl+C to stop)")
    # Serve in the background so the main thread can host the native window
    # (pywebview needs the GUI loop on the main thread) or the signal wait.
    threading.Thread(target=server.serve_forever, daemon=True).start()
    signal.signal(signal.SIGTERM, _on_sigterm)
    try:
        _present(url)
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.shutdown()
        server.server_close()
        purge_session_data()
    return 0


if __name__ == "__main__":
    sys.exit(main())
