#!/usr/bin/env python3
"""
ISPinger Web - a tiny self-hosted IP watcher with a browser UI.

Each enabled ping entry is pinged every 30 seconds. The first time it replies,
an email goes to that entry's address and the entry disables itself.

Standard library only (Python 3.8+). Runs on Linux or Windows.

    python3 ispinger_web.py --host 0.0.0.0 --port 8080

Web login (strongly recommended) comes from environment variables:
    ISPINGER_USER, ISPINGER_PASSWORD
"""

import argparse
import base64
import hmac
import json
import os
import platform
import re
import smtplib
import ssl
import subprocess
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

# ------------------------------------------------------------------ config
PING_INTERVAL = 30     # seconds between pings for each entry
PING_TIMEOUT = 2       # seconds to wait for one reply
IS_WINDOWS = platform.system() == "Windows"

EMAIL_RE = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")
TARGET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:%_\-]*$")   # IPv4 / IPv6 / hostname, no leading '-'


def now_str():
    return datetime.now().strftime("%b %d, %I:%M:%S %p")


def clean_email(text):
    t = str(text or "").strip()
    m = re.search(r"<([^>]+)>", t)
    return m.group(1).strip() if m else t


class ApiError(Exception):
    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.msg, self.code = msg, code


# ------------------------------------------------------------------ ping + email
def ping(target):
    """Return (ok, detail) using the system ping command."""
    if IS_WINDOWS:
        cmd = ["ping", "-n", "1", "-w", str(PING_TIMEOUT * 1000), target]
    else:
        cmd = ["ping", "-c", "1", "-W", str(PING_TIMEOUT), target]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                           timeout=PING_TIMEOUT + 5)
    except FileNotFoundError:
        return False, "Error: ping command not found (install iputils-ping)"
    except subprocess.TimeoutExpired:
        return False, "No reply (timed out)"
    out = (r.stdout or "") + (r.stderr or "")
    low = out.lower()
    if r.returncode == 0 and "ttl=" in low:      # 'ttl=' guards against Windows "unreachable" replies
        m = re.search(r"time[=<]\s*([\d.]+)\s*ms", out, re.I)
        return True, f"Reply in {m.group(1)} ms" if m else "Reply received"
    if any(s in low for s in ("unknown host", "not known", "could not find host",
                              "temporary failure in name resolution", "cannot resolve")):
        return False, "Error: hostname not found"
    if "operation not permitted" in low or "permission denied" in low:
        return False, "Error: ping not permitted for this user (see setup notes)"
    if "unreachable" in low:
        return False, "No reply (destination unreachable)"
    return False, "No reply (timed out)"


def send_email(settings, to, subject, body):
    host = (settings.get("smtp_host") or "").strip()
    sender = (settings.get("from_address") or "").strip()
    if not host or not sender:
        raise RuntimeError("Email is not set up yet. Fill in the Email settings tab.")
    msg = EmailMessage()
    msg["From"] = formataddr(("ISPinger", sender))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg.set_content(body)

    security = settings.get("security", "starttls")
    port = int(settings.get("smtp_port") or 587)
    ctx = ssl.create_default_context()
    try:
        if security == "ssl":
            server = smtplib.SMTP_SSL(host, port, timeout=20, context=ctx)
        else:
            server = smtplib.SMTP(host, port, timeout=20)
        with server:
            if security == "starttls":
                server.starttls(context=ctx)
            if settings.get("username"):
                server.login(settings["username"], settings.get("password", ""))
            server.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        raise RuntimeError("The mail server rejected the login. Check the username and app password.")
    except (OSError, smtplib.SMTPException) as e:
        raise RuntimeError(f"Could not send email: {e}")


