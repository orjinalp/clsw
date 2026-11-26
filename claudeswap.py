#!/usr/bin/env python3
# claudeswap — claude, claude2, claude3 … profillerinin hesaplarını yer değiştirir.
# Yalnızca hesap girişi ve hesap bilgisi taşınır; ayarlar, plugin'ler, hafıza,
# geçmiş ve projeler kendi klasöründe kalır.
import glob
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

try:
    import fcntl
except ImportError:          # Windows
    fcntl = None
    import msvcrt

__version__ = "1.2.0"

# Derlenmiş programın (PyInstaller) OpenSSL'i sertifika listesini derlendiği makinedeki yolda arar; o yol kullanıcının
# makinesinde yoksa her HTTPS isteği "sertifika doğrulanamadı" ile düşer. O durumda sistemin listesi gösterilir.
# Windows'ta Python sistem sertifika deposunu kendisi okur.
CA_FILES = ("/etc/ssl/cert.pem",                                  # macOS, Alpine, Arch
            "/etc/ssl/certs/ca-certificates.crt",                 # Debian, Ubuntu
            "/etc/pki/tls/certs/ca-bundle.crt",                   # Fedora, RHEL
            "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",
            "/etc/ssl/ca-bundle.pem")                             # openSUSE


def use_system_certs(env=None):
    """Kendi sertifika yolu yoksa SSL_CERT_FILE'ı sistemin listesine yönlendirir; seçilen yolu ya da None döner."""
    import ssl
    env = os.environ if env is None else env
    if os.name == "nt" or env.get("SSL_CERT_FILE"):
        return None
    paths = ssl.get_default_verify_paths()
    if (paths.cafile and os.path.isfile(paths.cafile)) or (paths.capath and os.path.isdir(paths.capath)
                                                            and os.listdir(paths.capath)):
        return None
    for f in CA_FILES:
        if os.path.isfile(f):
            env["SSL_CERT_FILE"] = f
            return f
    return None


use_system_certs()

HOME = os.path.expanduser("~")


def _slot(name, cfg_dir, global_json, keychain=None, config_env=False):
    """keychain: macOS'ta girişin tutulduğu Anahtar Zinciri servisi (yoksa yalnız .credentials.json).
    config_env: claude bu profil için CLAUDE_CONFIG_DIR ile çalışıyor (claude -p'ye de verilir)."""
    return {"name": name, "dir": cfg_dir, "json": global_json,
            "creds": os.path.join(cfg_dir, ".credentials.json"), "keychain": keychain, "config_env": config_env}


def keychain_service(cfg_dir):
    """Claude Code'un macOS'ta girişi yazdığı Anahtar Zinciri servisi: CLAUDE_CONFIG_DIR verildiyse
    "Claude Code-credentials-<sha256(yol)[:8]>", verilmediyse "Claude Code-credentials"."""
    if sys.platform != "darwin":
        return None
    if cfg_dir is None:
        return "Claude Code-credentials"
    import hashlib
    return "Claude Code-credentials-" + hashlib.sha256(cfg_dir.encode()).hexdigest()[:8]


def _home_uses_config_dir(cfg_dir, home_json, env=None):
    """`claude` ~/.claude'u CLAUDE_CONFIG_DIR ile mi açıyor? (öyleyse ayarı ~/.claude/.claude.json'da)
    Ortamda verilmişse ona bakılır; verilmemişse (cron, başka kabuk) hangi .claude.json daha yeniyse o."""
    env = os.environ if env is None else env
    val = env.get("CLAUDE_CONFIG_DIR")
    if val:
        return os.path.realpath(os.path.expanduser(val)) == os.path.realpath(cfg_dir)
    inner = os.path.join(cfg_dir, ".claude.json")
    try:
        t_inner = os.path.getmtime(inner)
    except OSError:
        return False
    try:
        return t_inner > os.path.getmtime(home_json)
    except OSError:
        return True


PROFILE_DIR = re.compile(r"\.claude(\d+)$")
PROFILE_CMD = re.compile(r"claude(\d+)(\.cmd)?$", re.I)
BASE_PROFILES = 10                   # claude … claude10 her zaman tanınır; fazlası diskte varsa


def profile_numbers():
    """Diskte olan profil numaraları (≥2): ~/.claudeN klasörleri ve ~/.local/bin/claudeN komutları."""
    nums = set()
    for folder, rx in ((HOME, PROFILE_DIR), (os.path.join(HOME, ".local", "bin"), PROFILE_CMD)):
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for n in names:
            m = rx.fullmatch(n)
            if m and int(m.group(1)) >= 2:
                nums.add(int(m.group(1)))
    return nums


def _make_slots():
    home_dir, home_json = os.path.join(HOME, ".claude"), os.path.join(HOME, ".claude.json")
    if _home_uses_config_dir(home_dir, home_json):
        first = _slot("claude", home_dir, os.path.join(home_dir, ".claude.json"),
                      keychain_service(home_dir), config_env=True)
    else:
        first = _slot("claude", home_dir, home_json, keychain_service(None))
    rest = []
    for i in range(2, max([BASE_PROFILES] + sorted(profile_numbers())) + 1):
        d = os.path.join(HOME, f".claude{i}")
        rest.append(_slot(f"claude{i}", d, os.path.join(d, ".claude.json"), keychain_service(d), config_env=True))
    return [first] + rest


# `claude`  → ~/.claude  + ~/.claude.json (ana dizinde); kabukta CLAUDE_CONFIG_DIR=~/.claude ise ~/.claude/.claude.json
# `claude2` → ~/.claude2 + ~/.claude2/.claude.json (CLAUDE_CONFIG_DIR ile)
# Giriş: macOS'ta Anahtar Zinciri (Claude Code oraya yazar; açılamazsa .credentials.json), diğerlerinde dosya.
SLOTS = _make_slots()


# .claude.json içindeki hesaba bağlı alanlar — hesapla birlikte taşınır.
ACCOUNT_KEYS = [
    "oauthAccount", "cachedUsageUtilization", "cachedExtraUsageDisabledReason",
    "hasAvailableSubscription", "subscriptionNoticeCount", "orgModelDefaultCache",
    "penguinModeOrgEnabled", "claudeCodeFirstTokenDate",
]

STATE_ROOT = os.path.join(HOME, ".claudeswap")
BACKUP_ROOT = os.path.join(STATE_ROOT, "backups")
KEEP_BACKUPS = 10
API = "https://api.anthropic.com/api/oauth/"
MISSING = object()

HELP = """\
clsw — Claude Code profilleri (claude, claude2, claude3 …) arasında hesap değiştirir.

Kullanım:
  clsw <hesap>              E-posta ya da kullanıcı adıyla hesabı claude'a getir
  clsw claude3              claude ile claude3'ün hesaplarını değiştir
  clsw claude2 claude4      İki profilin hesaplarını değiştir
  clsw empty                İlk giriş yapılmamış profili claude ile değiştir
  clsw generate 3           3 yeni profil aç (sıradaki numaralarla: claude7, claude8 …)
  clsw list                 Hesaplar, limitler, reset hakları
  clsw process              Açık Claude oturumları; eş: ps, oturum
  clsw cost                 İstem / iş başına maliyet (son 24 saat); eş: maliyet
  clsw web                  Maliyet web arayüzü (http://127.0.0.1:8790)
  clsw update               Son sürümü kur; eş: güncelle (--check: yalnız bak)
  clsw version              Kurulu sürüm; eş: sürüm, --version
  clsw help                 Bu yardım (tek başına clsw de gösterir)
  Uzun ad claudeswap da aynen çalışır.

Hesap değiştirme:
  Yalnız giriş (macOS'ta Anahtar Zinciri, yoksa .credentials.json) ve hesaba bağlı ayar alanları
  taşınır; ayarlar, plugin'ler, hafıza, geçmiş ve projeler kendi profilinde kalır.
  Boş profile de taşınabilir: hesap oraya geçer, claude giriş yapılmamış kalır (yeni hesap: /login).
  Her değişimden önce yedek: ~/.claudeswap/backups/<zaman>/ · geri almak: aynı iki profili tekrar değiştir.
  Açık oturumları yeni hesap için yeniden başlat.
  5 saatlik penceresi başlamamış hesaba küçük bir istek gönderilip pencere başlatılır
  (kapatmak: CLAUDESWAP_NO_ACTIVATE=1).

Açık oturumlar (clsw process):
  Her claude sürecinin profili, klasörü, durumu ve nasıl açıldığı (terminal, claude -p, kanal):
  ›  son istem (kanal oturumunda son gelen mesaj)
  ≡  claude -p işinin görevi
  ↳  başka bir claude oturumunun alt süreciyse hangisi
  ▸  son adım (çalışıyorsa şu an yaptığı)
  Token benzeri değerler gizlenir; yalnız okunur.

Maliyet (clsw cost):
  clsw cost 3g | 12sa | bugün | dün     zaman aralığı
  clsw cost geçiş | geçiş2 | geçiş3     son (2., 3.) hesap geçişinden beri
  clsw cost hepsi | clsw cost 200       satır sınırı (varsayılan: en yeni 60)
  clsw cost oturum                      oturum başına tek satır
  clsw cost terminal | p | kanal        yalnız o kaynak
  Tutar, abonelik kullanımının API fiyatıyla karşılığıdır; ⇄ satırı clsw ile yapılan hesap değişimidir.

Güncelleme:
  Terminalde açılınca yeni sürüm varsa notlarını gösterip sorar; npm ya da Homebrew ile
  kurulduysa onunla günceller. Kapatmak: CLSW_NO_UPDATE=1 · renkleri kapatmak: NO_COLOR=1
"""

if os.name == "nt":
    # Yönlendirilen çıktı cp1254'e düşmesin; konsolda ANSI renkleri açılsın.
    for _st in (sys.stdout, sys.stderr):
        if hasattr(_st, "reconfigure"):
            _st.reconfigure(encoding="utf-8", errors="replace")
    os.system("")

USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
ANSI = re.compile(r"\033\[[0-9;]*m")


def _c(code):
    return lambda s: f"\033[{code}m{s}\033[0m" if USE_COLOR else s


bold, dim, red, green, yellow = _c("1"), _c("2"), _c("31"), _c("32"), _c("33")
cyan, magenta = _c("36"), _c("35")


def render_help(text):
    """Yardım metnini renklendirir: başlıklar, komutlar, simgeler, ortam değişkenleri ve yollar."""
    if not USE_COLOR:
        return text
    out = []
    for i, line in enumerate(text.splitlines()):
        if i == 0:
            name, _, rest = line.partition(" — ")
            out.append(bold(cyan(name)) + dim(" — ") + rest)
            continue
        if line and not line.startswith(" ") and line.endswith(":"):
            out.append(bold(yellow(line)))
            continue
        m = re.match(r"^(  )((?:clsw|claudeswap)\b.*?)(\s{2,})(.*)$", line)
        if m:
            desc = re.sub(r"(; eş: .*|\(--check: yalnız bak\))", lambda x: dim(x.group(1)), m.group(4))
            cmd = re.sub(r"(<[^>]+>)", lambda x: magenta(x.group(1)), m.group(2))
            cmd = cmd.replace(" | ", dim(" | "))
            out.append(m.group(1) + green(cmd) + m.group(3) + desc)
            continue
        m = re.match(r"^(  )([›≡↳▸])(.*)$", line)
        if m:
            out.append(m.group(1) + cyan(m.group(2)) + m.group(3))
            continue
        line = re.sub(r"\b([A-Z][A-Z_]{3,}=1)\b", lambda x: yellow(x.group(1)), line)
        line = re.sub(r"(~/[^\s,;)]+|https?://\S+)", lambda x: dim(x.group(1)), line)
        out.append(line)
    return "\n".join(out) + "\n"


def vlen(s):
    return len(ANSI.sub("", s))


def tilde(p):
    if p and (p == HOME or p.startswith(HOME + "/")):
        return "~" + p[len(HOME):]
    return p


