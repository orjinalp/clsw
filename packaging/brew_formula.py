"""Homebrew formülü üretir: python packaging/brew_formula.py v1.2.3 SHA256SUMS > Formula/clsw.rb

SHA256SUMS: Release'teki `sha256  dosya` satırları. Formül orjinalp/homebrew-tap reposunda durur;
release.yml her sürümde bunu çalıştırıp tap'e push'lar.
"""
import sys

TEMPLATE = '''# release.yml üretir (orjinalp/clsw packaging/brew_formula.py); elle düzenleme.
class Clsw < Formula
  desc "Claude Code profilleri arasında hesap değiştirme, limit ve maliyet görüntüleme"
  homepage "https://github.com/orjinalp/clsw"
  version "{version}"

  on_macos do
    on_arm do
      url "{base}/clsw-macos-arm64.tar.gz"
      sha256 "{clsw-macos-arm64.tar.gz}"
    end
  end

  on_linux do
    on_intel do
      url "{base}/clsw-linux-x64.tar.gz"
      sha256 "{clsw-linux-x64.tar.gz}"
    end
    on_arm do
      url "{base}/clsw-linux-arm64.tar.gz"
      sha256 "{clsw-linux-arm64.tar.gz}"
    end
  end

  def install
    # Arşiv clsw/ klasörüdür (program + _internal/); Homebrew içine girip açar.
    libexec.install Dir["*"]
    bin.install_symlink libexec/"clsw"
    bin.install_symlink libexec/"clsw" => "claudeswap"
  end

  test do
    assert_match "v#{{version}}", shell_output("#{{bin}}/clsw version")
  end
end
'''


def main(tag, sums_path):
    sums = {}
    for line in open(sums_path, encoding="utf-8"):
        if line.strip():
            digest, name = line.split()
            sums[name.lstrip("*")] = digest
    need = ("clsw-macos-arm64.tar.gz", "clsw-linux-x64.tar.gz", "clsw-linux-arm64.tar.gz")
    missing = [n for n in need if n not in sums]
    if missing:
        sys.exit(f"SHA256SUMS içinde yok: {', '.join(missing)}")
    out = TEMPLATE.replace("{{", "\0").replace("}}", "\1")   # str.format değil: anahtarlarda nokta var
    for n in need:
        out = out.replace("{" + n + "}", sums[n])
    out = out.replace("{version}", tag.lstrip("v"))
    out = out.replace("{base}", f"https://github.com/orjinalp/clsw/releases/download/{tag}")
    sys.stdout.write(out.replace("\0", "{").replace("\1", "}"))


if __name__ == "__main__":
    main(*sys.argv[1:])