# ------------------------------------------------------------------ data store
class Store:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.entries = []
        self.contacts = []
        self.settings = {"smtp_host": "smtp.gmail.com", "smtp_port": 587, "security": "starttls",
                         "username": "", "password": "", "from_address": ""}
        self.runtime = {}          # entry id -> {"next": ts, "busy": bool, "gen": int}
        self.load()

    # ---- persistence
    def load(self):
        if not os.path.exists(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        for e in raw.get("entries", []):
            e.setdefault("id", uuid.uuid4().hex)
            e["enabled"] = bool(e.get("enabled"))
            if e["enabled"]:
                e["state"], e["status"] = "pinging", "Pinging..."
            e.setdefault("state", "disabled")
            e.setdefault("status", "Disabled")
            e.setdefault("last_checked", "")
            self.entries.append(e)
        self.contacts = raw.get("contacts", [])
        for c in self.contacts:
            c.setdefault("id", uuid.uuid4().hex)
        self.settings.update(raw.get("settings", {}))

    def save(self):
        with self.lock:
            data = {"entries": self.entries, "contacts": self.contacts, "settings": self.settings}
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            if not IS_WINDOWS:
                os.chmod(tmp, 0o600)          # holds the SMTP password
            os.replace(tmp, self.path)

    # ---- helpers
    def rt(self, eid):
        return self.runtime.setdefault(eid, {"next": 0.0, "busy": False, "gen": 0})

    def find_entry(self, eid):
        for e in self.entries:
            if e["id"] == eid:
                return e
        raise ApiError("That entry no longer exists.", 404)

    def find_contact(self, cid):
        for c in self.contacts:
            if c["id"] == cid:
                return c
        raise ApiError("That contact no longer exists.", 404)

    @staticmethod
    def validate_entry(body):
        name = str(body.get("name", "")).strip()
        target = str(body.get("target", "")).strip()
        email = clean_email(body.get("email"))
        if not name:
            raise ApiError("Enter a name.")
        if len(name) > 80:
            raise ApiError("Keep the name under 80 characters.")
        if not target or len(target) > 253 or not TARGET_RE.match(target):
            raise ApiError("Enter a valid IP address or hostname.")
        if not EMAIL_RE.match(email):
            raise ApiError("Pick a contact or enter a valid email address.")
        return name, target, email

    @staticmethod
    def validate_contact(body):
        name = str(body.get("name", "")).strip()
        email = str(body.get("email", "")).strip()
        if not name:
            raise ApiError("Enter a name.")
        if not EMAIL_RE.match(email):
            raise ApiError("Enter a valid email address.")
        return name, email

    def _set_enabled(self, e, on):
        rt = self.rt(e["id"])
        rt["gen"] += 1                       # invalidates any ping already in flight
        e["enabled"] = on
        if on:
            rt["next"] = 0.0
            e["state"], e["status"] = "pinging", "Pinging..."
        else:
            e["state"], e["status"] = "disabled", "Disabled"

    # ---- API operations
    def snapshot(self):
        with self.lock:
            now = time.time()
            entries = []
            for e in self.entries:
                item = dict(e)
                item["next_in"] = round(max(0.0, self.rt(e["id"])["next"] - now), 1) if e["enabled"] else None
                entries.append(item)
            settings = {k: v for k, v in self.settings.items() if k != "password"}
            settings["has_password"] = bool(self.settings.get("password"))
            return {"entries": entries, "contacts": list(self.contacts),
                    "settings": settings, "interval": PING_INTERVAL}

    def add_entry(self, body):
        name, target, email = self.validate_entry(body)
        with self.lock:
            e = {"id": uuid.uuid4().hex, "name": name, "target": target, "email": email,
                 "enabled": False, "state": "disabled", "status": "Disabled", "last_checked": ""}
            self.entries.append(e)
            if body.get("enabled", True):
                self._set_enabled(e, True)
            self.save()
            return e

    def update_entry(self, eid, body):
        name, target, email = self.validate_entry(body)
        with self.lock:
            e = self.find_entry(eid)
            e.update(name=name, target=target, email=email)
            if body.get("enabled"):
                self._set_enabled(e, True)          # restart with the new details
            elif e["enabled"]:
                self._set_enabled(e, False)
            self.save()
            return e

    def delete_entry(self, eid):
        with self.lock:
            e = self.find_entry(eid)
            self.entries.remove(e)
            self.runtime.pop(eid, None)
            self.save()

    def set_enabled(self, eid, on):
        with self.lock:
            e = self.find_entry(eid)
            self._set_enabled(e, on)
            self.save()
            return e

    def add_contact(self, body):
        name, email = self.validate_contact(body)
        with self.lock:
            c = {"id": uuid.uuid4().hex, "name": name, "email": email}
            self.contacts.append(c)
            self.save()
            return c

    def update_contact(self, cid, body):
        name, email = self.validate_contact(body)
        with self.lock:
            c = self.find_contact(cid)
            c.update(name=name, email=email)
            self.save()
            return c

    def delete_contact(self, cid):
        with self.lock:
            self.contacts.remove(self.find_contact(cid))
            self.save()

    def update_settings(self, body):
        s = {}
        s["smtp_host"] = str(body.get("smtp_host", "")).strip()
        try:
            s["smtp_port"] = int(body.get("smtp_port", 587))
        except (TypeError, ValueError):
            raise ApiError("Port must be a number.")
        if not 1 <= s["smtp_port"] <= 65535:
            raise ApiError("Port must be between 1 and 65535.")
        s["security"] = body.get("security", "starttls")
        if s["security"] not in ("starttls", "ssl", "none"):
            raise ApiError("Pick a security option.")
        s["username"] = str(body.get("username", "")).strip()
        s["from_address"] = str(body.get("from_address", "")).strip()
        if s["from_address"] and not EMAIL_RE.match(s["from_address"]):
            raise ApiError("The From address isn't a valid email address.")
        pw = str(body.get("password", ""))
        with self.lock:
            if pw:
                if "gmail" in s["smtp_host"].lower():
                    pw = re.sub(r"\s", "", pw)    # Gmail shows app passwords with spaces
                s["password"] = pw
            self.settings.update(s)
            self.save()

    def send_test(self):
        with self.lock:
            settings = dict(self.settings)
        to = settings.get("from_address", "")
        if not EMAIL_RE.match(to or ""):
            raise ApiError("Enter a From address first. The test email is sent there.")
        try:
            send_email(settings, to, "ISPinger test email",
                       f"If you're reading this, ISPinger on {platform.node()} can send email.")
        except RuntimeError as e:
            raise ApiError(str(e))
        return to


# ------------------------------------------------------------------ ping engine
def run_ping(store, eid, target, gen):
    ok, detail = ping(target)
    email_job = None
    with store.lock:
        rt = store.rt(eid)
        rt["busy"] = False
        e = next((x for x in store.entries if x["id"] == eid), None)
        if e is None or rt["gen"] != gen or not e["enabled"]:
            return                                   # disabled, edited or removed mid-ping
        stamp = now_str()
        e["last_checked"] = stamp
        if ok:
            rt["gen"] += 1
            e["enabled"] = False
            e["state"], e["status"] = "reached", f"Reached {stamp} - sending email..."
            store.save()
            email_job = (dict(e), dict(store.settings), stamp, detail, rt["gen"])
        elif detail.startswith("Error"):
            e["state"], e["status"] = "error", f"{detail}. Retrying every {PING_INTERVAL}s."
        else:
            e["state"], e["status"] = "waiting", f"{detail}. Retrying every {PING_INTERVAL}s."

    if email_job:
        e, settings, stamp, detail, gen_after = email_job
        body = (f'The IP address for "{e["name"]}" ({e["target"]}) successfully pinged at {stamp}.\n\n'
                f"{detail}.\n\n"
                "This ping entry has now been disabled. Re-enable it in ISPinger to watch it again.\n\n"
                f"- Sent by ISPinger on {platform.node()}\n")
        try:
            send_email(settings, e["email"], f'ISPinger: {e["name"]} ({e["target"]}) is responding', body)
            state, status = "reached", f'Reached {stamp} - email sent to {e["email"]}'
        except RuntimeError as err:
            state, status = "failed", f"Reached {stamp} - EMAIL FAILED: {err}"
            print(f"[{now_str()}] {e['name']}: {err}", file=sys.stderr, flush=True)
        with store.lock:
            cur = next((x for x in store.entries if x["id"] == e["id"]), None)
            if cur is not None and store.rt(e["id"])["gen"] == gen_after:   # not re-enabled since
                cur["state"], cur["status"] = state, status
                store.save()


def scheduler(store, pool):
    while True:
        now = time.time()
        due = []
        with store.lock:
            for e in store.entries:
                rt = store.rt(e["id"])
                if e["enabled"] and not rt["busy"] and now >= rt["next"]:
                    rt["busy"] = True
                    rt["next"] = now + PING_INTERVAL
                    due.append((e["id"], e["target"], rt["gen"]))
        for job in due:
            pool.submit(run_ping, store, *job)
        time.sleep(1)


# ------------------------------------------------------------------ web server
class Handler(BaseHTTPRequestHandler):
    server_version = "ISPinger"
    store = None
    auth = None           # (user, password) or None

    def log_message(self, fmt, *args):
        pass               # keep the journal quiet; errors are printed explicitly

    def _send(self, code, payload, ctype="application/json; charset=utf-8"):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self):
        if not self.auth:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            user, _, pw = base64.b64decode(header[6:]).decode("utf-8").partition(":")
        except Exception:
            return False
        return (hmac.compare_digest(user.encode(), self.auth[0].encode())
                and hmac.compare_digest(pw.encode(), self.auth[1].encode()))

    def _handle(self, method):
        if not self._authorized():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="ISPinger", charset="UTF-8"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        path = urlparse(self.path).path.rstrip("/") or "/"
        if method == "GET" and path == "/":
            return self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        if not path.startswith("/api/"):
            return self._send(404, {"error": "Not found."})
        # Custom header forces a CORS preflight, so other websites can't drive the API
        if method != "GET" and self.headers.get("X-ISPinger") != "1":
            return self._send(403, {"error": "Missing X-ISPinger header."})

        body = {}
        if method in ("POST", "PUT"):
            length = int(self.headers.get("Content-Length") or 0)
            if length > 65536:
                return self._send(413, {"error": "Request too large."})
            if length:
                try:
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                except ValueError:
                    return self._send(400, {"error": "Invalid JSON."})
            if not isinstance(body, dict):
                return self._send(400, {"error": "Invalid request body."})
        try:
            code, result = self._route(method, path.split("/")[2:], body)
            self._send(code, result)
        except ApiError as e:
            self._send(e.code, {"error": e.msg})
        except Exception:
            traceback.print_exc()
            self._send(500, {"error": "Server error. Check the service log."})

    def _route(self, method, parts, body):
        s = self.store
        if parts == ["state"] and method == "GET":
            return 200, s.snapshot()
        if parts == ["entries"] and method == "POST":
            return 201, s.add_entry(body)
        if len(parts) == 2 and parts[0] == "entries":
            if method == "PUT":
                return 200, s.update_entry(parts[1], body)
            if method == "DELETE":
                s.delete_entry(parts[1])
                return 200, {"ok": True}
        if len(parts) == 3 and parts[0] == "entries" and method == "POST" and parts[2] in ("enable", "disable"):
            return 200, s.set_enabled(parts[1], parts[2] == "enable")
        if parts == ["contacts"] and method == "POST":
            return 201, s.add_contact(body)
        if len(parts) == 2 and parts[0] == "contacts":
            if method == "PUT":
                return 200, s.update_contact(parts[1], body)
            if method == "DELETE":
                s.delete_contact(parts[1])
                return 200, {"ok": True}
        if parts == ["settings"] and method == "PUT":
            s.update_settings(body)
            return 200, {"ok": True}
        if parts == ["settings", "test"] and method == "POST":
            return 200, {"sent_to": s.send_test()}
        raise ApiError("Not found.", 404)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")