def die(msg, code=1):
    print(red("Hata: ") + msg, file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------- dosya işleri

def read_bytes(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None


def parse_json(raw):
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None


def atomic_write(path, data, private=False):
    """data: bytes; None ise dosya silinir. Yazma geçici dosya + rename ile yapılır.
    private: yalnız sahibi okuyabilsin (0600), eski dosyanın izni gevşek olsa da."""
    if data is None:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        mode = 0o600
    if private:
        mode = 0o600
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".claudeswap-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except FileNotFoundError:
            pass
        raise


class Lock:
    """`<dosya>.lock` dizini ile kilit (Claude Code'un config kilidiyle uyumlu)."""

    def __init__(self, target):
        self.path = target + ".lock"

    def __enter__(self):
        deadline = time.time() + 15
        while True:
            try:
                os.mkdir(self.path)
                return self
            except FileExistsError:
                if time.time() > deadline:
                    die(f"{tilde(self.path)} kilidi alınamadı (başka bir işlem tutuyor olabilir)")
                time.sleep(0.1)

    def __exit__(self, *exc):
        try:
            os.rmdir(self.path)
        except OSError:
            pass


def write_private(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def make_backup(raw):
    os.makedirs(BACKUP_ROOT, mode=0o700, exist_ok=True)
    os.chmod(STATE_ROOT, 0o700)
    base = os.path.join(BACKUP_ROOT, datetime.now().strftime("%Y%m%d-%H%M%S"))
    path, n = base, 1
    while os.path.exists(path):
        n += 1
        path = f"{base}-{n}"
    os.mkdir(path, 0o700)
    for (name, kind), data in raw.items():
        if data is not None:
            fname = "credentials.json" if kind == "creds" else "claude.json"
            write_private(os.path.join(path, f"{name}.{fname}"), data)
    # eski yedekleri temizle
    old = sorted(d for d in os.listdir(BACKUP_ROOT) if not d.startswith("."))
    for d in old[:-KEEP_BACKUPS]:
        full = os.path.join(BACKUP_ROOT, d)
        for f in os.listdir(full):
            os.remove(os.path.join(full, f))
        os.rmdir(full)
    return path


# ---------------------------------------------------------------- giriş bilgisi (Anahtar Zinciri / dosya)
# Claude Code macOS'ta girişi Anahtar Zinciri'ne yazar, oraya erişemezse (ör. SSH'ta kilitli) .credentials.json'a.
# Okurken de önce Anahtar Zinciri, yoksa dosya. clsw aynı sırayı izler; yazarken de aynı yere yazar.

class KeychainError(Exception):
    pass


def _keychain_account():
    import getpass
    return os.environ.get("USER") or getpass.getuser()


def _security(args, stdin=None):
    try:
        return subprocess.run(["/usr/bin/security"] + args, input=stdin, capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        raise KeychainError(str(e))


def keychain_read(service):
    """Anahtar Zinciri'ndeki giriş (bytes) ya da None (kayıt yok). Erişilemezse KeychainError."""
    r = _security(["find-generic-password", "-a", _keychain_account(), "-s", service, "-w"])
    if r.returncode == 44:                      # errSecItemNotFound
        return None
    if r.returncode:
        raise KeychainError(r.stderr.decode(errors="replace").strip() or f"security {r.returncode}")
    out = r.stdout.rstrip(b"\n")
    if out[:1] not in (b"{", b"[") and re.fullmatch(rb"[0-9a-fA-F]+", out or b"x") and len(out) % 2 == 0:
        out = bytes.fromhex(out.decode())       # security yazdırılamayan veriyi hex verir
    return out


def keychain_write(service, data):
    """data: bytes; None ise kayıt silinir. Komut stdin'den verilir (token süreç listesinde görünmez)."""
    acct = _keychain_account()
    if data is None:
        r = _security(["delete-generic-password", "-a", acct, "-s", service])
        if r.returncode not in (0, 44):
            raise KeychainError(r.stderr.decode(errors="replace").strip() or f"security {r.returncode}")
        return
    line = f'add-generic-password -U -a "{acct}" -s "{service}" -X "{data.hex()}"\n'
    r = _security(["-i"], stdin=line.encode())
    if r.returncode or b"error" in r.stderr.lower():
        raise KeychainError(r.stderr.decode(errors="replace").strip() or f"security {r.returncode}")


def read_creds(slot):
    """Profilin girişi (bytes) ya da None. macOS'ta önce Anahtar Zinciri, yoksa .credentials.json."""
    if slot.get("keychain"):
        try:
            data = keychain_read(slot["keychain"])
            if data is not None:
                return data
        except KeychainError:
            pass
    return read_bytes(slot["creds"])


def write_creds(slot, data):
    """Girişi yazar (None: siler). macOS'ta Anahtar Zinciri'ne; erişilemezse dosyaya. Giriş siliniyorsa
    ikisinden de silinir: kalan eski dosya, Anahtar Zinciri boşalınca Claude Code'a giriş gibi görünür."""
    if slot.get("keychain"):
        try:
            keychain_write(slot["keychain"], data)
        except KeychainError:
            if data is None:
                raise
        else:
            if data is None:
                atomic_write(slot["creds"], None, private=True)
            return
    atomic_write(slot["creds"], data, private=True)


def creds_where(slot):
    if slot.get("keychain"):
        try:
            if keychain_read(slot["keychain"]) is not None:
                return "Anahtar Zinciri"
        except KeychainError:
            pass
    return tilde(slot["creds"])


# ---------------------------------------------------------------- hesap bilgisi

def account_email(global_json):
    return ((global_json or {}).get("oauthAccount") or {}).get("emailAddress")


def slot_info(s):
    creds = parse_json(read_creds(s)) or {}
    return {
        "slot": s,
        "exists": os.path.isdir(s["dir"]),
        "oauth": creds.get("claudeAiOauth") or {},
        "email": account_email(parse_json(read_bytes(s["json"]))),
    }


def cli_user_agent():
    """Claude Code'un User-Agent'ı. Sunucu reset haklarını (cedar_ember) yalnızca
    Claude Code yüzeyinden gelen isteğe döner; başka UA ile "surface" der, boş gelir."""
    ver = os.path.basename(os.path.realpath(os.path.join(HOME, ".local", "bin", "claude")))
    if not re.fullmatch(r"\d+\.\d+\.\d+", ver):
        ver = "2.1.328"
    return f"claude-cli/{ver} (external, cli)"


# Limitler + reset hakları tek istekte; spend bloğu kullanılmadığı için atlanır.
USAGE_PATH = "usage?cedar_ember=1&skip_spend=1"


def api_get(token, path):
    req = urllib.request.Request(API + path, headers={
        "Authorization": "Bearer " + token,
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": cli_user_agent(),
        "x-app": "cli",
    })
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read())


# ---- kullanım API'si 429 dönünce yedek ölçüm
# /api/oauth/usage hesap başına istek sınırlıdır. Hesap yoğun kullanılırken (çok sayıda claude -p açılışı)
# 429 + Retry-After döner ve tabloda kullanım görünmez (profil isteği aynı anda sorunsuz). Yedek: o hesabın
# token'ıyla küçük bir haiku isteği — Claude Code stream-json çıktısında
# `rate_limit_event.rate_limit_info.unifiedWindows` (pencere başına kullanım oranı + sıfırlanma) verir.
# ~2–3 sn, ~$0.001; oturum kaydı yazılmaz. Sonuç ~/.claudeswap/usage-cache.json'da tutulur.
USAGE_CACHE_TTL = 300                # yedek ölçüm bu kadar saniye içinde tekrarlanmaz
_usage_lock = threading.Lock()


def usage_cache_put(o, usage, src):
    key = o.get("accountUuid")
    if not key or not isinstance(usage, dict):
        return
    with _usage_lock:
        cache = read_json(_state("usage-cache.json")) or {}
        old = cache.get(key) or {}
        if src != "api" and isinstance((old.get("usage") or {}).get("cedar_ember"), dict):
            usage = dict(usage, cedar_ember=old["usage"]["cedar_ember"])   # RESET hakları son API yanıtından
        cache[key] = {"ts": time.time(), "usage": usage, "src": src}
        try:
            os.makedirs(STATE_ROOT, mode=0o700, exist_ok=True)
            atomic_write(_state("usage-cache.json"), json.dumps(cache, ensure_ascii=False).encode())
        except OSError:
            pass


def measure_usage(slot, token):
    """Küçük bir haiku isteğiyle pencere kullanımları ({five_hour: {utilization %, resets_at}}) ya da None.
    Token açıkça verilir: claude -p macOS'ta önce anahtar zincirini okuyabilir, ölçülen hesap bu profilinki olmalı."""
    exe = os.path.join(HOME, ".local", "bin", "claude")
    if not os.access(exe, os.X_OK):
        exe = shutil.which("claude")
    if not exe:
        return None
    env = {k: v for k, v in os.environ.items() if k not in ACTIVATE_UNSET}
    if slot.get("config_env") or slot["name"] != SLOTS[0]["name"]:
        env["CLAUDE_CONFIG_DIR"] = slot["dir"]
    env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    cmd = [exe, "-p", "ok", "--model", "haiku", "--output-format", "stream-json", "--verbose", "--max-turns", "1",
           "--tools", "", "--strict-mcp-config", "--no-session-persistence"]
    try:
        r = subprocess.run(cmd, env=env, cwd=tempfile.gettempdir(), stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=60)
    except (subprocess.TimeoutExpired, OSError):
        return None
    for line in reversed(r.stdout.splitlines()):
        ev = parse_json(line)
        if not isinstance(ev, dict) or ev.get("type") != "rate_limit_event":
            continue
        out = {}
        for k, v in ((ev.get("rate_limit_info") or {}).get("unifiedWindows") or {}).items():
            if not isinstance(v, dict):
                continue
            at = v.get("resetsAt")
            out[k] = {"utilization": round(float(v.get("utilization") or 0) * 100, 1),
                      "resets_at": datetime.fromtimestamp(at, timezone.utc).isoformat() if at else None}
        return out
    return None


def usage_fallback(info):
    """429 sonrası: 5 dk'dan taze önbellek ya da yeni ölçüm. Bulunursa info["usage"] dolar, True döner."""
    o = info["oauth"]
    hit = (read_json(_state("usage-cache.json")) or {}).get(o.get("accountUuid") or "-")
    if not hit or time.time() - hit.get("ts", 0) >= USAGE_CACHE_TTL:
        u = measure_usage(info["slot"], o["accessToken"])
        if u is not None:
            usage_cache_put(o, u, "olcum")
            hit = (read_json(_state("usage-cache.json")) or {}).get(o.get("accountUuid") or "-") \
                or {"ts": time.time(), "usage": u, "src": "olcum"}
    if not hit or not isinstance(hit.get("usage"), dict):
        return False
    info["usage"], info["usage_src"], info["usage_ts"] = hit["usage"], hit.get("src"), hit.get("ts")
    return True


# ---- süresi geçmiş access token'ı yenileme (clsw list)
# Access token ~8 saat geçerli; Claude Code onu yalnız o profille açılınca yeniler. Kimsenin açmadığı profilde
# (claude2…) tabloda "token süresi dolmuş" kalıyordu, kullanım/RESET gelmiyordu. clsw list artık Claude Code'un
# yaptığını yapar (2.1.332'den): iki kilidi alır (proper-lockfile uyumlu mkdir dizinleri:
# <profil>/.oauth_refresh.lock ve eski <profil>.lock), dosyayı kilit altında yeniden okur, refresh_token ile
# TOKEN_URL'e POST eder, dosyayı yalnız refreshToken hâlâ gönderdiğimizse yazar (CAS). Kilit doluysa o an bir
# claude yeniliyordur → dokunulmaz. refreshTokenExpiresAt uzatılmaz (Claude Code da min(eski, yeni) yazar).
OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"   # Claude Code CLI
REFRESH_LOCK_STALE = 60                                      # sn; Claude Code'un kilidi 5 sn'de bir tazelenir


def _take_dir_lock(path):
    try:
        os.mkdir(path)
        return True
    except FileExistsError:
        return False


def refresh_oauth(slot, force=False):
    """Profilin access token'ını yeniler. None = yenilendi (ya da başkası az önce yeniledi), yoksa kısa hata.
    force: süresi geçmemiş olsa da yenile (Claude Code'daki force gibi; yalnız deneme için)."""
    locks = [os.path.join(slot["dir"], ".oauth_refresh.lock"), os.path.realpath(slot["dir"]) + ".lock"]
    taken = []
    try:
        for p in locks:
            if not _take_dir_lock(p):
                return "kilit dolu"
            taken.append(p)
        cur = parse_json(read_creds(slot)) or {}
        o = cur.get("claudeAiOauth") or {}
        sent = o.get("refreshToken")
        if not sent:
            return "refresh token yok"
        if not force and o.get("expiresAt", 0) / 1000 > time.time() + 60:
            return None   # kilidi beklerken başka bir claude yenilemiş
        body = {"grant_type": "refresh_token", "refresh_token": sent,
                "client_id": o.get("clientId") or OAUTH_CLIENT_ID}
        if o.get("scopes"):
            body["scope"] = " ".join(o["scopes"])
        req = urllib.request.Request(OAUTH_TOKEN_URL, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": cli_user_agent()})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            err = (parse_json(e.read()) or {}).get("error") if e.code == 400 else None
            return "token geçersiz" if err == "invalid_grant" else f"yenileme hatası ({e.code})"
        now_ms = int(time.time() * 1000)
        if not isinstance(data.get("access_token"), str) or not isinstance(data.get("expires_in"), (int, float)):
            return "yenileme yanıtı bozuk"
        new = dict(o, accessToken=data["access_token"], refreshToken=data.get("refresh_token") or sent,
                   expiresAt=now_ms + int(data["expires_in"] * 1000))
        rte = data.get("refresh_token_expires_in")
        if isinstance(rte, (int, float)) and not isinstance(rte, bool):
            new["refreshTokenExpiresAt"] = min(o.get("refreshTokenExpiresAt") or 10 ** 15, now_ms + int(rte * 1000))
        if isinstance(data.get("scope"), str) and data["scope"].split():
            new["scopes"] = data["scope"].split()
        latest = parse_json(read_creds(slot)) or {}
        if (latest.get("claudeAiOauth") or {}).get("refreshToken") != sent:
            return "dosya yenileme sırasında değişti"
        write_creds(slot, json.dumps(dict(latest, claudeAiOauth=new), separators=(",", ":")).encode())
        return None
    except (OSError, ValueError, KeychainError) as e:
        return f"yenilenemedi ({e.__class__.__name__})"
    finally:
        for p in reversed(taken):
            try:
                os.rmdir(p)
            except OSError:
                pass


def refresh_if_stale(info):
    """Access token'ı geçmiş ama girişi (refresh token) geçerli profili yeniler, bilgiyi yeniden okur."""
    o, now = info["oauth"], time.time()
    if not o.get("refreshToken") or o.get("expiresAt", 0) / 1000 >= now \
            or (o.get("refreshTokenExpiresAt") and o["refreshTokenExpiresAt"] / 1000 < now):
        return info
    err = refresh_oauth(info["slot"])
    fresh = slot_info(info["slot"])
    if err in NEEDS_LOGIN:
        fresh["refresh_error"] = err
    return fresh


# Kısa durum metinleri (clsw list DURUM sütunu); ne yapılacağı tablonun altındaki notta.
NEEDS_LOGIN = ("giriş yok", "oturum süresi dolmuş", "token geçersiz")   # → o profili açıp /login


def fetch_live(info):
    o = info["oauth"]
    now = time.time()
    if not info["exists"]:
        info["status"] = "kurulu değil"
    elif not o.get("accessToken"):
        info["status"] = "giriş yok"
    elif o.get("refreshTokenExpiresAt") and o["refreshTokenExpiresAt"] / 1000 < now:
        info["status"] = "oturum süresi dolmuş"
    elif o.get("expiresAt", 0) / 1000 < now:
        info["status"] = info.get("refresh_error") or "token süresi dolmuş"
        info["stale"] = True
    else:
        try:
            # İki istek aynı anda: her biri ~1 sn (çoğu TLS el sıkışması); arka arkaya hesap başına 2 sn ederdi.
            with ThreadPoolExecutor(2) as ex:
                f_prof = ex.submit(api_get, o["accessToken"], "profile")
                f_usage = ex.submit(api_get, o["accessToken"], USAGE_PATH)
                prof = f_prof.result()
                info["token_email"] = (prof.get("account") or {}).get("email")
                info["token_uuid"] = (prof.get("account") or {}).get("uuid")
                try:
                    info["usage"] = f_usage.result()
                    usage_cache_put(o, info["usage"], "api")
                except urllib.error.HTTPError as e:
                    if e.code != 429 or not usage_fallback(info):
                        raise
        except urllib.error.HTTPError as e:
            info["status"] = "token geçersiz" if e.code in (401, 403) else f"API hatası ({e.code})"
        except Exception:  # ağ hatası vb.
            info["status"] = "limit alınamadı"
    return info


def plan_label(o):
    """Yalnız haftalık kullanım çarpanı (Pro'ya göre): Ultra → 10x, Max → 5x, Pro → 1x (kullanıcı isteği).
    Ultra = eski Max 20x; sunucu bu hesaplara hâlâ rateLimitTier "default_claude_max_20x" veriyor
    (profil: plan_display_name "Ultra") ama haftalık hak Pro'nun 10 katı."""
    tier, sub = o.get("rateLimitTier") or "", o.get("subscriptionType") or ""
    if "ultra" in tier or "max_20x" in tier:
        return "10x"
    if "max_5x" in tier:
        return "5x"
    if sub == "pro":
        return "1x"
    return sub.capitalize() or "-"   # çarpanı bilinmeyen plan (team vb.) adıyla kalır


def fmt_delta(sec):
    sec = max(0, int(sec))
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}g {h}sa"
    if h:
        return f"{h}sa {m}dk"
    return f"{m}dk"


def expires_cell(o, now=None):
    """EXPIRES AT: girişin (yenileme token'ı) bittiği an; sonra o profili açıp /login gerekir.
    3 günden az kaldıysa sarı, geçtiyse kırmızı."""
    now = time.time() if now is None else now
    ms = o.get("refreshTokenExpiresAt")
    if not o.get("refreshToken"):
        return dim("-")
    if not isinstance(ms, (int, float)) or isinstance(ms, bool) or ms <= 0:
        return dim("?")
    t = ms / 1000
    txt = clock(t)
    if t <= now:
        return red(txt + " (dolmuş)")
    return yellow(txt) if t - now < 3 * 86400 else txt


def parse_iso(s):
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return datetime.fromisoformat(re.sub(r"\.\d+", "", s))


def usage_cell(u):
    if not u or u.get("utilization") is None:
        return dim("-")
    pct = u["utilization"]
    color = green if pct < 50 else yellow if pct < 80 else red
    txt = color(f"%{pct:.0f}")
    if u.get("resets_at"):
        try:
            secs = (parse_iso(u["resets_at"]) - datetime.now(timezone.utc)).total_seconds()
            txt += dim(" " + fmt_delta(secs))
        except ValueError:
            pass
    return txt


TR_MONTHS = ["Oca", "Şub", "Mar", "Nis", "May", "Haz", "Tem", "Ağu", "Eyl", "Eki", "Kas", "Ara"]


# Claude Code'daki adlandırmayla aynı türler (/limit-reset menüsü).
RESET_KINDS = {"full": "tam", "weekly": "Hafta", "session": "5 saat", "fable": "Fable"}


def reset_kind(g):
    if g.get("gives_back_weekly"):
        return "fable"
    if g.get("type") in RESET_KINDS:
        return g["type"]
    clears = g.get("clears") or []
    five = "five_hour" in clears
    other = any(c != "five_hour" for c in clears)
    return "session" if five and not other else "weekly" if other and not five else "full"


def resets_cell(u, now=None):
    """Kalan limit sıfırlama hakları: tür başına adet + en yakın son kullanma tarihi.
    Yeşil = şu an kullanılabilir, sarı = hak var ama şu an kullanılamıyor."""
    ce = (u or {}).get("cedar_ember")
    if not isinstance(ce, dict):
        return dim("-")
    now = datetime.now(timezone.utc) if now is None else now
    groups = {}
    for g in ce.get("grants") or []:
        if not isinstance(g, dict) or g.get("paused"):
            continue
        left = g.get("resets_left")
        if isinstance(left, bool) or not isinstance(left, int) or left <= 0:
            continue
        try:
            end = parse_iso(g["ends_at"]) if g.get("ends_at") else None
        except (TypeError, ValueError):
            end = None
        if end is not None and end <= now:
            continue
        x = groups.setdefault(reset_kind(g), {"left": 0, "end": None, "usable": False})
        x["left"] += left
        x["usable"] = x["usable"] or bool(g.get("usable_now"))
        if end is not None and (x["end"] is None or end < x["end"]):
            x["end"] = end
    if not groups:
        return dim("-")
    parts = []
    for k in sorted(groups, key=list(RESET_KINDS).index):
        x = groups[k]
        txt = (green if x["usable"] else yellow)(f"{x['left']} {RESET_KINDS[k]}")
        if x["end"] is not None:
            d = x["end"].astimezone()
            txt += dim(f" {d.day} {TR_MONTHS[d.month - 1]}")
        parts.append(txt)
    return "\n".join(parts)   # her tür ayrı satırda (print_table hücreyi alt alta basar)


def print_table(headers, rows):
    """Hücrede "\\n" varsa satırlar alt alta basılır; satırın diğer sütunları ilk satırda kalır."""
    split = lambda r: [str(c).split("\n") for c in r]
    widths = [max(vlen(line) for cell in col for line in cell) for col in zip(split(headers), *map(split, rows))]

    def fmt(cells, style=None):
        cells = split(cells)
        lines = []
        for n in range(max(map(len, cells))):
            out = []
            for c, w in zip(cells, widths):
                s = c[n] if n < len(c) else ""
                out.append((style(s) if style and s else s) + " " * (w - vlen(s)))
            lines.append("  " + "  ".join(out).rstrip())
        return "\n".join(lines)

    print(fmt(headers, bold))
    for r in rows:
        print(fmt(r))


# ---------------------------------------------------------------- açık oturumlar

def fmt_etime(e):
    days = 0
    if "-" in e:
        d, e = e.split("-", 1)
        days = int(d)
    parts = [int(x) for x in e.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    return fmt_delta(days * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2])


def _run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True).stdout
    except OSError:
        return ""


def _is_claude(comm):
    return os.path.basename(comm) == "claude" or "/claude/versions/" in comm


def running_sessions():
    slot_by_dir = {os.path.realpath(s["dir"]): s["name"] for s in SLOTS}
    procs = {}  # pid → (ppid, tty, comm): ebeveyn zinciri (ssh mi, başka bir claude'un alt süreci mi) için
    rows = []
    for line in _run(["ps", "-axo", "pid=,ppid=,etime=,tty=,comm="]).splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        pid, ppid, etime, tty, comm = parts
        procs[pid] = (ppid, tty, comm)
        if _is_claude(comm):
            rows.append((pid, etime, tty))
    sessions = []
    for pid, etime, tty in rows:
        full = _run(["ps", "-Eww", "-o", "command=", "-p", pid])
        if not full.strip() or "--chrome-native-host" in full:
            continue
        env = proc_env(full)
        cfg = env.get("CLAUDE_CONFIG_DIR")
        cfg_dir = os.path.expanduser(cfg) if cfg else SLOTS[0]["dir"]
        slot = slot_by_dir.get(os.path.realpath(cfg_dir), tilde(cfg_dir))
        lo = _run(["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"])
        cwd = next((l[1:] for l in lo.splitlines() if l.startswith("n")), "?")
        s = {"pid": pid, "slot": slot, "etime": etime, "cwd": tilde(cwd), "tty": tty, "env": env,
             "cfg_dir": cfg_dir, "full": full}
        s.update(ancestry(pid, procs))
        s.update(session_record(cfg_dir, pid))
        s["kind"], s["what"] = session_kind(s)
        sessions.append(s)
    order = {s["name"]: i for i, s in enumerate(SLOTS)}
    sessions.sort(key=lambda s: (order.get(s["slot"], 99), KIND_ORDER.get(s["kind"], 99), s.get("sort", ""),
                                 int(s["pid"])))
    return sessions


def print_sessions(sessions, detail=False):
    """detail=False: tek satır (hesap değişiminden sonraki uyarı). detail=True: her oturumun altında kim / iş /
    son adım satırları (clsw list, clsw oturum)."""
    width = shutil.get_terminal_size((160, 24)).columns
    headers = ["PID", "KOMUT", "SÜRE", "DURUM", "NE"]
    rows = [[s["pid"], s["slot"], fmt_etime(s["etime"]), status_cell(s), s["what"]] for s in sessions]
    widths = [max(vlen(x) for x in col) for col in zip(headers, *rows)][:4]

    def fmt(cells, style=None):
        head = "  " + "  ".join((style(c) if style else c) + " " * (w - vlen(c)) for c, w in zip(cells, widths)) + "  "
        last = cut(cells[4], max(12, width - vlen(head) - 1))
        return head + (style(last) if style else last)

    print(fmt(headers, bold))
    indent = " " * (2 + widths[0] + 2)
    for s, r in zip(sessions, rows):
        print(fmt(r))
        if not detail:
            continue
        for mark, text in detail_lines(s):
            text = cut(" ".join(mask(text).split()), max(12, width - len(indent) - 3))
            print(indent + dim(mark) + " " + (dim(text) if mark in ("▸", "↳") else text))


# ---------------------------------------------------------------- oturum ayrıntısı: ne yapıyor
# Salt okur. Süreç ortamından yalnız CLAUDE_CONFIG_DIR okunur (token taşıyan değişkenlere hiç bakılmaz). Oturumun
# durumu <profil>/sessions/<pid>.json'dan, yaptığı iş <profil>/projects/<klasör>/<oturum>.jsonl kaydından gelir.

ENV_NAMES = ("CLAUDE_CONFIG_DIR",)
TAIL_BYTES = (2 << 20, 16 << 20)     # kaydın sonundan okunan: önce 2 MB, tur başı bulunmazsa 16 MB
HEAD_BYTES = 512 << 10               # ilk istem için kaydın başından okunan
KIND_ORDER = {"kanal": 0, "terminal": 1, "p": 2, "diger": 3}
_CHANNEL_TAG = re.compile(r"\s*<channel\s([^>]*)>(.*?)(?:</channel>\s*)?$", re.S)
_cache = {}


def proc_env(full):
    """`ps -E` çıktısından yalnız izinli adlar (aynı ad birden çok geçerse sonuncusu: ortam argümanlardan sonra gelir)."""
    out = {}
    for tok in full.split():
        name, eq, value = tok.partition("=")
        if eq and name in ENV_NAMES:
            out[name] = value
    return out


def read_json(path):
    """Salt okuma yardımcısı: okunamayan dosya (izin, klasör, yok) None döner; hesap değişimi read_bytes kullanır."""
    try:
        return parse_json(read_bytes(path))
    except OSError:
        return None


def epoch(v):
    if isinstance(v, bool) or v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return v / 1000 if v > 1e11 else float(v)
    try:
        return parse_iso(str(v).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def clock(t):
    if t is None:
        return "--:--"
    d = datetime.fromtimestamp(t)
    if d.date() == datetime.now().date():
        return f"{d:%H:%M}"
    return f"{d.day} {TR_MONTHS[d.month - 1]} {d:%H:%M}"


def cut(s, n):
    s = str(s)
    return s if vlen(s) <= n else ANSI.sub("", s)[: max(1, n - 1)] + "…"


_SECRET_FORM = re.compile(r"(bearer\s+\S{12,}|://[^\s/:@]+:[^\s/@]+@)", re.I)


def _secret_like(word):
    """32+ karakterlik, rakam + (büyük harf ya da uzun hex) içeren, az tireli dizgiler token sayılır."""
    if _SECRET_FORM.search(word):
        return True
    for d in re.findall(r"[A-Za-z0-9_\-+=]{32,}", word):
        if re.search(r"\d", d) and (re.search(r"[A-Z]", d) or re.search(r"[0-9a-f]{24,}", d)) and d.count("-") <= 3:
            return True
    return False


def mask(text):
    t = re.sub(r"(?i)(bearer\s+)\S{12,}", r"\1[gizli]", str(text))          # sözcük sınırını aşan biçimler
    t = re.sub(r"(://)[^\s/:@]+:[^\s/@]+@", r"\1[gizli]@", t)
    return " ".join("[gizli]" if _secret_like(w) else w for w in t.split(" "))


def quote(text, n=160):
    t = " ".join(str(text or "").split())
    return "«" + (t if len(t) <= n else t[: n - 1] + "…") + "»"


# ---- oturum kaydı: tur kimin (istem mi, kanal mesajı mı, görev bildirimi mi)

def _content_text(k):
    t = k.get("type")
    if t == "queue-operation":
        c = k.get("content")
    elif t == "attachment":
        c = (k.get("attachment") or {}).get("prompt")
    else:
        c = (k.get("message") or {}).get("content")
        if isinstance(c, list):
            c = next((p.get("text") for p in c if isinstance(p, dict) and p.get("type") == "text"), None)
    return c if isinstance(c, str) else ""


def _channel_body(text):
    m = _CHANNEL_TAG.match(text or "")
    return m.group(2) if m else (text or "")


def classify(k):
    """Kaydın bir satırı → (tür, ek bilgi) ya da None. Türler: kanal (Claude Code kanalından gelen mesaj), istem
    (yazılan ya da -p ile verilen), bildirim (arka plan görevinin sonucu; turu işi başlatan tura aittir), arac."""
    if k.get("type") == "_arac":
        return "arac", {"idler": k.get("idler") or []}
    if k.get("type") != "user" or k.get("isCompactSummary"):
        return None
    c = (k.get("message") or {}).get("content")
    if isinstance(c, list) and any(isinstance(p, dict) and p.get("type") == "tool_result" for p in c):
        return None
    kind = (k.get("origin") or {}).get("kind")
    text = _content_text(k)
    turn_origin = (k.get("turnOrigin") or "").replace("-", "_")
    if kind == "channel":
        return "kanal", {}
    if kind == "task-notification" or turn_origin == "task_notification":
        m = re.search(r"<tool-use-id>(toolu_[\w-]+)</tool-use-id>", text)
        return ("bildirim", {"arac": m.group(1)}) if m else None
    if kind == "auto-continuation" or turn_origin == "auto_continuation":
        return None
    if kind == "human" or (text and not k.get("isMeta")):
        return "istem", {}
    return None


class TurnTracker:
    """Kaydı baştan sona yürür; her an süren turun türünü, mesajını ve zamanını tutar."""

    def __init__(self):
        self.kind = self.text = self.ts = None
        self._tools = {}

    def feed(self, k):
        if k.get("type") == "assistant":
            ids = [p.get("id") for p in (k.get("message") or {}).get("content") or []
                   if isinstance(p, dict) and p.get("type") == "tool_use" and p.get("id")]
            if not ids:
                return
            k = {"type": "_arac", "idler": ids}
        c = classify(k)
        if not c:
            return
        kind, tag = c
        if kind == "arac":
            for i in tag.get("idler") or []:
                self._tools[i] = (self.kind, self.text, self.ts)
        elif kind in ("kanal", "istem"):
            self.kind, self.text, self.ts = kind, _channel_body(_content_text(k)), epoch(k.get("timestamp"))
        elif kind == "bildirim":                     # görev bildirimi: sahibi işi başlatan turdur
            prev = self._tools.get(tag.get("arac"))
            if prev and prev[0]:
                self.kind, self.text, self.ts = prev


def parse_record(raw):
    """Ham JSONL satırı (bytes) → işe yarayan kayıt ya da None. Büyük ve gereksiz satırlar ayrıştırılmaz."""
    if raw.startswith(b'{"type":"api-request') or b'"type":"tool_result"' in raw:
        return None
    if b'"assistant"' not in raw and b'"user"' not in raw and b"queue-operation" not in raw \
            and b"queued_command" not in raw:
        return None
    try:
        k = json.loads(raw)
    except ValueError:
        return None
    return k if isinstance(k, dict) else None


def tool_line(name, inp):
    base = lambda p: os.path.basename(str(p or "?"))  # noqa: E731
    if name == "Bash":
        return "$ " + (inp.get("description") or "komut çalıştırıyor")
    if name == "Read":
        return "okuyor: " + base(inp.get("file_path"))
    if name in ("Edit", "MultiEdit", "NotebookEdit"):
        return "düzenliyor: " + base(inp.get("file_path") or inp.get("notebook_path"))
    if name == "Write":
        return "yazıyor: " + base(inp.get("file_path"))
    if name in ("Grep", "Glob"):
        return "arıyor: " + str(inp.get("pattern") or "")
    if name in ("Agent", "Task"):
        return "alt-agent: " + str(inp.get("description") or inp.get("subagent_type") or "")
    if name == "TodoWrite":
        cur = next((t for t in inp.get("todos") or [] if isinstance(t, dict) and t.get("status") == "in_progress"), None)
        return ("yapılacak: " + str(cur.get("activeForm") or cur.get("content") or "")) if cur else None
    if name in ("ToolSearch", "TaskOutput"):
        return None
    if name == "WebFetch":
        return "web okuyor: " + (re.sub(r"^https?://([^/]+).*", r"\1", str(inp.get("url") or "")) or "?")
    if name == "WebSearch":
        return "web'de arıyor: " + str(inp.get("query") or "")
    if name.startswith("mcp__"):
        parts = name.split("__")
        return f"{parts[1] if len(parts) > 1 else 'mcp'}: {parts[2] if len(parts) > 2 else ''}"
    return "araç: " + name


def _step(k):
    last = None
    for p in (k.get("message") or {}).get("content") or []:
        if not isinstance(p, dict):
            continue
        if p.get("type") == "tool_use":
            last = tool_line(p.get("name") or "", p.get("input") or {}) or last
        elif p.get("type") == "text" and (p.get("text") or "").strip():
            last = "yazdı: " + quote(p["text"])
    return last


def _tail_records(path, nbytes):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            start = max(0, size - nbytes)
            f.seek(start)
            data = f.read()
    except OSError:
        return [], True
    lines = data.split(b"\n")
    if start > 0:
        lines = lines[1:]                            # yarım ilk satır
    return [k for k in map(parse_record, lines) if k], start == 0


def transcript_summary(path):
    """Kaydın sonundan: süren (ya da son) turun türü + mesajı, son adım ve zamanı."""
    if "sum:" + path in _cache:
        return _cache["sum:" + path]
    tr, step, step_ts = TurnTracker(), None, None
    for nbytes in TAIL_BYTES:
        records, whole = _tail_records(path, nbytes)
        tr, step, step_ts = TurnTracker(), None, None
        for k in records:
            tr.feed(k)
            if k.get("type") == "assistant":
                st = _step(k)
                if st:
                    step, step_ts = st, epoch(k.get("timestamp"))
        if tr.kind or whole:
            break
    out = {"kind": tr.kind, "text": tr.text, "ts": tr.ts, "step": step, "step_ts": step_ts}
    _cache["sum:" + path] = out
    return out


def first_prompt(path):
    """Kaydın ilk kullanıcı istemi (claude -p işinin görevi)."""
    try:
        with open(path, "rb") as f:
            data = f.read(HEAD_BYTES)
    except OSError:
        return ""
    for raw in data.split(b"\n"):
        if b'"user"' not in raw:
            continue
        try:
            k = json.loads(raw)
        except ValueError:
            return ""
        if isinstance(k, dict) and k.get("type") == "user" and _content_text(k):
            return _content_text(k)
    return ""


def project_dir(cfg_dir, cwd):
    return os.path.join(cfg_dir, "projects", re.sub(r"[^A-Za-z0-9]", "-", cwd))


# ---- süreç başına

def ancestry(pid, procs):
    """ssh'tan mı açıldı, başka bir claude oturumunun alt süreci mi."""
    out, cur = {"ssh": False, "parent_claude": None}, procs.get(pid, ("1",))[0]
    for _ in range(12):
        if cur in ("0", "1") or cur not in procs:
            break
        ppid, _tty, comm = procs[cur]
        if "sshd" in comm:
            out["ssh"] = True
        if _is_claude(comm) and not out["parent_claude"]:
            out["parent_claude"] = cur
        cur = ppid
    return out


def session_record(cfg_dir, pid):
    """<profil>/sessions/<pid>.json: oturum kimliği, durum (busy/idle), kaydın yolu."""
    rec = read_json(os.path.join(cfg_dir, "sessions", f"{pid}.json")) or {}
    out = {"status": rec.get("status"), "status_since": epoch(rec.get("statusUpdatedAt")), "transcript": None}
    sid, cwd = rec.get("sessionId"), rec.get("cwd")
    if sid and cwd:
        path = os.path.join(project_dir(cfg_dir, cwd), f"{sid}.jsonl")
        if not os.path.exists(path):
            path = next(iter(glob.glob(os.path.join(cfg_dir, "projects", "*", f"{sid}.jsonl"))), None)
        out["transcript"] = path
        out["session_id"] = sid
    return out


def status_cell(s):
    st = s.get("status")
    since = s.get("status_since")
    if st == "busy":
        return green("● çalışıyor")
    if st == "idle":
        return dim("○ boşta" + (f" {fmt_delta(time.time() - since)}" if since else ""))
    if st == "shell":                                # tur bitti, arka planda komutu sürüyor (canlı çocuk kabuk)
        return yellow("◐ arka planda komut")
    return yellow(f"◐ {st}") if st else dim("?")


def session_kind(s):
    """(tür, NE sütunu) — argümanlardan ve terminalden; kayıt okunmaz."""
    full = s["full"]
    if "--channels" in full:
        return "kanal", "kanal oturumu · " + s["cwd"]
    if re.search(r"\s(-p|--print)(\s|$)", full):
        return "p", "claude -p · " + s["cwd"]
    if s["tty"] not in ("??", "-", ""):
        return "terminal", f"terminal ({'ssh ' if s['ssh'] else ''}{s['tty']}) · {s['cwd']}"
    return "diger", s["cwd"]


def detail_lines(s):
    """[(simge, metin)] — › son istem / mesaj · ≡ işin görevi · ↳ hangi oturumun alt süreci · ▸ son adım."""
    out = []
    try:
        summ = transcript_summary(s["transcript"]) if s.get("transcript") else None
        kind = s["kind"]
        if kind == "p":
            head = first_prompt(s["transcript"]) if s.get("transcript") else ""
            if head:
                out.append(("≡", quote(head, 120)))
        elif summ and summ["kind"]:
            what = "son mesaj" if summ["kind"] == "kanal" else "son istem"
            out.append(("›", f"{clock(summ['ts'])} {what}: {quote(summ['text'])}"))
        if s.get("parent_claude"):
            out.append(("↳", f"PID {s['parent_claude']} oturumunun başlattığı alt süreç"))
        if summ and summ["step"]:
            out.append(("▸", f"{clock(summ['step_ts'])} {summ['step']}"))
    except Exception as e:  # ayrıntı okunamazsa tablo yine basılır
        out.append(("▸", f"ayrıntı okunamadı ({e.__class__.__name__})"))
    return out


# ---------------------------------------------------------------- maliyet (clsw cost)
# İstem/iş başına maliyet oturum kayıtlarından hesaplanır: her assistant mesajının token sayıları × modelin fiyatı.
# Claude Code kendi tutarını (cost-state) yalnız oturum kapanırken ve oturum toplamı olarak yazar; istem kırılımı yoktur.
# Kapanmış oturumların büyük çoğunluğunda bu hesap cost-state ile aynı çıkar; kayda düşmeyen yan istekler (bağlam
# özeti vb.) olan oturumlarda biraz düşük kalır. Abonelik kullanımıdır: tutar API fiyatı karşılığıdır.
# Devam ettirilen/çatallanan oturum eski mesajları yeni kayda kopyalar: mesaj kimliğiyle tekilleştirilir, aynı mesaj
# bir kez sayılır. Yalnız okur; ayrıştırma sonucu ~/.claudeswap/cost-cache.json'da tutulur.

# $/MTok: girdi, çıktı, önbellek okuma, önbellek yazma 5 dk, önbellek yazma 1 sa. Claude Code'un cost-state.modelUsage
# kayıtlarından çıkarıldı. Listede olmayan model aile adıyla yaklaşık
# fiyatlanır ve tutarın yanına «~» konur.
PRICES = {
    "claude-fable-5-2": (8, 40, 0.2, 10, 16),
    "claude-opus-5-5": (4, 20, 0.2, 5, 8),
    "claude-opus-5": (5, 25, 0.5, 6.25, 10),
    "claude-opus-4-8": (5, 25, 0.5, 6.25, 10),
    "claude-sonnet-5-5": (2, 10, 0.1, 2.5, 4),
    "claude-haiku-5-5": (0.1, 0.5, 0.01, 0.125, 0.2),
}
PRICE_FAMILY = (("fable", "claude-fable-5-2"), ("opus", "claude-opus-5-5"), ("sonnet", "claude-sonnet-5-5"),
                ("haiku", "claude-haiku-5-5"))
WEB_SEARCH_USD = 0.01
COST_CACHE_V = 3                     # 2: mesaj başına [USD, girdi, çıktı, önb. okuma, önb. yazma]
COST_CACHE_DAYS = 40                 # önbellekte en çok bu kadar günlük kayıt tutulur
COST_ROWS = 60                       # varsayılan gösterilen satır (en yeniler); `hepsi` ya da sayı ile değişir
TEXT_KEEP = 300
COST_CMDS = ("cost", "costs", "maliyet", "harcama")
SRC_LABEL = {"terminal": "terminal", "p": "claude -p", "kanal": "kanal", "zamanlayici": "zamanlayıcı",
             "yerel": "yerel", "devam": "devam"}
SRC_COLOR = {"kanal": cyan, "p": magenta, "devam": dim}
SRC_FILTER = {"terminal": {"terminal"}, "p": {"p"}, "-p": {"p"}, "otomasyon": {"p"}, "kanal": {"kanal"},
              "zamanlayici": {"zamanlayici"}, "zamanlayıcı": {"zamanlayici"}}
COST_USAGE = ("clsw cost [24sa|3g|1hafta|bugün|dün|geçiş|geçiş2|geçiş3] [hepsi|<satır sayısı>] [oturum] "
              "[terminal|p|kanal]")


def _state(name):
    return os.path.join(STATE_ROOT, name)


def model_price(model):
    """(fiyat, kesin mi). Tarihli kimlik (claude-x-20251001) ve [1m] eki de tanınır."""
    m = str(model or "")
    base = re.sub(r"(\[1m\]|-\d{8})$", "", m)
    if base in PRICES:
        return PRICES[base], True
    fam = next((p for f, p in PRICE_FAMILY if f in m), None)
    return (PRICES[fam], False) if fam else (None, False)


def message_cost(usage, model):
    """(USD, kesin mi) — bir assistant mesajının usage bloğundan."""
    price, exact = model_price(model)
    if not price or not isinstance(usage, dict):
        return 0.0, exact

    def n(key, d=usage):
        v = (d or {}).get(key)
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0

    cc = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else None
    w5, w1 = (n("ephemeral_5m_input_tokens", cc), n("ephemeral_1h_input_tokens", cc)) if cc else (0, 0)
    if not w5 and not w1:                            # eski biçim: ayrım yok → 5 dakikalık yazma
        w5 = n("cache_creation_input_tokens")
    st = usage.get("server_tool_use") if isinstance(usage.get("server_tool_use"), dict) else {}
    usd = (n("input_tokens") * price[0] + n("output_tokens") * price[1] + n("cache_read_input_tokens") * price[2]
           + w5 * price[3] + w1 * price[4]) / 1e6 + n("web_search_requests", st) * WEB_SEARCH_USD
    return usd, exact


def usage_tokens(usage):
    """[girdi, çıktı, önbellekten okuma, önbelleğe yazma] token — usage bloğundan."""
    if not isinstance(usage, dict):
        return [0, 0, 0, 0]

    def n(key):
        v = usage.get(key)
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0
    return [n("input_tokens"), n("output_tokens"), n("cache_read_input_tokens"), n("cache_creation_input_tokens")]


def agent_name(cwd):
    c = cwd or ""
    if re.match(r"(/private)?/(var/folders|tmp)/", c) or c.startswith(tempfile.gettempdir()):
        return "geçici dizin"
    return tilde(c) or "?"


def _want_line(raw):
    """Maliyet için gereken satır mı (büyük ve gereksiz satırlar ayrıştırılmaz)."""
    if raw.startswith(b'{"type":"api-request') or raw.startswith(b'{"type":"last-prompt') \
            or raw.startswith(b'{"type":"atis'):
        return False
    if b'"type":"assistant"' in raw:
        return b'"usage"' in raw
    if raw.startswith(b'{"type":"session-owner"'):
        return True
    if b'"type":"tool_result"' in raw:
        return False
    return b'"type":"user"' in raw or b"queue-operation" in raw or b"queued_command" in raw


def _sub_files(path):
    return sorted(glob.glob(os.path.join(path[:-6], "subagents", "*.jsonl")))


def _sub_sig(path):
    out = []
    for sp in _sub_files(path):
        try:
            st = os.stat(sp)
        except OSError:
            continue
        out.append([os.path.basename(sp), st.st_size, st.st_mtime_ns])
    return out


def parse_session(path):
    """Bir oturum kaydından turlar. Tur = bir istem (terminal istemi, claude -p işi, kanal mesajı …) ve onun
    bitişine kadarki bütün API çağrıları. Görev bildirimiyle başlayan tur işi başlatan tura, alt-agent'ın maliyeti onu
    çağıran tura yazılır. Tur: {ts, end, src, text, job, m: {mesaj kimliği: USD}, models, acct, exact}."""
    turns, tool_turn, seen = [], {}, {}
    st = {"cur": None, "acct": None}
    info = {"cwd": None, "entry": None, "channel": False, "first": None}

    def new_turn(ts, src, text, k):
        text = text or ""
        t = {"ts": ts, "end": ts, "src": src, "text": mask(text[:TEXT_KEEP * 2])[:TEXT_KEEP], "m": {}, "models": {},
             "acct": st["acct"],
             "exact": True}
        if src == "p":
            t["job"] = agent_name(k.get("cwd") or info["cwd"])
        turns.append(t)
        st["cur"] = len(turns) - 1

    def charge(k, idx, sub=False):
        msg = k.get("message") or {}
        model = str(msg.get("model") or "?")
        mid = msg.get("id") or k.get("requestId") or k.get("uuid")
        if model == "<synthetic>" or not mid:
            return
        if mid in seen:                              # akışta aynı mesaj birkaç satır yazılır: son usage geçerli
            idx = seen[mid]
        elif idx is None:
            if st["cur"] is None:                    # kayıt bir turun ortasından başlıyor
                new_turn(epoch(k.get("timestamp")), "devam", "", k)
            idx = st["cur"]
        usd, exact = message_cost(msg.get("usage"), model)
        t = turns[idx]
        t["models"][model] = t["models"].get(model, 0.0) - (t["m"].get(mid) or [0.0])[0] + usd
        t["m"][mid] = [usd] + usage_tokens(msg.get("usage"))
        t["exact"] = t["exact"] and exact
        seen[mid] = idx
        ts = epoch(k.get("timestamp"))
        if ts and (t["end"] is None or ts > t["end"]):
            t["end"] = ts
        if st["acct"] and not sub:
            t["acct"] = st["acct"]
        for p in msg.get("content") or []:
            if isinstance(p, dict) and p.get("type") == "tool_use" and p.get("id"):
                tool_turn[p["id"]] = idx

    with open(path, "rb") as f:
        for raw in f:
            if not _want_line(raw):
                continue
            try:
                k = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(k, dict):
                continue
            typ = k.get("type")
            if typ == "session-owner":
                st["acct"] = k.get("accountUuid") or st["acct"]
                continue
            if info["cwd"] is None and k.get("cwd"):
                info["cwd"] = k["cwd"]
            if info["entry"] is None and k.get("entrypoint"):
                info["entry"] = k["entrypoint"]
            if info["first"] is None and k.get("timestamp"):
                info["first"] = epoch(k["timestamp"])
            if typ == "assistant":
                charge(k, None)
                continue
            if k.get("isSidechain"):
                continue
            c = classify(k)
            if not c:
                continue
            kind, tag = c
            ts = epoch(k.get("timestamp"))
            if kind == "kanal":
                info["channel"] = True
                new_turn(ts, "kanal", _channel_body(_content_text(k)), k)
            elif kind == "istem":
                if k.get("turnOrigin") == "scheduled" or k.get("scheduledTaskId"):
                    src = "zamanlayici"
                elif str(k.get("entrypoint") or info["entry"] or "").startswith("sdk"):
                    src = "p"
                elif (k.get("origin") or {}).get("kind") == "human" or k.get("promptSource") == "typed":
                    src = "terminal"
                else:
                    src = "yerel"
                new_turn(ts, src, _content_text(k), k)
            elif kind == "bildirim":
                idx = tool_turn.get(tag.get("arac"))
                if idx is not None:
                    st["cur"] = idx

    # Alt-agent'lar: <oturum>/subagents/agent-*.jsonl; meta.json'daki toolUseId onu çağıran turu verir (iç içe
    # alt-agent'ın çağrısı üst alt-agent'ın kaydındadır → çözülene kadar tur tur). Başlatanı bulunamayan zamana göre.
    def add_sub(sp, idx):
        try:
            f = open(sp, "rb")
        except OSError:
            return
        with f:
            for raw in f:
                if b'"type":"assistant"' not in raw or b'"usage"' not in raw:
                    continue
                try:
                    k = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(k, dict) or k.get("type") != "assistant":
                    continue
                if idx is None:
                    ts = epoch(k.get("timestamp")) or 0
                    idx = max((i for i, t in enumerate(turns) if (t["ts"] or 0) <= ts), default=len(turns) - 1)
                    if idx < 0:
                        return
                charge(k, idx, sub=True)

    pending = [(sp, (read_json(sp[:-6] + ".meta.json") or {}).get("toolUseId")) for sp in _sub_files(path)]
    while pending:
        rest = [(sp, tid) for sp, tid in pending if tid not in tool_turn]
        for sp, tid in pending:
            if tid in tool_turn:
                add_sub(sp, tool_turn[tid])
        if len(rest) == len(pending):
            break
        pending = rest
    for sp, _tid in pending:
        add_sub(sp, None)

    info["turns"] = [t for t in turns if t["m"]]
    return info


def cost_sessions(since):
    """Pencerede değişmiş kayıtlar: [(profil, oturum kimliği, ayrıştırılmış)]. Değişmeyen kayıt önbellekten gelir."""
    cache = read_json(_state("cost-cache.json")) or {}
    if cache.get("v") != COST_CACHE_V or not isinstance(cache.get("files"), dict):
        cache = {"v": COST_CACHE_V, "files": {}}
    files, dirty, out, todo = cache["files"], False, [], []
    for s in SLOTS:
        for path in glob.glob(os.path.join(s["dir"], "projects", "*", "*.jsonl")):
            try:
                stt = os.stat(path)
            except OSError:
                continue
            if stt.st_mtime < since:
                continue
            todo.append((s["name"], path, [stt.st_size, stt.st_mtime_ns, _sub_sig(path)]))
    fresh = sum(1 for _n, p, sig in todo if (files.get(p) or {}).get("sig") != sig)
    show = sys.stdout.isatty() and fresh > 30
    for i, (name, path, sig) in enumerate(todo):
        ent = files.get(path)
        if not ent or ent.get("sig") != sig:
            if show and i % 25 == 0:
                print(f"\r{dim(f'  Kayıtlar taranıyor… {i}/{len(todo)}')}", end="", flush=True)
            try:
                ent = parse_session(path)
            except OSError:
                continue
            ent["sig"] = sig
            files[path] = ent
            dirty = True
        out.append((name, os.path.basename(path)[:-6], ent))
    if show:
        print("\r\033[K", end="", flush=True)
    old = time.time() - COST_CACHE_DAYS * 86400
    for p in list(files):
        sig = files[p].get("sig") or [0, 0]
        if sig[1] / 1e9 < old or not os.path.exists(p):
            del files[p]
            dirty = True
    if dirty:
        try:
            os.makedirs(STATE_ROOT, mode=0o700, exist_ok=True)
            atomic_write(_state("cost-cache.json"),
                         json.dumps(cache, ensure_ascii=False, separators=(",", ":")).encode())
        except OSError:
            pass
    return out


def busy_session_ids():
    """Şu an çalışan (busy) oturumların kimlikleri: <profil>/sessions/<pid>.json, süreç ayaktaysa."""
    out = set()
    for s in SLOTS:
        for p in glob.glob(os.path.join(s["dir"], "sessions", "*.json")):
            rec = read_json(p) or {}
            if rec.get("status") != "busy" or not rec.get("sessionId"):
                continue
            try:
                pid = int(os.path.basename(p)[:-5])
            except ValueError:
                continue
            if pid_alive(pid):
                out.add(rec["sessionId"])
    return out


def pid_alive(pid):
    """Süreç ayakta mı. Windows'ta os.kill(pid, 0) süreci SONLANDIRIR; orada yalnız sorgulanır."""
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            pass
        return True
    import ctypes
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return k32.GetLastError() == 5               # erişim reddi = süreç var
    try:
        code = ctypes.c_ulong()
        return bool(k32.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259   # STILL_ACTIVE
    finally:
        k32.CloseHandle(h)


def cost_rows(since, until=None):
    """Penceredeki turlar, zamana göre. Aynı mesaj (kopyalanmış geçmiş) yalnız ilk göründüğü turda sayılır."""
    busy = busy_session_ids()
    turns = []
    for slot, sid, ent in cost_sessions(since):
        last = len(ent["turns"]) - 1
        for i, t in enumerate(ent["turns"]):
            turns.append((t["ts"] or ent.get("first") or 0, sid, i, slot, ent, t, i == last and sid in busy))
    turns.sort(key=lambda x: (x[0], x[1], x[2]))
    seen, rows = set(), []
    for ts, sid, _i, slot, ent, t, running in turns:
        usd, new, tok = 0.0, 0, [0, 0, 0, 0]
        for mid, c in t["m"].items():
            if mid not in seen:
                seen.add(mid)
                c = c if isinstance(c, list) else [c, 0, 0, 0, 0]
                usd += c[0]
                tok = [a + b for a, b in zip(tok, c[1:5])]
                new += 1
        if not new or ts < since or (until and ts >= until):
            continue
        rows.append({"ts": ts, "end": t["end"] or ts, "src": t["src"], "slot": slot, "sid": sid, "cwd": ent.get("cwd"),
                     "channel": ent.get("channel"), "text": t["text"], "job": t.get("job"),
                     "usd": usd, "tokens": tok, "models": t["models"], "accts": [t["acct"]] if t["acct"] else [],
                     "exact": t["exact"], "running": running, "count": 1})
    return rows


def collapse_rows(rows):
    """Aynı claude -p işinin aynı istemle tekrarlanan küçük çağrıları (sağlık yoklaması gibi) tek satırda: ×N."""
    groups = {}
    for r in rows:
        if r["src"] == "p":
            groups.setdefault((r["job"], r["text"][:200]), []).append(r)
    merged, drop = {}, set()
    for rs in groups.values():
        if len(rs) < 3 or any(x["usd"] >= 0.25 for x in rs):
            continue
        last = dict(rs[-1], usd=sum(x["usd"] for x in rs), count=len(rs), first_ts=rs[0]["ts"],
                    running=any(x["running"] for x in rs), exact=all(x["exact"] for x in rs))
        last["accts"] = sorted({a for x in rs for a in x["accts"]})
        drop.update(id(x) for x in rs)
        merged[id(rs[-1])] = last
    return [merged.get(id(r), r) for r in rows if id(r) not in drop or id(r) in merged]


def session_rows(rows):
    """Oturum başına tek satır: ilk istem, istem sayısı, toplam; kaynağı en çok harcayan kaynak."""
    by = {}
    for r in rows:
        s = by.get((r["slot"], r["sid"]))
        if s is None:
            s = by[(r["slot"], r["sid"])] = dict(r, models={}, accts=[], n=0, usd=0.0, src_usd={})
        s["n"] += 1
        s["usd"] += r["usd"]
        s["end"] = max(s["end"], r["end"])
        s["running"] = s["running"] or r["running"]
        s["exact"] = s["exact"] and r["exact"]
        for m, c in r["models"].items():
            s["models"][m] = s["models"].get(m, 0.0) + c
        s["accts"] += [a for a in r["accts"] if a not in s["accts"]]
        s["src_usd"][r["src"]] = s["src_usd"].get(r["src"], 0.0) + r["usd"]
    out = sorted(by.values(), key=lambda s: s["ts"])
    for s in out:
        s["src"] = "kanal" if s["channel"] else max(s["src_usd"], key=s["src_usd"].get)
    return out


# ---- hesaplar ve hesap değişimleri

def account_book():
    """accountUuid → e-posta ve clsw yedeklerinin (her değişimden hemen önce alınır) özeti. ~/.claudeswap/accounts.json'da
    birikir: eski yedekler silinse de (KEEP_BACKUPS) değişim geçmişi ve hesap adları kalır."""
    if "book" in _cache:
        return _cache["book"]
    book = read_json(_state("accounts.json")) or {}
    uuids = dict(book.get("hesaplar") or {})
    backups = dict(book.get("yedekler") or {})
    before = json.dumps([uuids, backups], sort_keys=True)

    def take(j):
        o = j.get("oauthAccount") if isinstance(j, dict) else None
        if not isinstance(o, dict):
            return None
        if o.get("accountUuid") and o.get("emailAddress"):
            uuids[o["accountUuid"]] = o["emailAddress"]
        return o.get("emailAddress")

    for s in SLOTS:
        take(read_json(s["json"]))
    try:
        dirs = sorted(d for d in os.listdir(BACKUP_ROOT) if not d.startswith("."))
    except OSError:
        dirs = []
    for d in dirs:
        folder = os.path.join(BACKUP_ROOT, d)
        try:
            if d in backups or time.time() - os.path.getmtime(folder) < 60:   # yazılmakta olan yedeğe dokunma
                continue
            names_ = os.listdir(folder)
        except OSError:
            continue
        slots = {}
        for fn in names_:
            m = re.fullmatch(r"(claude\d*)\.(claude|credentials)\.json", fn)
            if m:
                slots.setdefault(m.group(1), None)
                if m.group(2) == "claude":
                    slots[m.group(1)] = take(read_json(os.path.join(folder, fn)))
        if slots:
            backups[d] = slots
    book = {"hesaplar": uuids, "yedekler": backups}
    if json.dumps([uuids, backups], sort_keys=True) != before:
        try:
            os.makedirs(STATE_ROOT, mode=0o700, exist_ok=True)
            atomic_write(_state("accounts.json"), json.dumps(book, ensure_ascii=False, indent=1).encode())
        except OSError:
            pass
    _cache["book"] = book
    return book


def remember_accounts(pairs):
    """Profil API'sinden öğrenilen accountUuid → e-posta (clsw list): profilden çıkmış hesap da adıyla görünsün."""
    if not pairs:
        return
    book = read_json(_state("accounts.json")) or {}
    known = dict(book.get("hesaplar") or {})
    if all(known.get(u) == e for u, e in pairs.items()):
        return
    known.update(pairs)
    book["hesaplar"] = known
    try:
        os.makedirs(STATE_ROOT, mode=0o700, exist_ok=True)
        atomic_write(_state("accounts.json"), json.dumps(book, ensure_ascii=False, indent=1).encode())
    except OSError:
        pass
    _cache.pop("book", None)


def acct_label(uuid):
    if not uuid:
        return "?"
    email = account_book()["hesaplar"].get(uuid)
    return email.split("@")[0] if email else uuid[:8]


def log_swap(a, b, ea, eb, na, nb):
    """clsw değişimini ~/.claudeswap/swaps.jsonl'e yazar (clsw cost'taki ayırıcı satır). Hata değişimi bozmaz."""
    rec = {"ts": round(time.time(), 3), "profiller": {a["name"]: [ea, na], b["name"]: [eb, nb]}}
    try:
        fd = os.open(_state("swaps.jsonl"), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def swap_events():
    """clsw hesap değişimleri: swaps.jsonl + (günlükten önceki değişimler için) yedek klasörlerinden çıkarılanlar."""
    events = []
    try:
        with open(_state("swaps.jsonl"), encoding="utf-8") as f:
            for line in f:
                e = parse_json(line)
                if isinstance(e, dict) and isinstance(e.get("ts"), (int, float)) and isinstance(e.get("profiller"), dict):
                    events.append(e)
    except OSError:
        pass
    logged = list(events)
    for d, slots in account_book()["yedekler"].items():
        m = re.match(r"(\d{8}-\d{6})", d)
        if not m or not isinstance(slots, dict) or not slots:
            continue
        try:
            ts = time.mktime(time.strptime(m.group(1), "%Y%m%d-%H%M%S"))
        except ValueError:
            continue
        if any(abs(e["ts"] - ts) < 120 and set(slots) <= set(e["profiller"]) for e in logged):
            continue
        names_ = sorted(slots)
        if len(names_) == 2:
            a, b = names_
            prof = {a: [slots[a], slots[b]], b: [slots[b], slots[a]]}
        else:                                        # öbür profil boştu (yedekte dosyası yok)
            prof = {n: [slots[n], None] for n in names_}
        events.append({"ts": ts, "profiller": prof})
    events.sort(key=lambda e: e["ts"])
    return events


def swap_since(n, events=None):
    """Sondan n. hesap geçişi (1 = en son): (olay, kayıtlı geçiş sayısı). Yeterli geçiş yoksa en eskisi; hiç yoksa None."""
    events = swap_events() if events is None else events
    if not events:
        return None, 0
    return events[-min(max(1, n), len(events))], len(events)


def swap_summary(e, width=None):
    """«claude: ali → ayse» (profil sırasıyla, e-postanın @ öncesi)."""
    order = {s["name"]: i for i, s in enumerate(SLOTS)}
    short = lambda x: (x or "giriş yok").split("@")[0]
    return " · ".join(f"{n}: {short((list(e['profiller'][n]) + [None])[0])} → {short((list(e['profiller'][n]) + [None, None])[1])}"
                      for n in sorted(e["profiller"], key=lambda n: order.get(n, 99)))


def swap_line(e, width):
    order = {s["name"]: i for i, s in enumerate(SLOTS)}
    parts = []
    for name in sorted(e["profiller"], key=lambda n: order.get(n, 99)):
        pair = list(e["profiller"][name]) + [None, None]
        parts.append(f"{name}: {pair[0] or 'giriş yok'} → {pair[1] or 'giriş yok'}")
    txt = f" ⇄ {clock(e['ts'])} HESAP DEĞİŞİKLİĞİ YAPILDI (clsw) · " + " · ".join(parts) + " "
    return "  " + yellow(bold("━━━━" + txt + "━" * max(4, width - 7 - len(txt))))


# ---- tablo

def parse_cost_args(args):
    now = time.time()
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    opt = {"since": now - 86400, "until": None, "label": "son 24 saat", "limit": COST_ROWS, "mode": "istem",
           "src": None, "src_name": None}
    units = {"dk": 60, "m": 60, "sa": 3600, "s": 3600, "h": 3600, "g": 86400, "gün": 86400, "gun": 86400, "d": 86400,
             "hafta": 604800, "w": 604800}
    unit_tr = {60: "dakika", 3600: "saat", 86400: "gün", 604800: "hafta"}
    i = 0
    while i < len(args):
        raw = args[i]
        a = raw.lower()
        i += 1
        if a in ("hepsi", "tümü", "tumu", "tum", "tüm", "all", "-a", "--all", "--hepsi"):
            opt["limit"] = None
        elif a == "-n" and i < len(args) and args[i].isdigit():
            opt["limit"] = int(args[i])
            i += 1
        elif a.isdigit():
            opt["limit"] = int(a)
        elif a in ("oturum", "oturumlar", "session", "sessions"):
            opt["mode"] = "oturum"
        elif a in ("bugün", "bugun", "today"):
            opt["since"], opt["label"] = today.timestamp(), "bugün"
        elif a in ("dün", "dun", "yesterday"):
            opt["since"] = datetime.fromordinal(today.toordinal() - 1).timestamp()   # yerel gece yarısı (yaz saati dahil)
            opt["until"], opt["label"] = today.timestamp(), "dün"
        elif a in SRC_FILTER:
            opt["src"], opt["src_name"] = SRC_FILTER[a], raw
        elif re.fullmatch(r"(\d*)\s*(geçiş|gecis|swap)(\d*)", a):
            m = re.fullmatch(r"(\d*)\s*(geçiş|gecis|swap)(\d*)", a)
            n = int(m.group(1) or m.group(3) or 1)
            e, total = swap_since(n)
            if e is None:
                die("Kayıtlı hesap geçişi yok (clsw ile yapılan geçişler ~/.claudeswap/swaps.jsonl'e yazılır).", 2)
            nth = "son" if n == 1 else f"son {n}."
            opt["since"], opt["until"] = e["ts"], None
            opt["label"] = f"{nth} hesap geçişinden beri ({clock(e['ts'])}, {swap_summary(e)})"
            if total < n:
                opt["label"] += f" — yalnız {total} geçiş kayıtlı, en eskisinden"
        else:
            m = re.fullmatch(r"(\d+)\s*(dk|m|sa|s|h|g|gün|gun|d|hafta|w)", a)
            if not m:
                die(f"`{raw}` anlaşılmadı. Kullanım: {COST_USAGE}", 2)
            sec = units[m.group(2)]
            opt["since"] = now - int(m.group(1)) * sec
            opt["label"] = f"son {m.group(1)} {unit_tr[sec]}"
    return opt


def short_model(m):
    m = re.sub(r"^claude-", "", str(m or "?"))
    m = re.sub(r"-(\d+)-(\d+)$", r" \1.\2", m)      # opus-5-5 → opus 5.5
    return re.sub(r"-(\d+)$", r" \1", m)            # opus-5 → opus 5


def model_cell(models):
    total = sum(models.values()) or 1
    main = sorted(models.items(), key=lambda x: -x[1])
    if not main:
        return dim("-")
    extra = [m for m, c in main[1:] if c / total >= 0.05]
    return short_model(main[0][0]) + (dim("+" + ",".join(short_model(m).split()[0] for m in extra)) if extra else "")


def cost_cell(usd, exact=True):
    txt = f"${usd:,.2f}" + ("" if exact else "~")
    if usd >= 10:
        return red(bold(txt))
    if usd >= 2:
        return yellow(txt)
    return dim(txt) if usd < 0.01 else txt


def row_who(r):
    if r["src"] == "p":
        return r.get("job") or agent_name(r["cwd"])
    return tilde(r["cwd"] or "?")


def row_text(r):
    t = " ".join(mask(r.get("text") or "").split())
    if r.get("count", 1) > 1:
        t = f"×{r['count']} (ilki {clock(r['first_ts'])}) {t}"
    return t or dim("-")


def print_cost_table(rows, events, session_mode=False):
    width = shutil.get_terminal_size((160, 24)).columns
    headers = ["ZAMAN", "KAYNAK", "KİM / İŞ", "HESAP", "MODEL", "SÜRE", "COST"]
    data = []
    for r in rows:
        who = r.get("who") or row_who(r)
        if session_mode and r["n"] > 1:
            who = f"{who} · {r['n']} istem"
        dur = fmt_delta(max(0, (r["end"] or r["ts"]) - r["ts"])) if r.get("count", 1) == 1 else "-"
        data.append((r, [clock(r["ts"]), SRC_COLOR.get(r["src"], str)(SRC_LABEL.get(r["src"], r["src"])), cut(who, 46),
                         cut(",".join(acct_label(a) for a in r["accts"]) or "?", 18), model_cell(r["models"]),
                         green("● " + dur) if r["running"] else dur, cost_cell(r["usd"], r["exact"])]))
    widths = [max(vlen(x) for x in col) for col in zip(headers, *[c for _, c in data])]

    def line(cells, last, style=None):
        out = []
        for j, (c, w) in enumerate(zip(cells, widths)):
            c = style(c) if style else c
            pad = " " * (w - vlen(c))
            out.append(pad + c if j == 6 else c + pad)
        head = "  " + "  ".join(out) + "  "
        last = cut(last, max(12, width - vlen(head) - 1))
        return head + (style(last) if style else last)

    print(line(headers, "İLK İSTEM" if session_mode else "İSTEM", bold))
    pending = list(events)
    for r, cells in data:
        while pending and pending[0]["ts"] <= r["ts"]:
            print(swap_line(pending.pop(0), width))
        print(line(cells, row_text(r)))
    for e in pending:
        print(swap_line(e, width))


def _totals(rows, key):
    out = {}
    for r in rows:
        for k in key(r):
            x = out.setdefault(k, [0.0, 0])
            x[0] += r["usd"] / max(1, len(key(r)))
            x[1] += 1
    return sorted(out.items(), key=lambda kv: -kv[1][0])


def cmd_cost(args):
    opt = parse_cost_args(args)
    t0 = time.time()
    rows = cost_rows(opt["since"], opt["until"])
    if opt["src"]:
        rows = [r for r in rows if r["src"] in opt["src"]]
    session_mode = opt["mode"] == "oturum"
    rows = session_rows(rows) if session_mode else collapse_rows(rows)
    shown = rows if opt["limit"] is None else rows[-opt["limit"]:] if opt["limit"] else []
    events = [e for e in swap_events() if e["ts"] >= opt["since"] and (opt["until"] is None or e["ts"] < opt["until"])]
    if len(shown) < len(rows) and shown:
        events = [e for e in events if e["ts"] >= shown[0]["ts"]]
    what = "oturum" if session_mode else "istem / iş"
    title = f"Maliyet — {opt['label']}, {what} başına" + (f" · yalnız {opt['src_name']}" if opt["src_name"] else "")
    keep = [a for j, a in enumerate(args) if not a.isdigit() and a != "-n"]
    all_cmd = "clsw cost " + " ".join(keep + ["hepsi"])
    print(bold(title))
    if len(shown) < len(rows) and shown:            # kesilme en üstte görünsün: eski satırlar "yok" sanılmasın
        print(yellow(f"  {len(rows)} satırdan yalnız en yeni {len(shown)} tanesi gösteriliyor ({clock(shown[0]['ts'])} ve sonrası). "
                     f"Öncesi için: {all_cmd} · ya da tarayıcıda: clsw web"))
    if not rows and not events:
        print(dim("  Bu aralıkta kayıt yok."))
        return
    print_cost_table(shown, events, session_mode)
    total = sum(r["usd"] for r in rows)
    print()
    n_items = sum(r.get("count", 1) for r in rows) if not session_mode else len(rows)
    summary = f"  Toplam: {bold(f'${total:,.2f}')} · {n_items} {what}"
    if len(shown) < len(rows):
        summary += dim(f" · gösterilen son {len(shown)} satır — tümü için: {all_cmd}")
    print(summary)
    by_src = _totals(rows, lambda r: [r["src"]])
    print("  Kaynağa göre: " + dim(" · ").join(
        f"{SRC_COLOR.get(k, str)(SRC_LABEL.get(k, k))} ${v[0]:,.2f} {dim(f'({v[1]})')}" for k, v in by_src))
    by_acct = _totals(rows, lambda r: [acct_label(a) for a in r["accts"]] or ["?"])
    print("  Hesaba göre:  " + dim(" · ").join(f"{k} ${v[0]:,.2f}" for k, v in by_acct))
    print(dim("  COST: abonelik kullanımının API fiyatıyla karşılığı (Claude Code'un fiyatları) · ● sürüyor"
              " · ~ yaklaşık fiyat · ⇄ clsw ile hesap değişikliği"))
    print(dim(f"  Diğer görünümler: clsw cost oturum · clsw cost 7g · clsw cost geçiş|geçiş2 · clsw cost terminal|p|kanal · "
              f"tarayıcıda: clsw web · {time.time() - t0:.1f} sn"))


# ---------------------------------------------------------------- ilk mesaj (aktifleştirme)

ACTIVATE_PROMPT = "sadece ok yaz"
ACTIVATE_TIMEOUT = 120
# Profil komutlarının (~/.local/bin/claudeN) temizlediği değişkenler + iç içe oturum işaretleri.
ACTIVATE_UNSET = ("CLAUDE_CONFIG_DIR", "CLAUDE_SECURESTORAGE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN",
                  "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT",
                  "GH_TOKEN", "GITHUB_TOKEN", "NPM_TOKEN", "NODE_AUTH_TOKEN")   # başka hizmetlerin token'ları geçmesin
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def needs_activation(slot):
    """5 saatlik pencere başlamamışsa (ilk mesaj bekleniyorsa) hesap bilgisini, değilse None döner."""
    info = fetch_live(slot_info(slot))
    if not info["oauth"].get("accessToken"):
        return None
    if info.get("stale"):  # access token uzun süredir yenilenmemiş: hesap boşta, claude açılınca yenilenir
        return info
    if info.get("status"):  # giriş geçersiz, ağ hatası vb. — dokunma
        return None
    if info.get("usage_src") == "olcum":  # yedek ölçüm isteği pencereyi zaten başlattı
        return None
    fh = (info.get("usage") or {}).get("five_hour")
    return info if not fh or not fh.get("resets_at") else None


def claude_command(slot):
    exe = os.path.join(HOME, ".local", "bin", "claude")
    if not os.access(exe, os.X_OK):
        exe = shutil.which("claude")
    env = {k: v for k, v in os.environ.items() if k not in ACTIVATE_UNSET}
    if slot.get("config_env") or slot["name"] != SLOTS[0]["name"]:
        env["CLAUDE_CONFIG_DIR"] = slot["dir"]
    # Araçsız, MCP'siz, oturum kaydetmeden en küçük istek; pencereyi başlatmaya yeter.
    cmd = [exe, "-p", ACTIVATE_PROMPT, "--model", "haiku", "--tools", "",
           "--strict-mcp-config", "--no-session-persistence"]
    return cmd, env


def send_first_message(slot):
    cmd, env = claude_command(slot)
    if not cmd[0]:
        return False, "claude komutu bulunamadı"
    try:
        # CLAUDE.md taranmasın diye ana dizin dışında çalıştır.
        r = subprocess.run(cmd, env=env, cwd=tempfile.gettempdir(), stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=ACTIVATE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, f"{ACTIVATE_TIMEOUT} sn içinde yanıt gelmedi"
    except OSError as e:
        return False, str(e)
    if r.returncode != 0:
        lines = (r.stderr.strip() or r.stdout.strip()).splitlines()
        return False, lines[-1] if lines else f"çıkış kodu {r.returncode}"
    return True, r.stdout.strip()


def window_after(slot, tries=3):
    """Mesajdan sonra başlayan 5 saatlik pencerenin bilgisi (API birkaç sn gecikebilir)."""
    for n in range(tries):
        fh = (fetch_live(slot_info(slot)).get("usage") or {}).get("five_hour")
        if fh and fh.get("resets_at"):
            return fh
        if n + 1 < tries:
            time.sleep(2)
    return None


def activate(slots):
    if os.environ.get("CLAUDESWAP_NO_ACTIVATE") or not slots:
        return
    with ThreadPoolExecutor(len(slots)) as ex:
        todo = [i for i in ex.map(needs_activation, slots) if i]
    if not todo:
        return
    label = ", ".join(f"{i['slot']['name']} ({i['email'] or '?'})" for i in todo)
    msg = f"İlk mesaj bekleyen hesap aktifleştiriliyor: {label}"
    print()
    with ThreadPoolExecutor(len(todo)) as ex:
        futs = [ex.submit(send_first_message, i["slot"]) for i in todo]
        if sys.stdout.isatty():
            n = 0
            while not all(f.done() for f in futs):
                print(f"\r{yellow(SPIN[n % len(SPIN)])} {msg}", end="", flush=True)
                n += 1
                time.sleep(0.1)
            print("\r\033[K", end="", flush=True)
        else:
            print(msg, flush=True)
        results = [f.result() for f in futs]

    for i, (ok, detail) in zip(todo, results):
        who = f"{bold(i['slot']['name'])} ({i['email'] or '?'})"
        if not ok:
            print(yellow(f"⚠  {who} aktifleştirilemedi: {mask(detail)}"))
            continue
        fh = window_after(i["slot"])
        if fh:
            print(green("✓ ") + f"{who} aktif — 5 saatlik pencere başladı: {usage_cell(fh)}")
        else:
            print(green("✓ ") + f"{who} ilk mesaj gönderildi " + dim(f"(yanıt: {mask(detail)[:40]!r}; pencere henüz görünmüyor)"))


# ---------------------------------------------------------------- komutlar

LIST_SLOTS = 6   # clsw list claude … claude6'yı gösterir; sonrakiler girişliyse ya da clsw generate ile açıldıysa


def cmd_list():
    infos = [slot_info(s) for s in SLOTS[:LIST_SLOTS]]
    infos += [i for i in map(slot_info, SLOTS[LIST_SLOTS:]) if i["oauth"].get("accessToken") or i["email"]
              or os.path.exists(os.path.join(i["slot"]["dir"], GENERATED_MARK))]
    with ThreadPoolExecutor(len(infos)) as ex:   # süresi geçmiş access token önce yenilenir (refresh_if_stale)
        infos = list(ex.map(lambda i: fetch_live(refresh_if_stale(i)), infos))
    remember_accounts({i["token_uuid"]: i["token_email"] for i in infos if i.get("token_uuid") and i.get("token_email")})

    rows, mismatch = [], []
    for i in infos:
        email = i.get("token_email") or i["email"] or "-"
        if i.get("status"):
            status = yellow("⚠  " + i["status"])
        elif i.get("token_email") and i["email"] and i["token_email"] != i["email"]:
            status = red("⚠  hesap uyuşmuyor")
            mismatch.append(f"{i['slot']['name']}: .claude.json {i['email']} diyor, token {i['token_email']} hesabına ait")
        else:
            status = green("✓ hazır")
        if i.get("usage_src"):
            age = fmt_delta(max(60, time.time() - (i.get("usage_ts") or time.time())))   # en az "1dk"
            status += dim(f" · {'ölçüm ' if i['usage_src'] == 'olcum' else ''}{age}")
        u = i.get("usage") or {}
        rows.append([bold(i["slot"]["name"]), email, plan_label(i["oauth"]),
                     usage_cell(u.get("five_hour")), usage_cell(u.get("seven_day")),
                     resets_cell(u), expires_cell(i["oauth"]), status])

    print(bold("Hesaplar"))
    print_table(["KOMUT", "HESAP", "PLAN", "5 SAAT", "7 GÜN", "RESET", "EXPIRES AT", "DURUM"], rows)
    print(dim("  5 SAAT / 7 GÜN: % kullanım + sıfırlanmasına kalan süre"))
    print(dim("  RESET = kalan limit sıfırlama hakkı + son kullanma tarihi · yeşil: şimdi kullanılabilir"
              " · kullanmak için o profili açıp /limit-reset"))
    print(dim("  EXPIRES AT = girişin bittiği an; sonra o profili açıp /login"))
    statuses = {i.get("status") for i in infos}
    if statuses & set(NEEDS_LOGIN):
        print(dim("  " + " / ".join(x for x in NEEDS_LOGIN if x in statuses) + " = o profili açıp /login"))
    if "token süresi dolmuş" in statuses:
        print(dim("  token süresi dolmuş = kendiliğinden yenilenemedi (o an başka bir claude yeniliyor ya da ağ hatası);"
                  " bir sonraki clsw list'te ya da profil açılınca yenilenir"))
    for m in mismatch:
        print(red("  hesap uyuşmuyor — " + m))
    if any(i.get("usage_src") for i in infos):
        print(dim("  ✓ hazır · Ndk = kullanım API'si o hesap için istek sınırında (429); değerler N dk önceki kayıttan"))
    if any(i.get("usage_src") == "olcum" for i in infos):
        print(dim("  ölçüm = yedek ölçüm: kayıt yoktu, değerler o hesapla küçük bir haiku isteğiyle ölçüldü (~$0.001)"))
    print(dim("  Açık oturumlar için: clsw process · istem/iş başına maliyet: clsw cost"))


def cmd_sessions():
    sessions = running_sessions()
    print(bold("Açık oturumlar") + dim(f" ({len(sessions)})" if sessions else ""))
    if sessions:
        print_sessions(sessions, detail=True)
        print(dim("  › son istem (kanalda son mesaj) · ≡ claude -p işinin görevi · ↳ hangi oturumun alt süreci · ▸ son adım"))
    else:
        print(dim("  Açık Claude oturumu yok."))


def _set_or_pop(d, key, value):
    if value is MISSING:
        d.pop(key, None)
    else:
        d[key] = value


def cmd_swap(a, b):
    """Değişen profilleri döner (değişiklik yoksa boş liste)."""
    if a == b:
        print(f"✓ {a['name']} zaten seçili; değişiklik yapılmadı.")
        return []
    pair = [a, b]
    for s in pair:
        os.makedirs(s["dir"], mode=0o700, exist_ok=True)

    first, second = sorted(pair, key=lambda s: s["json"])
    with Lock(first["json"]), Lock(second["json"]):
        raw = {(s["name"], kind): (read_creds(s) if kind == "creds" else read_bytes(s[kind]))
               for s in pair for kind in ("creds", "json")}
        ja, jb = [({} if raw[(s["name"], "json")] is None else parse_json(raw[(s["name"], "json")])) for s in pair]
        if not isinstance(ja, dict) or not isinstance(jb, dict):
            die(".claude.json okunamadı; hiçbir şey değiştirilmedi.")
        for slot in pair:
            for kind in ("creds", "json"):
                validated_object(raw[(slot["name"], kind)], slot[kind])
        if raw[(a["name"], "creds")] is None and raw[(b["name"], "creds")] is None:
            die("İki tarafta da giriş yok; değiştirilecek hesap bulunamadı.")
        ea, eb = account_email(ja) or "giriş yok", account_email(jb) or "giriş yok"

        backup = make_backup(raw)
        for k in ACCOUNT_KEYS:
            va, vb = ja.get(k, MISSING), jb.get(k, MISSING)
            _set_or_pop(ja, k, vb)
            _set_or_pop(jb, k, va)

        try:
            write_creds(a, raw[(b["name"], "creds")])
            write_creds(b, raw[(a["name"], "creds")])
            atomic_write(a["json"], json.dumps(ja, indent=2, ensure_ascii=False).encode())
            atomic_write(b["json"], json.dumps(jb, indent=2, ensure_ascii=False).encode())
        except BaseException as e:
            failures = []
            for s in pair:  # Her dosya için geri almayı dene.
                for kind in ("creds", "json"):
                    try:
                        (write_creds(s, raw[(s["name"], kind)]) if kind == "creds"
                         else atomic_write(s[kind], raw[(s["name"], kind)]))
                    except BaseException:
                        failures.append(f"{s['name']} girişi" if kind == "creds" else tilde(s[kind]))
            if failures:
                die("Geri alma tamamlanamadı. Etkilenen dosyalar: " + ", ".join(failures)
                    + f". Kurtarma yedeği: {tilde(backup)}")
            die(f"Değişim yarıda kaldı ve geri alındı ({type(e).__name__}). Yedek: {tilde(backup)}")

    na = account_email(parse_json(read_bytes(a["json"]))) or "giriş yok"
    nb = account_email(parse_json(read_bytes(b["json"]))) or "giriş yok"
    log_swap(a, b, *[None if x == "giriş yok" else x for x in (ea, eb, na, nb)])
    w = max(len(ea), len(eb))
    print(green("✓ Hesaplar yer değiştirdi"))
    print(f"  {bold(a['name']):<8}  {ea:<{w}}  →  {bold(na)}")
    print(f"  {bold(b['name']):<8}  {eb:<{w}}  →  {bold(nb)}")
    print(dim(f"  Yedek: {tilde(backup)}"))

    sessions = running_sessions()
    if sessions:
        print()
        print(yellow("⚠  Bu oturumlar hâlâ eski hesapla çalışıyor; yeni hesaba geçmeleri için kapatıp yeniden aç:"))
        print_sessions(sessions)
    print()
    print(dim("Limitleri görmek için: claudeswap list"))
    return pair


def validated_object(raw, path):
    if raw is None:
        return {}
    value = parse_json(raw)
    if not isinstance(value, dict):
        die(f"{tilde(path)} geçerli bir JSON nesnesi değil; işlem durduruldu.")
    for key in ("oauthAccount", "claudeAiOauth"):
        if value.get(key) is not None and not isinstance(value[key], dict):
            die(f"{tilde(path)} içindeki {key} geçersiz; işlem durduruldu.")
    return value


def is_empty(slot):
    config = validated_object(read_bytes(slot["json"]), slot["json"])
    creds = validated_object(read_creds(slot), slot["creds"])
    # Bilinmeyen kimlik bilgilerini dolu kabul et; yanlışlıkla boş sayma.
    return not creds and not config.get("oauthAccount")


def cmd_use(target):
    """İstenen hesabı `claude` komutuna getirir; gerekirse değiştirir."""
    emails = {s["name"]: account_email(validated_object(read_bytes(s["json"]), s["json"])) for s in SLOTS}
    t = target.strip().lower()
    hits = [n for n, e in emails.items() if e and t in (e.lower(), e.lower().split("@")[0])]
    if not hits:
        known = ", ".join(e for e in emails.values() if e) or "yok"
        die(f"`{target}` hesabı bulunamadı. Mevcut hesaplar: {known}", 2)
    if len(hits) > 1:
        die(f"`{target}` birden fazla komutta görünüyor ({', '.join(hits)}); `claudeswap list` ile kontrol et.")
    if hits[0] == SLOTS[0]["name"]:
        print(green(f"✓ {emails[hits[0]]} zaten `{SLOTS[0]['name']}` komutunda, değişiklik yapılmadı."))
        return []
    return cmd_swap(SLOTS[0], next(s for s in SLOTS if s["name"] == hits[0]))


# ---------------------------------------------------------------- sürüm ve güncelleme
# Dağıtım GitHub Releases'ten (public): her sürümde Actions platform başına program arşivi yükler
# (clsw-windows-x64.zip, clsw-macos-arm64.tar.gz, clsw-linux-x64.tar.gz, clsw-linux-arm64.tar.gz) ve SHA256SUMS.
# Sürüm sorgusu ve indirme tokensızdır (hiçbir kimlik bilgisi gönderilmez); arşiv SHA-256 ile doğrulanır.
# curl/cmd kurulumunda program yeni sürümü yanına açar; npm ya da Homebrew kurulumunda onların komutuyla günceller.

REPO = "orjinalp/clsw"
FROZEN = bool(getattr(sys, "frozen", False))
UPDATE_STATE = os.path.join(STATE_ROOT, "update.json")
CHECK_EVERY = 60                     # sn; art arda çağrılarda GitHub'a tekrar sorulmaz
SNOOZE = 86400                       # "hayır" denen sürüm bu kadar süre tekrar sorulmaz
VERSION_CMDS = ("version", "--version", "-v", "sürüm", "surum")
UPDATE_CMDS = ("update", "güncelle", "guncelle")


def parse_version(v):
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", (v or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


def fmt_version(v):
    return "v" + ".".join(map(str, v))


def asset_name():
    import platform
    system = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")
    arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
    return f"clsw-{system}-{arch}" + (".zip" if system == "windows" else ".tar.gz")


MANAGED_UPDATE = {"npm": "npm install -g @orjinalp/clsw@latest", "brew": "brew upgrade clsw"}
LAUNCHER_UPDATE_EXIT = 75            # npm başlatıcısına "beni güncelle, sonra komutu yeniden çalıştır" (EX_TEMPFAIL)
LAUNCHER_UPDATE_ONLY_EXIT = 76       # "beni güncelle, komut zaten çalıştı" (CLSW_LAUNCHER=2 olan başlatıcı anlar)


def install_method():
    """Programı kuran paket yöneticisi: "npm", "brew" ya da None (curl/cmd kurulumu, kaynak)."""
    via = (os.environ.get("CLSW_INSTALLER") or "").strip().lower()
    if via in MANAGED_UPDATE:
        return via
    if not FROZEN:
        return None
    path = os.path.realpath(sys.executable).replace("\\", "/").lower()
    if "/node_modules/" in path:
        return "npm"
    if "/cellar/" in path or "/homebrew/" in path or "/linuxbrew/" in path:
        return "brew"
    return None


def _allowed_url(url):
    """Sürüm bilgisi ve indirme yalnız https ya da yerel dosyadan (ayna/deneme: CLSW_RELEASES_API)."""
    return isinstance(url, str) and url.lower().startswith(("https://", "file://"))


def latest_release(timeout=5):
    """Son sürüm → (sürüm bilgisi, None) ya da (None, hata metni). Tokensız: repo public."""
    url = os.environ.get("CLSW_RELEASES_API") or f"https://api.github.com/repos/{REPO}/releases/latest"
    if not _allowed_url(url):
        return None, "CLSW_RELEASES_API yalnız https:// ya da file:// olabilir"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": f"clsw/{__version__}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            rel = json.load(r)
    except urllib.error.HTTPError as e:
        return None, f"GitHub {e.code}" + (" (istek sınırı; biraz sonra tekrar dene)" if e.code in (403, 429) else "")
    except (urllib.error.URLError, OSError, ValueError) as e:
        return None, f"GitHub'a ulaşılamadı ({getattr(e, 'reason', e)})"
    ver = parse_version(rel.get("tag_name"))
    if not ver:
        return None, f"sürüm etiketi okunamadı: {rel.get('tag_name')}"
    asset = next((a for a in rel.get("assets") or [] if a.get("name") == asset_name()), None)
    return {"version": ver, "notes": (rel.get("body") or "").strip(), "asset": asset}, None


def download_asset(rel, dest):
    """Arşivi dest'e indirir ve SHA-256'sını Release'teki özetle karşılaştırır (özet yoksa kurmaz)."""
    import hashlib
    asset = rel["asset"]
    url = asset.get("browser_download_url")
    want = (asset.get("digest") or "").split(":", 1)
    if not _allowed_url(url):
        raise RuntimeError("indirme adresi https değil")
    if len(want) != 2 or want[0] != "sha256" or not re.fullmatch(r"[0-9a-f]{64}", want[1]):
        raise RuntimeError("Release'te SHA-256 özeti yok; kurulmadı")
    r = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": f"clsw/{__version__}"}),
                               timeout=120)
    h = hashlib.sha256()
    with r, open(dest, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
            f.write(chunk)
    bad = None
    if want[1] != h.hexdigest():
        bad = "indirilen dosyanın SHA-256'sı tutmuyor"
    elif asset.get("size") and os.path.getsize(dest) != asset["size"]:
        bad = "indirme yarım kaldı"
    if bad:
        os.remove(dest)
        raise RuntimeError(bad)


# ---- kurulum düzeni
# Program tek dosya değil klasördür (PyInstaller onedir): tek dosya her açılışta kendini geçici klasöre açar,
# macOS da açılan dosyaları her seferinde tarar (her komut ~5 sn). Klasör bir kez taranır, sonra ~0,1 sn.
#   ~/.local/share/clsw/versions/<sürüm>/clsw/   program klasörü (clsw, _internal/)
#   ~/.local/share/clsw/current                  → kullanılan sürüm (Windows'ta dizin bağlantısı: junction)
#   ~/.local/bin/clsw, claudeswap                → current/clsw (Windows: PATH'e current eklenir)
# Güncelleme yeni sürümü yanına açar ve current'ı çevirir; çalışan program yerinden oynamaz (Windows'ta kilitli).
KEEP_VERSIONS = 2


def app_root():
    return os.path.join(HOME, ".local", "share", "clsw")


def bin_dir():
    return os.path.join(HOME, ".local", "bin")


def exe_name():
    return "clsw.exe" if os.name == "nt" else "clsw"


def bundle_dir():
    return os.path.dirname(os.path.realpath(sys.executable))


def _point(link, target):
    """Dizin bağlantısını target'a çevirir (POSIX: symlink, yerine atomik; Windows: junction, yönetici gerekmez)."""
    if os.name == "nt":
        if os.path.lexists(link):
            os.rmdir(link)                           # junction'ı siler, hedefine dokunmaz
        r = subprocess.run(["cmd", "/c", "mklink", "/J", link, target], capture_output=True, text=True)
        if r.returncode:
            raise OSError(f"dizin bağlantısı kurulamadı: {r.stdout.strip() or r.stderr.strip()}")
        return
    tmp = link + ".new"
    if os.path.lexists(tmp):
        os.remove(tmp)
    os.symlink(target, tmp)
    os.replace(tmp, link)


def place_bundle(src, version):
    """Program klasörünü versions/<sürüm>/clsw'ye koyar, current'ı ona çevirir; çalıştırılacak yolu döner."""
    root = app_root()
    parent = os.path.join(root, "versions", version)
    dest = os.path.join(parent, "clsw")
    os.makedirs(os.path.join(root, "versions"), exist_ok=True)
    if os.path.normcase(os.path.realpath(src)) != os.path.normcase(os.path.realpath(dest)):
        tmp = parent + ".tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.copytree(src, os.path.join(tmp, "clsw"), symlinks=True)
        if os.name == "nt":
            shutil.copy2(os.path.join(tmp, "clsw", "clsw.exe"), os.path.join(tmp, "clsw", "claudeswap.exe"))
        if os.path.exists(parent):
            old = parent + ".old"
            shutil.rmtree(old, ignore_errors=True)
            os.replace(parent, old)
            shutil.rmtree(old, ignore_errors=True)
        os.replace(tmp, parent)
    _point(os.path.join(root, "current"), dest)
    return os.path.join(root, "current", exe_name())


def link_commands():
    """~/.local/bin/clsw ve claudeswap → current (POSIX). Windows'ta eski tek dosya kurulumunu kaldırır."""
    d = bin_dir()
    os.makedirs(d, exist_ok=True)
    if os.name == "nt":
        for n in ("clsw.exe", "claudeswap.cmd", "clsw.exe.old"):
            try:
                os.remove(os.path.join(d, n))
            except OSError:
                pass
        return os.path.join(app_root(), "current")
    target = os.path.join(app_root(), "current", "clsw")
    for name, dest in (("clsw", target), ("claudeswap", "clsw")):
        p = os.path.join(d, name)
        if os.path.lexists(p):
            os.remove(p)
        os.symlink(dest, p)
    return d


def cleanup_versions():
    """Kullanılan sürüm, çalışan sürüm ve en yeni KEEP_VERSIONS dışındakileri siler (hata olursa sessiz)."""
    vroot = os.path.join(app_root(), "versions")
    try:
        names = [n for n in os.listdir(vroot) if parse_version(n)]
        keep = {os.path.normcase(os.path.realpath(os.path.join(app_root(), "current"))),
                os.path.normcase(bundle_dir())}
    except OSError:
        return
    for n in sorted(names, key=parse_version)[:-KEEP_VERSIONS]:
        d = os.path.join(vroot, n)
        if os.path.normcase(os.path.realpath(os.path.join(d, "clsw"))) not in keep:
            shutil.rmtree(d, ignore_errors=True)
    for n in os.listdir(vroot):
        if n.endswith((".tmp", ".old")):
            shutil.rmtree(os.path.join(vroot, n), ignore_errors=True)


def extract_archive(path, dest):
    """Release arşivini açar (yol dışına çıkan girdiler reddedilir); içindeki clsw klasörünü döner."""
    import tarfile
    import zipfile
    base = os.path.realpath(dest)

    def safe(name):
        full = os.path.realpath(os.path.join(base, name))
        if not (full == base or full.startswith(base + os.sep)):
            raise RuntimeError(f"arşivde geçersiz yol: {name}")
    if path.endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            for n in z.namelist():
                safe(n)
            z.extractall(base)
    else:
        with tarfile.open(path) as t:
            for m in t.getmembers():
                safe(m.name)
                if m.issym() or m.islnk():
                    safe(os.path.join(os.path.dirname(m.name), m.linkname))
            if hasattr(tarfile, "data_filter"):      # 3.12+: güvenli süzgeç (3.14'te varsayılan)
                t.extractall(base, filter="data")
            else:
                t.extractall(base)
    out = os.path.join(base, "clsw")
    if not os.path.isfile(os.path.join(out, exe_name())):
        raise RuntimeError("arşivde clsw programı yok")
    return out


def install_release(rel):
    """Yeni sürümü indirir, yanına açar, current'ı çevirir; yeni programın yolunu döner."""
    ver = fmt_version(rel["version"]).lstrip("v")
    with tempfile.TemporaryDirectory(prefix="clsw-update-") as tmp:
        archive = os.path.join(tmp, rel["asset"]["name"])
        download_asset(rel, archive)
        exe = place_bundle(extract_archive(archive, os.path.join(tmp, "x")), ver)
    link_commands()
    return exe


def _update_state():
    return parse_json(read_bytes(UPDATE_STATE)) or {}


def _save_update_state(st):
    try:
        os.makedirs(STATE_ROOT, mode=0o700, exist_ok=True)
        atomic_write(UPDATE_STATE, json.dumps(st).encode())
    except OSError:
        pass


def star_box(lines):
    """Satırları yıldız çerçeve içinde alt alta basılacak metne çevirir (renk kodları genişliğe sayılmaz)."""
    width = max(vlen(x) for x in lines) + 4
    edge = yellow("*" * (width + 2))
    body = [yellow("*") + "  " + x + " " * (width - 2 - vlen(x)) + yellow("*") for x in lines]
    return "\n".join([edge] + body + [edge])


def show_release(rel):
    lines = [bold("clsw güncellemesi var"), "",
             f"v{__version__}  →  " + bold(green(fmt_version(rel['version'])))]
    notes = [n.strip() for n in rel["notes"].splitlines() if n.strip()][:12]
    if notes:
        lines += [""] + notes
    print(star_box(lines))


def do_install(rel):
    if not rel["asset"]:
        die(f"{fmt_version(rel['version'])} sürümünde bu platformun dosyası yok ({asset_name()}).")
    print(dim("İndiriliyor…"), flush=True)
    try:
        target = install_release(rel)
    except (OSError, RuntimeError, urllib.error.URLError) as e:
        die(f"Güncelleme kurulamadı: {e}")
    print(green(f"{fmt_version(rel['version'])} kuruldu.") + dim(f"  {tilde(target)}"))
    return target


def managed_update(via, argv):
    """npm/Homebrew kurulumunu kendi komutuyla günceller. argv verilirse komutu yeni sürümle yeniden çalıştırır;
    None ise (komut zaten çalıştı) yalnız günceller.
    npm: güncellemeyi başlatıcı yapar (Windows'ta çalışan .exe kilitli, npm üzerine yazamaz)."""
    if via == "npm":
        if os.environ.get("CLSW_LAUNCHER_UPDATE") == "1":
            sys.stdout.flush()
            only = argv is None and os.environ.get("CLSW_LAUNCHER") == "2"
            sys.exit(LAUNCHER_UPDATE_ONLY_EXIT if only else LAUNCHER_UPDATE_EXIT)
        die(f"Bu kurulum kendi kendine güncellenemiyor; çalıştır: {MANAGED_UPDATE['npm']}")
    brew = shutil.which("brew")
    if not brew:
        die(f"brew bulunamadı; çalıştır: {MANAGED_UPDATE['brew']}")
    print(dim(MANAGED_UPDATE["brew"] + " …"), flush=True)
    # Onay clsw'de alındı; Homebrew 7+ terminalde ikinci kez sormasın (eski sürümler bu değişkeni yok sayar).
    if subprocess.call([brew, "upgrade", "clsw"], env=dict(os.environ, HOMEBREW_NO_ASK="1")):
        die("brew upgrade clsw başarısız oldu.")
    exe = os.path.realpath(sys.executable).replace("\\", "/")
    cut = exe.lower().find("/cellar/")
    target = os.path.join(exe[:cut], "bin", "clsw") if cut > 0 else shutil.which("clsw")
    if argv is None:
        print(green("Güncellendi; yeni sürüm bir sonraki komutta çalışır."))
        return
    if target and os.access(target, os.X_OK):
        print()
        rerun(target, argv)
    sys.exit(0)


def rerun(target, argv):
    args = [target] + argv[1:]
    if os.name == "nt":
        sys.exit(subprocess.call(args))
    os.execv(target, args)


def start_update_check():
    """Etkileşimli açılışta son sürüm sorgusunu arka planda başlatır (komutu bekletmez); gerekmiyorsa None."""
    if not FROZEN or os.environ.get("CLSW_NO_UPDATE") or os.environ.get("CLSW_AFTER_UPDATE") \
            or not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None
    st = _update_state()
    now = time.time()
    if now - st.get("checked", 0) < CHECK_EVERY:
        return None
    box = {}

    def work():
        box["rel"], box["err"] = latest_release(timeout=3)
    t = threading.Thread(target=work, daemon=True)
    t.start()
    return t, box, st, now


def finish_update_check(handle, wait=1.5):
    """Komut bittikten sonra: yeni sürüm varsa çerçevede gösterip sorar; evet → kurar (komut yeniden çalışmaz).
    Sorgu wait sn içinde bitmediyse (yavaş ağ) bu sefer sormaz."""
    if not handle:
        return
    t, box, st, now = handle
    t.join(wait)
    if t.is_alive():
        return
    rel = box.get("rel")
    st["checked"] = now
    if rel:
        st["latest"] = fmt_version(rel["version"])
    _save_update_state(st)
    if not rel or rel["version"] <= parse_version(__version__) or not rel["asset"]:
        return
    tag = fmt_version(rel["version"])
    if now - (st.get("snoozed") or {}).get(tag, 0) < SNOOZE:
        return
    print()
    show_release(rel)
    via = install_method()
    if via:
        print(dim("Kurulum: " + {"npm": "npm", "brew": "Homebrew"}[via] + " · " + MANAGED_UPDATE[via]))
    try:
        answer = input("Şimdi güncelleyeyim mi? [E/h] ").strip().lower()
    except EOFError:
        answer = "h"
    if answer in ("", "e", "evet", "y", "yes"):
        if via:
            managed_update(via, None)
        else:
            do_install(rel)
            print(dim("Yeni sürüm bir sonraki komutta çalışır."))
        return
    st["snoozed"] = {tag: now}
    _save_update_state(st)
    print(dim("Atlandı; bu sürümü 24 saat sormayacağım. İstediğinde: clsw update"))


def maybe_offer_update(argv=None):
    """Sorguyu başlatıp sonucunu bekler (eş zamanlı kullanım ve testler için)."""
    finish_update_check(start_update_check(), wait=5)


def cmd_version():
    where = tilde(os.path.realpath(sys.executable if FROZEN else __file__))
    via = install_method()
    kind = {"npm": "npm", "brew": "Homebrew"}.get(via) or ("program" if FROZEN else "kaynak")
    print(f"clsw v{__version__}  " + dim(f"({kind}: {where})"))
    latest = parse_version(_update_state().get("latest"))
    if latest and latest > parse_version(__version__):
        print(yellow(f"GitHub'da {fmt_version(latest)} var: " + (MANAGED_UPDATE[via] if via else "clsw update")))


def cmd_install():
    """İndirilip açılan programı ~/.local/share/clsw'ye kurar; clsw ve claudeswap komutlarını bağlar."""
    if not FROZEN:
        die("install yalnız GitHub'dan indirilen programda çalışır (kaynak: python claudeswap.py).")
    if install_method():
        die(f"Bu clsw {install_method()} ile kurulu; ayrıca kurmaya gerek yok.")
    exe = place_bundle(bundle_dir(), __version__)
    where = link_commands()
    cleanup_versions()
    print(green(f"clsw v{__version__} kuruldu: {tilde(exe)}") + dim("  (eş: claudeswap)"))
    paths = [os.path.normcase(os.path.realpath(p)) for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    if os.path.normcase(os.path.realpath(where)) not in paths and not os.environ.get("CLSW_QUIET_PATH"):
        print(yellow(f"{tilde(where)} PATH'te değil; ekle ki her yerden clsw yazılabilsin."))


def cmd_update(args):
    rel, err = latest_release()
    if err:
        die(err)
    if rel["version"] <= parse_version(__version__):
        print(f"Güncel: v{__version__} (GitHub'daki son sürüm {fmt_version(rel['version'])})")
        return
    show_release(rel)
    if "--check" in args:
        return
    if not FROZEN:
        die("Kaynaktan çalışıyor; kaynak klasöründe git pull yap. Program kurulumu kendini günceller.")
    via = install_method()
    if via:
        managed_update(via, ["clsw", "version"])
    do_install(rel)
    _save_update_state({})


def dispatch(argv):
    """Komutu çalıştırır; hesabı değişen profilleri döner."""
    args = argv[1:]
    cmd = args[0] if args else "help"
    by_name = {s["name"]: s for s in SLOTS}
    if cmd.lower() in GENERATE_CMDS:
        cmd_generate(args[1:])
        return []
    if len(args) > 2:
        die("En fazla iki profil belirt. Kullanım: claudeswap help", 2)
    if len(args) == 2:
        if any(n not in by_name for n in args):
            die("İki profil için claude, claude2, claude3 … kullan (yenisi için: clsw generate <sayı>).", 2)
        return cmd_swap(by_name[args[0]], by_name[args[1]])
    if cmd in ("help", "-h", "--help", "yardım", "yardim"):
        print(render_help(HELP), end="")
    elif cmd in ("list", "ls", "liste"):
        cmd_list()
    elif cmd in ("process", "processes", "proc", "oturum", "oturumlar", "ps"):
        cmd_sessions()
    elif cmd in by_name:
        return cmd_swap(SLOTS[0], by_name[cmd])
    elif cmd in ("empty", "bos", "boş"):
        empty = next((s for s in SLOTS if is_empty(s)), None)
        if empty is None:
            die("Giriş yapılmamış profil yok.", 2)
        return cmd_swap(SLOTS[0], empty)
    else:
        return cmd_use(cmd)
    return []


GENERATE_CMDS = ("generate", "gen", "olustur", "oluştur", "yeni")
GENERATED_MARK = ".clsw-profile"     # clsw generate ile açılan profil (clsw list girişsiz de gösterir)


def launcher_text(n):
    """claudeN komutunun içeriği: Claude Code'u o profilin klasörüyle açar."""
    if os.name == "nt":
        return (f"@echo off\r\nrem clsw generate: claude{n} profili\r\nsetlocal\r\n"
                f'set "CLAUDE_CONFIG_DIR=%USERPROFILE%\\.claude{n}"\r\nclaude %*\r\n')
    return (f"#!/bin/sh\n# clsw generate: claude{n} profili\n"
            f'CLAUDE_CONFIG_DIR="$HOME/.claude{n}" exec claude "$@"\n')


def cmd_generate(args):
    """clsw generate N: mevcut en büyük numaradan sonra N yeni profil (klasör + claudeN komutu)."""
    if len(args) != 1 or not args[0].isdigit() or not 1 <= int(args[0]) <= 20:
        die("Kullanım: clsw generate <sayı>  (1–20; örn. clsw generate 3)", 2)
    count = int(args[0])
    start = max([1] + sorted(profile_numbers())) + 1
    bdir = os.path.join(HOME, ".local", "bin")
    os.makedirs(bdir, exist_ok=True)
    made = []
    for n in range(start, start + count):
        d = os.path.join(HOME, f".claude{n}")
        os.makedirs(d, mode=0o700, exist_ok=True)
        with open(os.path.join(d, GENERATED_MARK), "w") as f:
            f.write("clsw generate\n")
        cmd = os.path.join(bdir, f"claude{n}" + (".cmd" if os.name == "nt" else ""))
        if not os.path.exists(cmd):
            with open(cmd, "w", newline="") as f:
                f.write(launcher_text(n))
            if os.name != "nt":
                os.chmod(cmd, 0o755)
        made.append(n)
    names = ", ".join(f"claude{n}" for n in made)
    print(green(f"✓ {len(made)} yeni profil açıldı: {names}"))
    for n in made:
        print(f"  {bold(f'claude{n}')}  {dim(tilde(os.path.join(HOME, f'.claude{n}')))}")
    print(f"\nGiriş için: {bold(f'claude{made[0]}')} yaz, açılınca {bold('/login')} ile hesabına gir.")
    print(dim("Sonra hesapları görmek için: clsw list · birini claude'a getirmek için: clsw claude" + str(made[0])))
    paths = [os.path.normcase(os.path.realpath(x)) for x in os.environ.get("PATH", "").split(os.pathsep) if x]
    if os.path.normcase(os.path.realpath(bdir)) not in paths:
        print(yellow(f"{tilde(bdir)} PATH'te değil; claudeN komutları için PATH'e ekle."))
    elif os.name != "nt":
        print(dim("Komut bulunamazsa yeni terminal aç (ya da: hash -r)."))
    return made


def take_lock(f):
    if fcntl:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    else:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)


def cmd_web():
    sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
    import cost_web
    cost_web.attach(globals())
    cost_web.main()


def main(argv):
    if FROZEN and not install_method():
        cleanup_versions()
    cmd = argv[1].lower() if len(argv) > 1 else ""
    if cmd in VERSION_CMDS:
        return cmd_version()
    if cmd in UPDATE_CMDS:
        return cmd_update(argv[2:])
    if cmd == "web":
        return cmd_web()
    if cmd == "install":
        return cmd_install()
    update = start_update_check()             # GitHub sorgusu komutla aynı anda; soru komuttan sonra
    # Maliyet tablosu yalnız okur ve sürebilir (ilk taramada birkaç sn): hesap değişimini bekletmesin diye kilitsiz.
    if cmd in COST_CMDS:
        cmd_cost(argv[2:])
        finish_update_check(update)
        return
    # Seçim dahil tüm işlemi sıraya al; süreç ölürse kilit otomatik bırakılır.
    os.makedirs(STATE_ROOT, mode=0o700, exist_ok=True)
    fd = os.open(os.path.join(STATE_ROOT, "operation.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "a") as lock:
        deadline = time.monotonic() + 20
        while True:
            try:
                take_lock(lock)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    die("Başka bir claudeswap işlemi sürüyor; bitince tekrar dene.")
                time.sleep(0.1)
        changed = dispatch(argv)
    # Kilit bırakıldıktan sonra: claude -p sürerken başka bir claudeswap beklemesin.
    activate(changed)
    finish_update_check(update)


if __name__ == "__main__":
    try:
        main(sys.argv)
    except KeyboardInterrupt:
        sys.exit(130)
