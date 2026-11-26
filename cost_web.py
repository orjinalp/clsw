#!/usr/bin/python3
"""clsw cost — web arayüzü. `clsw cost`un hesabını (claudeswap'taki cost_rows) JSON olarak verir, sayfa
(cost_web.html) süzgeç, grafik ve tabloyu tarayıcıda çizer.

Varsayılan olarak yalnız bu makineden erişilir (127.0.0.1). Yalnız okur: oturum kayıtları ve clsw dosyaları.
Başka makinelerden erişim için: CLSW_WEB_HOST=0.0.0.0, CLSW_WEB_ALLOW=<ağ/maske,…> (ör. 192.0.2.0/24) ve
tarayıcıda yazılacak adlar CLSW_WEB_NAMES=<ad,…>. İstek Host başlığı bu adlardan biri değilse reddedilir
(başka bir sitenin DNS rebinding ile 127.0.0.1'deki veriyi okuması engellenir). Bağlantı şifresizdir.
"""
import gzip
import ipaddress
import json
import os
import re
import runpy
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HOME = os.path.expanduser("~")
HERE = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "cost_web.html")
CLAUDESWAP = os.path.join(HOME, ".local", "bin", "claudeswap")
PORT = int(os.environ.get("CLSW_WEB_PORT", "8790"))
HOST = os.environ.get("CLSW_WEB_HOST", "127.0.0.1")
ALLOWED = [ipaddress.ip_network(n.strip(), strict=False)
           for n in ("127.0.0.0/8,::1/128," + os.environ.get("CLSW_WEB_ALLOW", "")).split(",") if n.strip()]
NAMES = {"127.0.0.1", "localhost", "::1"} | {n.strip().lower() for n in
                                             (os.environ.get("CLSW_WEB_NAMES", "") + "," + HOST).split(",") if n.strip()}
MAX_SPAN = 31 * 86400                 # tek istekte en geniş aralık (ilk tarama 30 günde ~1 dk sürer)
MEMO_SEC = 15                         # aynı aralık bu kadar saniye içinde tekrar istenirse yeniden hesaplanmaz
GROUP = {"p": "p", "terminal": "terminal", "kanal": "kanal", "zamanlayici": "zamanlayici"}   # grafik grupları; kalanlar «diğer»

G = None                              # claudeswap işlevlerinin gördüğü global sözlük (_cache burada)
_mtime = None
_lock = threading.Lock()
_memo = {}
_embedded = False


def attach(g):
    """clsw web: claudeswap kendi işlevlerini verir; dosyadan yüklenmez."""
    global G, _embedded
    G, _embedded = g, True


def load_claudeswap():
    """claudeswap'ı yükler; dosya değişmişse yeniden (servisi yeniden başlatmadan yeni kod)."""
    global G, _mtime
    if _embedded:
        return
    m = os.path.getmtime(CLAUDESWAP)
    if G is None or m != _mtime:
        G = runpy.run_path(CLAUDESWAP)["main"].__globals__
        _mtime = m
        _memo.clear()


def fresh_cache():
    """İstek başına: hesap adları ve yedek özeti yeniden okunsun."""
    G["_cache"].clear()


def short_model(m):
    return G["short_model"](m)


def row_json(r):
    models = {}
    for m, c in (r.get("models") or {}).items():
        k = short_model(m)
        models[k] = models.get(k, 0.0) + c
    top = max(models, key=models.get) if models else "-"
    return {
        "ts": r["ts"], "end": r["end"] or r["ts"], "src": r["src"], "group": GROUP.get(r["src"], "diger"),
        "tok": list(r.get("tokens") or [0, 0, 0, 0]),
        "who": G["row_who"](r), "text": G["row_text"](r), "usd": round(r["usd"], 6),
        "model": top, "models": {k: round(v, 6) for k, v in models.items()},
        "accts": [G["acct_label"](a) for a in r.get("accts") or []] or ["?"],
        "running": bool(r.get("running")), "exact": bool(r.get("exact", True)),
        "slot": r["slot"], "sid": r["sid"], "cwd": G["tilde"](r.get("cwd") or "") or "?", "job": r.get("job"),
        "channel": bool(r.get("channel")),
    }


def swap_json(e):
    order = {s["name"]: i for i, s in enumerate(G["SLOTS"])}
    return {"ts": e["ts"], "profiller": [[n] + (list(e["profiller"][n]) + [None, None])[:2]
                                         for n in sorted(e["profiller"], key=lambda n: order.get(n, 99))]}


