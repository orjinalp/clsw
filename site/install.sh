#!/bin/sh
# clsw kurulumu (macOS, Linux):  curl -fsSL https://github.com/orjinalp/clsw/releases/latest/download/install.sh | sh
# Ayna: CLSW_DOWNLOAD_URL=<programların ve SHA256SUMS'in durduğu adres>
# Belirli sürüm: curl -fsSL https://github.com/orjinalp/clsw/releases/latest/download/install.sh | CLSW_VERSION=1.2.3 sh
set -eu

REPO="orjinalp/clsw"
BIN_DIR="$HOME/.local/bin"

say() { printf '%s\n' "$*"; }
fail() { printf 'clsw kurulumu: %s\n' "$*" >&2; exit 1; }

case "$(uname -s)" in
  Darwin) os=macos ;;
  Linux) os=linux ;;
  *) fail "desteklenmeyen sistem: $(uname -s) (Windows: install.cmd)" ;;
esac
case "$(uname -m)" in
  arm64 | aarch64) arch=arm64 ;;
  x86_64 | amd64) arch=x64 ;;
  *) fail "desteklenmeyen işlemci: $(uname -m)" ;;
esac
if [ "$os" = macos ] && [ "$arch" = x64 ]; then
  # Rosetta altındaki kabuk x86_64 der; makine Apple Silicon ise arm64 programı çalışır.
  if [ "$(sysctl -n hw.optional.arm64 2>/dev/null || echo 0)" = 1 ]; then
    arch=arm64
  else
    fail "Intel Mac için program derlenmiyor (yalnız Apple Silicon)."
  fi
fi
asset="clsw-$os-$arch.tar.gz"

if [ -n "${CLSW_DOWNLOAD_URL:-}" ]; then
  base="${CLSW_DOWNLOAD_URL%/}"                  # ayna ya da yerel deneme (file://)
elif [ -n "${CLSW_VERSION:-}" ]; then
  base="https://github.com/$REPO/releases/download/v${CLSW_VERSION#v}"
else
  base="https://github.com/$REPO/releases/latest/download"
fi

command -v curl >/dev/null 2>&1 || fail "curl gerekli"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT INT TERM

say "clsw indiriliyor ($asset)…"
curl -fL --progress-bar -o "$tmp/$asset" "$base/$asset" || fail "indirilemedi: $base/$asset"
curl -fsSL -o "$tmp/SHA256SUMS" "$base/SHA256SUMS" || fail "SHA256SUMS indirilemedi"

want="$(awk -v n="$asset" '$2 == n || $2 == "*" n { print $1 }' "$tmp/SHA256SUMS")"
[ -n "$want" ] || fail "SHA256SUMS içinde $asset yok"
if command -v sha256sum >/dev/null 2>&1; then
  got="$(sha256sum "$tmp/$asset" | awk '{print $1}')"
else
  got="$(shasum -a 256 "$tmp/$asset" | awk '{print $1}')"
fi
[ "$want" = "$got" ] || fail "SHA-256 tutmuyor (beklenen $want, gelen $got)"

mkdir "$tmp/x"
tar -xzf "$tmp/$asset" -C "$tmp/x" || fail "arşiv açılamadı"
CLSW_QUIET_PATH=1 "$tmp/x/clsw/clsw" install
# macOS yeni programı ilk açılışta tarar (birkaç saniye); kullanıcının ilk komutu beklemesin diye şimdi.
say "İlk açılış hazırlanıyor…"
CLSW_NO_UPDATE=1 "$HOME/.local/share/clsw/current/clsw" version >/dev/null 2>&1 || true

# Program ~/.local/share/clsw altına kuruldu; komutlar ~/.local/bin'de.
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    line='export PATH="$HOME/.local/bin:$PATH"'
    case "${SHELL:-}" in
      */zsh) rc="$HOME/.zshrc" ;;
      */bash) [ "$os" = macos ] && rc="$HOME/.bash_profile" || rc="$HOME/.bashrc" ;;
      *) rc="$HOME/.profile" ;;
    esac
    if ! grep -qs '.local/bin' "$rc"; then
      printf '\n# clsw\n%s\n' "$line" >>"$rc"
      say "PATH'e ~/.local/bin eklendi ($rc). Yeni terminal aç ya da: $line"
    else
      say "~/.local/bin PATH'te değil; yeni terminal aç ya da: $line"
    fi
    ;;
esac
say "Hazır. Başlamak için: clsw list"