# ------------------------------------------------------------------ web page
PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ISPinger</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Ccircle cx='16' cy='16' r='15' fill='%231E6FD9'/%3E%3Cpath d='M7.5 21.5a12 12 0 0 1 17 0M11 25a7 7 0 0 1 10 0' stroke='white' stroke-width='2.6' fill='none' stroke-linecap='round'/%3E%3Ccircle cx='16' cy='27.5' r='2' fill='white'/%3E%3C/svg%3E">
<style>
:root{color-scheme:light dark;--bg:#E9EEF2;--surface:#FFFFFF;--field:#F5F8FA;--ink:#18232D;--muted:#5B6976;--line:#D3DBE2;
  --accent:#1E6FD9;--accent-ink:#FFFFFF;--up:#1D8A5A;--bad:#C23B2A;--off:#A3AEB8;--radius:10px}
@media (prefers-color-scheme:dark){:root{--bg:#121A22;--surface:#1A2530;--field:#15202A;--ink:#E4EBF1;--muted:#93A3B1;
  --line:#2A3946;--accent:#5B9BEF;--accent-ink:#0E1620;--up:#3FBF84;--bad:#EB6B59;--off:#5E6E7C}}
*{box-sizing:border-box}
[hidden]{display:none!important}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.5 "Segoe UI Variable Text","Segoe UI",system-ui,-apple-system,Ubuntu,Cantarell,"Noto Sans",sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:0 20px}
header.top{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;padding:22px 0 14px}
.brand{display:flex;align-items:center;gap:10px}
.brand h1{font-size:1.35rem;font-weight:650;letter-spacing:-.01em;margin:0}
.summary{margin:0;color:var(--muted);font-variant-numeric:tabular-nums}
.tabs{display:flex;gap:4px;border-bottom:1px solid var(--line);overflow-x:auto}
.tabs button{appearance:none;background:none;border:0;border-bottom:2px solid transparent;margin-bottom:-1px;
  padding:10px 14px;font:inherit;color:var(--muted);cursor:pointer;white-space:nowrap}
.tabs button[aria-selected="true"]{color:var(--ink);border-bottom-color:var(--accent);font-weight:600}
main{padding:20px 0 64px}
.panel-head{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin-bottom:16px}
.panel-head .hint{margin:0}
.panel-head .btn{flex:none}
.hint{color:var(--muted);margin:0 0 16px;max-width:70ch}
.offline{background:var(--bad);color:#fff;padding:8px 12px;border-radius:8px;margin-top:14px}

.rows{list-style:none;margin:0;padding:0;background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);overflow:hidden}
.row{display:grid;grid-template-columns:40px minmax(150px,1.1fr) minmax(170px,1fr) minmax(210px,1.5fr) auto;
  gap:6px 16px;align-items:center;padding:12px 16px;border-top:1px solid var(--line)}
.row:first-child{border-top:0}
.contacts .row{grid-template-columns:minmax(150px,1fr) minmax(200px,1.4fr) auto}
.who strong{display:block;font-weight:600;overflow-wrap:anywhere}
.ip{color:var(--muted);font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.mail{color:var(--muted);overflow-wrap:anywhere}
.status span{display:block;overflow-wrap:anywhere}
.status small{color:var(--muted)}
.row.is-off .who strong,.row.is-off .status span{color:var(--muted)}
.row.is-up .status span{color:var(--up)}
.row.is-bad .status span{color:var(--bad)}
.actions{display:flex;gap:6px;justify-content:flex-end;flex-wrap:wrap}
.actions [data-act="toggle"]{min-width:78px}
.empty{padding:32px 16px;text-align:center;color:var(--muted)}

.lamp{position:relative;width:36px;height:36px;display:block}
.lamp svg{width:36px;height:36px;transform:rotate(-90deg);display:block}
.lamp .track{fill:none;stroke:var(--line);stroke-width:3}
.lamp .arc{fill:none;stroke:var(--accent);stroke-width:3;stroke-linecap:round;stroke-dasharray:94.25;transition:stroke-dashoffset 1s linear}
.lamp .core{fill:var(--accent)}
.is-off .lamp .arc{display:none}
.is-off .lamp .core{fill:var(--off)}
.is-up .lamp .core{fill:var(--up)}
.is-bad .lamp .core{fill:var(--bad)}
.lamp.pulse::after{content:"";position:absolute;inset:6px;border-radius:50%;border:2px solid var(--accent);animation:ping .9s ease-out}
@keyframes ping{from{transform:scale(.4);opacity:.9}to{transform:scale(1.8);opacity:0}}
@media (prefers-reduced-motion:reduce){.lamp.pulse::after{display:none}.lamp .arc{transition:none}}

.btn{appearance:none;font:inherit;font-size:.9rem;padding:6px 12px;border-radius:7px;border:1px solid var(--line);
  background:var(--surface);color:var(--ink);cursor:pointer}
.btn:hover{border-color:var(--muted)}
.btn:disabled{opacity:.6;cursor:default}
.btn.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-ink);font-weight:600}
.btn.danger{color:var(--bad)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}

.form{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);padding:20px;display:grid;gap:14px;max-width:640px}
.field{display:grid;gap:4px}
.field label{font-weight:600;font-size:.92rem}
.field input,.field select{font:inherit;padding:8px 10px;border-radius:7px;border:1px solid var(--line);background:var(--field);color:var(--ink);width:100%}
.field small{color:var(--muted)}
.two{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.form-actions{display:flex;gap:8px;flex-wrap:wrap}
.check{display:flex;gap:8px;align-items:center}
.help{color:var(--muted);max-width:640px;margin-top:20px}
.help ol{padding-left:1.3em;margin:6px 0}
.err{color:var(--bad);margin:0}
.err:empty{display:none}

dialog{border:1px solid var(--line);border-radius:12px;background:var(--surface);color:var(--ink);padding:0;width:min(460px,calc(100vw - 32px))}
dialog::backdrop{background:rgba(10,20,30,.45)}
dialog form{padding:20px;display:grid;gap:14px}
dialog h3{margin:0;font-size:1.05rem}
.toast{position:fixed;left:50%;bottom:24px;transform:translate(-50%,16px);background:var(--ink);color:var(--surface);
  padding:10px 16px;border-radius:8px;opacity:0;pointer-events:none;transition:opacity .2s,transform .2s;max-width:calc(100vw - 32px)}
.toast.show{opacity:1;transform:translate(-50%,0)}

@media (max-width:760px){
  .row,.contacts .row{grid-template-columns:40px 1fr;grid-template-areas:"lamp who" ". mail" ". status" ". actions"}
  .contacts .row{grid-template-columns:1fr;grid-template-areas:"who" "mail" "actions"}
  .lamp{grid-area:lamp}.who{grid-area:who}.mail{grid-area:mail}.status{grid-area:status}
  .actions{grid-area:actions;justify-content:flex-start}
  .two{grid-template-columns:1fr}
}
</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <div class="brand">
      <svg width="30" height="30" viewBox="0 0 32 32" aria-hidden="true"><circle cx="16" cy="16" r="15" fill="#1E6FD9"/><path d="M7.5 21.5a12 12 0 0 1 17 0M11 25a7 7 0 0 1 10 0" stroke="#fff" stroke-width="2.6" fill="none" stroke-linecap="round"/><circle cx="16" cy="27.5" r="2" fill="#fff"/></svg>
      <h1>ISPinger</h1>
    </div>
    <p class="summary" id="summary"></p>
  </header>

  <nav class="tabs" role="tablist">
    <button role="tab" data-tab="entries" aria-selected="true">Ping entries</button>
    <button role="tab" data-tab="contacts" aria-selected="false">Contacts</button>
    <button role="tab" data-tab="settings" aria-selected="false">Email settings</button>
  </nav>
  <div class="offline" id="offline" hidden>Can't reach the ISPinger server. Retrying every few seconds.</div>

  <main>
    <section id="tab-entries">
      <div class="panel-head"><p class="hint">Each enabled entry is pinged every 30 seconds. When it replies, ISPinger emails the address on the entry and turns the entry off.</p><button class="btn primary" id="addEntry">Add entry</button></div>
      <ul class="rows" id="entryList"></ul>
    </section>

    <section id="tab-contacts" hidden>
      <div class="panel-head"><p class="hint">Saved addresses you can pick when creating an entry. You can still type any address on an entry.</p><button class="btn primary" id="addContact">Add contact</button></div>
      <ul class="rows contacts" id="contactList"></ul>
    </section>

    <section id="tab-settings" hidden>
      <p class="hint">The mail account ISPinger sends alerts from.</p>
      <form class="form" id="settingsForm" autocomplete="off">
        <div class="field"><label for="sHost">SMTP server</label><input id="sHost" placeholder="smtp.gmail.com"></div>
        <div class="two">
          <div class="field"><label for="sSec">Security</label>
            <select id="sSec"><option value="starttls">STARTTLS (port 587)</option><option value="ssl">SSL/TLS (port 465)</option><option value="none">None</option></select></div>
          <div class="field"><label for="sPort">Port</label><input id="sPort" type="number" min="1" max="65535"></div>
        </div>
        <div class="field"><label for="sUser">Username</label><input id="sUser" placeholder="you@gmail.com"></div>
        <div class="field"><label for="sPass">Password</label><input id="sPass" type="password" autocomplete="new-password"><small id="sPassNote"></small></div>
        <div class="field"><label for="sFrom">From address</label><input id="sFrom" type="email" placeholder="you@gmail.com"></div>
        <p class="err" id="settingsErr"></p>
        <div class="form-actions">
          <button class="btn primary" type="submit">Save settings</button>
          <button class="btn" type="button" id="testEmail">Send test email</button>
        </div>
      </form>
      <div class="help">
        <strong>Free Gmail setup</strong>
        <ol>
          <li>Turn on 2-Step Verification on the Google account.</li>
          <li>Create an app password at myaccount.google.com/apppasswords.</li>
          <li>Use smtp.gmail.com with STARTTLS on port 587. Username and From address are your Gmail address. Password is the 16-character app password.</li>
        </ol>
        The test email goes to the From address.
      </div>
    </section>
  </main>
</div>

<dialog id="entryDialog">
  <form id="entryForm" novalidate>
    <h3 id="entryTitle">New entry</h3>
    <div class="field"><label for="eName">Name</label><input id="eName" maxlength="80" placeholder="R730xd iDRAC"></div>
    <div class="field"><label for="eTarget">IP address or hostname</label><input id="eTarget" placeholder="192.168.1.50" inputmode="url" autocapitalize="off" spellcheck="false"></div>
    <div class="field"><label for="eEmail">Email to notify</label><input id="eEmail" list="contactEmails" type="email" placeholder="Pick a contact or type an address"></div>
    <label class="check"><input type="checkbox" id="eEnabled" checked> Start pinging when saved</label>
    <p class="err" id="entryErr"></p>
    <div class="form-actions">
      <button class="btn primary" type="submit">Save entry</button>
      <button class="btn" type="button" data-close>Cancel</button>
    </div>
  </form>
</dialog>

<dialog id="contactDialog">
  <form id="contactForm" novalidate>
    <h3 id="contactTitle">New contact</h3>
    <div class="field"><label for="cName">Name</label><input id="cName" maxlength="80"></div>
    <div class="field"><label for="cEmail">Email</label><input id="cEmail" type="email"></div>
    <p class="err" id="contactErr"></p>
    <div class="form-actions">
      <button class="btn primary" type="submit">Save contact</button>
      <button class="btn" type="button" data-close>Cancel</button>
    </div>
  </form>
</dialog>

<datalist id="contactEmails"></datalist>
<div class="toast" id="toast" role="status" aria-live="polite"></div>

<script>
const $ = s => document.querySelector(s);
const C = 94.25;   // circumference of the countdown ring
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let state = {entries: [], contacts: [], settings: {}, interval: 30};
let deadlines = {};
let settingsFilled = false;

async function api(method, path, body) {
  const r = await fetch(path, {
    method,
    headers: {'Content-Type': 'application/json', 'X-ISPinger': '1'},
    body: body === undefined ? undefined : JSON.stringify(body)
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `Request failed (${r.status})`);
  return data;
}

let toastTimer;
function toast(msg) {
  const t = $('#toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), 3200);
}

async function refresh() {
  try {
    state = await api('GET', '/api/state');
    $('#offline').hidden = true;
  } catch (e) {
    $('#offline').hidden = false;
    return;
  }
  const now = Date.now();
  deadlines = {};
  for (const e of state.entries) if (e.next_in != null) deadlines[e.id] = now + e.next_in * 1000;
  render();
}

function rowClass(e) {
  const cls = [];
  if (!e.enabled) cls.push('is-off');
  if (e.state === 'reached') cls.push('is-up');
  if (e.state === 'failed' || e.state === 'error') cls.push('is-bad');
  return cls.join(' ');
}

function arcOffset(id) {
  const d = deadlines[id];
  if (d == null) return C;
  const rem = Math.max(0, (d - Date.now()) / 1000);
  return (C * (1 - rem / state.interval)).toFixed(2);
}

function render() {
  const on = state.entries.filter(e => e.enabled).length;
  $('#summary').textContent = state.entries.length
    ? `${on} of ${state.entries.length} ${state.entries.length === 1 ? 'entry' : 'entries'} watching`
    : '';

  $('#entryList').innerHTML = state.entries.length ? state.entries.map(e => `
    <li class="row ${rowClass(e)}" data-id="${esc(e.id)}">
      <span class="lamp" aria-hidden="true"><svg viewBox="0 0 36 36">
        <circle class="track" cx="18" cy="18" r="15"/>
        <circle class="arc" cx="18" cy="18" r="15" data-id="${esc(e.id)}" style="stroke-dashoffset:${arcOffset(e.id)}"/>
        <circle class="core" cx="18" cy="18" r="6"/></svg></span>
      <div class="who"><strong>${esc(e.name)}</strong><span class="ip">${esc(e.target)}</span></div>
      <div class="mail">${esc(e.email)}</div>
      <div class="status"><span>${esc(e.status)}</span>${e.last_checked ? `<small>Last checked ${esc(e.last_checked)}</small>` : ''}</div>
      <div class="actions">
        <button class="btn" data-act="toggle">${e.enabled ? 'Disable' : 'Enable'}</button>
        <button class="btn" data-act="edit">Edit</button>
        <button class="btn danger" data-act="remove">Remove</button>
      </div>
    </li>`).join('')
    : '<li class="empty">No ping entries yet. Add one to start watching an IP.</li>';

  $('#contactList').innerHTML = state.contacts.length ? state.contacts.map(c => `
    <li class="row" data-id="${esc(c.id)}">
      <div class="who"><strong>${esc(c.name)}</strong></div>
      <div class="mail">${esc(c.email)}</div>
      <div class="actions">
        <button class="btn" data-act="edit">Edit</button>
        <button class="btn danger" data-act="remove">Remove</button>
      </div>
    </li>`).join('')
    : '<li class="empty">No contacts yet. Add the addresses you send alerts to most.</li>';

  $('#contactEmails').innerHTML = state.contacts.map(c => `<option value="${esc(c.email)}">${esc(c.name)}</option>`).join('');

  const s = state.settings;
  $('#sPassNote').textContent = s.has_password ? 'Saved. Leave blank to keep it.' : 'Not set yet.';
  if (!settingsFilled) {
    $('#sHost').value = s.smtp_host || '';
    $('#sPort').value = s.smtp_port || 587;
    $('#sSec').value = s.security || 'starttls';
    $('#sUser').value = s.username || '';
    $('#sFrom').value = s.from_address || '';
    settingsFilled = true;
  }
}

// Countdown rings and the ping pulse
setInterval(() => {
  document.querySelectorAll('.arc[data-id]').forEach(el => {
    const id = el.dataset.id;
    if (deadlines[id] == null) return;
    el.style.strokeDashoffset = arcOffset(id);
    if (Date.now() >= deadlines[id] && !el.dataset.pulsed) {
      el.dataset.pulsed = '1';
      const lamp = el.closest('.lamp');
      lamp.classList.add('pulse');
      setTimeout(() => lamp.classList.remove('pulse'), 900);
      setTimeout(refresh, 1500);
    }
  });
}, 1000);
setInterval(refresh, 5000);

// Tabs
function showTab(name) {
  document.querySelectorAll('.tabs button').forEach(b => b.setAttribute('aria-selected', String(b.dataset.tab === name)));
  for (const t of ['entries', 'contacts', 'settings']) $('#tab-' + t).hidden = t !== name;
  history.replaceState(null, '', '#' + name);
}
document.querySelectorAll('.tabs button').forEach(b => b.addEventListener('click', () => showTab(b.dataset.tab)));
showTab(['entries', 'contacts', 'settings'].includes(location.hash.slice(1)) ? location.hash.slice(1) : 'entries');

document.querySelectorAll('[data-close]').forEach(b => b.addEventListener('click', () => b.closest('dialog').close()));

// Entries
function openEntry(e) {
  $('#entryForm').dataset.id = e ? e.id : '';
  $('#entryTitle').textContent = e ? 'Edit entry' : 'New entry';
  $('#eName').value = e ? e.name : '';
  $('#eTarget').value = e ? e.target : '';
  $('#eEmail').value = e ? e.email : '';
  $('#eEnabled').checked = e ? e.enabled : true;
  $('#entryErr').textContent = '';
  $('#entryDialog').showModal();
  $('#eName').focus();
}
$('#addEntry').addEventListener('click', () => openEntry(null));

$('#entryForm').addEventListener('submit', async ev => {
  ev.preventDefault();
  const id = ev.target.dataset.id;
  const body = {name: $('#eName').value, target: $('#eTarget').value, email: $('#eEmail').value, enabled: $('#eEnabled').checked};
  try {
    if (id) await api('PUT', `/api/entries/${encodeURIComponent(id)}`, body);
    else await api('POST', '/api/entries', body);
    $('#entryDialog').close();
    toast(id ? 'Entry saved' : 'Entry added');
    refresh();
  } catch (e) { $('#entryErr').textContent = e.message; }
});

$('#entryList').addEventListener('click', async ev => {
  const b = ev.target.closest('button[data-act]');
  if (!b) return;
  const id = b.closest('li').dataset.id;
  const e = state.entries.find(x => x.id === id);
  if (!e) return;
  try {
    if (b.dataset.act === 'edit') return openEntry(e);
    if (b.dataset.act === 'toggle') {
      await api('POST', `/api/entries/${encodeURIComponent(id)}/${e.enabled ? 'disable' : 'enable'}`);
      toast(e.enabled ? `${e.name} disabled` : `${e.name} enabled`);
    }
    if (b.dataset.act === 'remove') {
      if (!confirm(`Remove "${e.name}"?`)) return;
      await api('DELETE', `/api/entries/${encodeURIComponent(id)}`);
      toast('Entry removed');
    }
    refresh();
  } catch (err) { toast(err.message); }
});

// Contacts
function openContact(c) {
  $('#contactForm').dataset.id = c ? c.id : '';
  $('#contactTitle').textContent = c ? 'Edit contact' : 'New contact';
  $('#cName').value = c ? c.name : '';
  $('#cEmail').value = c ? c.email : '';
  $('#contactErr').textContent = '';
  $('#contactDialog').showModal();
  $('#cName').focus();
}
$('#addContact').addEventListener('click', () => openContact(null));

$('#contactForm').addEventListener('submit', async ev => {
  ev.preventDefault();
  const id = ev.target.dataset.id;
  const body = {name: $('#cName').value, email: $('#cEmail').value};
  try {
    if (id) await api('PUT', `/api/contacts/${encodeURIComponent(id)}`, body);
    else await api('POST', '/api/contacts', body);
    $('#contactDialog').close();
    toast(id ? 'Contact saved' : 'Contact added');
    refresh();
  } catch (e) { $('#contactErr').textContent = e.message; }
});

$('#contactList').addEventListener('click', async ev => {
  const b = ev.target.closest('button[data-act]');
  if (!b) return;
  const id = b.closest('li').dataset.id;
  const c = state.contacts.find(x => x.id === id);
  if (!c) return;
  try {
    if (b.dataset.act === 'edit') return openContact(c);
    if (b.dataset.act === 'remove') {
      if (!confirm(`Remove contact "${c.name}"?`)) return;
      await api('DELETE', `/api/contacts/${encodeURIComponent(id)}`);
      toast('Contact removed');
      refresh();
    }
  } catch (err) { toast(err.message); }
});

// Settings
$('#sSec').addEventListener('change', () => {
  const port = $('#sPort');
  const defaults = {starttls: 587, ssl: 465, none: 25};
  if (['587', '465', '25', ''].includes(port.value)) port.value = defaults[$('#sSec').value];
});

async function saveSettings() {
  await api('PUT', '/api/settings', {
    smtp_host: $('#sHost').value, smtp_port: $('#sPort').value, security: $('#sSec').value,
    username: $('#sUser').value, password: $('#sPass').value, from_address: $('#sFrom').value
  });
  $('#sPass').value = '';
  $('#settingsErr').textContent = '';
}

$('#settingsForm').addEventListener('submit', async ev => {
  ev.preventDefault();
  try { await saveSettings(); toast('Settings saved'); refresh(); }
  catch (e) { $('#settingsErr').textContent = e.message; }
});

$('#testEmail').addEventListener('click', async () => {
  const b = $('#testEmail');
  b.disabled = true;
  b.textContent = 'Sending...';
  try {
    await saveSettings();
    const r = await api('POST', '/api/settings/test');
    toast(`Test email sent to ${r.sent_to}`);
    refresh();
  } catch (e) { $('#settingsErr').textContent = e.message; }
  finally { b.disabled = false; b.textContent = 'Send test email'; }
});

refresh();
</script>
</body>
</html>
"""


# ------------------------------------------------------------------ main
def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="ISPinger web server")
    ap.add_argument("--host", default="0.0.0.0", help="address to listen on (default 0.0.0.0)")
    ap.add_argument("--port", type=int, default=8080, help="port to listen on (default 8080)")
    ap.add_argument("--data", default=os.path.join(here, "ispinger-data.json"), help="path to the data file")
    args = ap.parse_args()

    store = Store(args.data)
    user, pw = os.environ.get("ISPINGER_USER", ""), os.environ.get("ISPINGER_PASSWORD", "")
    Handler.store = store
    Handler.auth = (user, pw) if user and pw else None

    pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="ping")
    threading.Thread(target=scheduler, args=(store, pool), daemon=True).start()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    print(f"ISPinger running on http://{args.host}:{args.port}  (data: {args.data})", flush=True)
    if not Handler.auth:
        print("WARNING: no web login set. Set ISPINGER_USER and ISPINGER_PASSWORD to require one.", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