def build(since, until, swap=None):
    """since/until: epoch sn (yerel saat dilimi tarayıcıda). until None → şimdiye kadar (canlı).
    swap=n: aralık sondan n. hesap geçişinden şimdiye (since yok sayılır; yeni geçiş olunca aralık kayar)."""
    clamped = False
    if swap:
        load_claudeswap()
        e, _total = G["swap_since"](swap)
        since = e["ts"] if e else time.time() - 86400
        if time.time() - since > MAX_SPAN:
            since, clamped = time.time() - MAX_SPAN, True
        until = None
    key = (round(since), round(until) if until else None)
    now = time.time()
    for k in [k for k, v in _memo.items() if now - v[0] > 60]:
        del _memo[k]
    hit = _memo.get(key)
    if hit and now - hit[0] < MEMO_SEC:
        return hit[1]
    with _lock:
        hit = _memo.get(key)
        if hit and time.time() - hit[0] < MEMO_SEC:
            return hit[1]
        t0 = time.time()
        load_claudeswap()
        fresh_cache()
        rows = G["cost_rows"](since, until)
        end = until or time.time()
        all_ev = G["swap_events"]()
        events = [swap_json(e) for e in all_ev if since <= e["ts"] < end]
        data = {"since": since, "until": end, "live": until is None, "generated": time.time(),
                "rows": [row_json(r) for r in rows], "events": events,
                "swaps": [swap_json(e) for e in all_ev[-3:]],          # zaman seçicideki «son N geçişten beri» için
                "clamped": clamped, "elapsed": round(time.time() - t0, 2)}
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
        _memo[key] = (time.time(), body)
        return body


def parse_range(q):
    """?since=<epoch>&until=<epoch|boş> → (since, until, None); ?swap=<1-9> → (0, None, n); geçersizse None."""
    raw = (q.get("swap") or [""])[0]
    if raw:
        return (0.0, None, int(raw)) if raw.isdigit() and 1 <= int(raw) <= 9 else None
    try:
        since = float((q.get("since") or [""])[0])
        raw = (q.get("until") or [""])[0]
        until = float(raw) if raw else None
    except ValueError:
        return None
    now = time.time()
    if until is not None and until >= now:
        until = None                                  # geleceğe uzanan bitiş = canlı
    end = until or now
    if not (0 < since < end) or end - since > MAX_SPAN:
        return None
    return since, until, None


class Handler(BaseHTTPRequestHandler):
    server_version = "clsw-cost"

    def host_ok(self):
        """Host başlığı: bilinen ad ve bu port. Yoksa (DNS rebinding) reddedilir."""
        host = (self.headers.get("Host") or "").strip().lower()
        m = re.fullmatch(r"\[([0-9a-f:]+)\](?::(\d+))?|([^:\[\]]+)(?::(\d+))?", host)
        if not m:
            return False
        name, port = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        return name in NAMES and (port is None or port == str(PORT))

    def allowed(self):
        if not self.host_ok():
            return False
        try:
            ip = ipaddress.ip_address(self.client_address[0].split("%")[0])
        except ValueError:
            return False
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        return any(ip in n for n in ALLOWED)

    def send(self, code, body, ctype, cache="no-store"):
        if "gzip" in (self.headers.get("Accept-Encoding") or "") and len(body) > 2048:
            body = gzip.compress(body, 5)
            gz = True
        else:
            gz = False
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                         "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
        if gz:
            self.send_header("Content-Encoding", "gzip")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.allowed():
            self.send(403, "Bu adresten erişim kapalı (CLSW_WEB_ALLOW).\n".encode(), "text/plain; charset=utf-8")
            return
        url = urlparse(self.path)
        try:
            if url.path in ("/", "/index.html"):
                with open(PAGE, "rb") as f:
                    self.send(200, f.read(), "text/html; charset=utf-8")
            elif url.path == "/api/cost":
                rng = parse_range(parse_qs(url.query))
                if rng is None:
                    self.send(400, json.dumps({"error": "Geçersiz aralık: başlangıç bitişten önce olmalı, "
                                               "en çok 31 gün."}, ensure_ascii=False).encode(),
                              "application/json; charset=utf-8")
                    return
                self.send(200, build(*rng), "application/json; charset=utf-8")
            elif url.path == "/health":
                self.send(200, b"ok\n", "text/plain")
            else:
                self.send(404, b"not found\n", "text/plain")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # sayfa bir hata metni görsün, servis ayakta kalsın
            self.log_error("hata: %r", e)
            try:
                self.send(500, json.dumps({"error": type(e).__name__}).encode(), "application/json")
            except OSError:
                pass

    def log_request(self, code="-", size="-"):  # başarılı istekler loglanmaz (otomatik yenileme dakikada bir gelir)
        if str(getattr(code, "value", code))[:1] in ("4", "5"):
            super().log_request(code, size)


def main(ready=None):
    load_claudeswap()
    try:
        srv = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as e:
        sys.stderr.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} clsw web açılamadı: {HOST}:{PORT} — "
                         f"{e.strerror or e} (port başka bir süreçte olabilir)\n")
        sys.exit(1)
    srv.daemon_threads = True
    shown = "127.0.0.1" if HOST in ("0.0.0.0", "::") else HOST
    sys.stderr.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} clsw web: http://{shown}:{PORT}\n")
    if HOST not in ("127.0.0.1", "localhost", "::1"):
        sys.stderr.write("UYARI: clsw web bu makinenin dışına açık (CLSW_WEB_HOST={}). Bağlantı şifresiz ve giriş "
                         "yok; yalnız güvenilir bir VPN içinde kullan. İzinli ağlar: {}\n".format(
                             HOST, ", ".join(str(n) for n in ALLOWED)))
    sys.stderr.flush()
    if ready:
        ready()
    srv.serve_forever()


if __name__ == "__main__":
    main()
