# clsw

Birden fazla Claude Code hesabını tek makinede kullananlar için: `claude`, `claude2` … `claude10` profilleri
arasında hesapları tek komutla yer değiştirir, her hesabın kalan limitini, açık oturumları ve harcamayı gösterir.
Kurulum için Python ya da başka bir şey gerekmez.

## Kurulum

```sh
# macOS (Apple Silicon) / Linux
curl -fsSL https://github.com/orjinalp/clsw/releases/latest/download/install.sh | bash

# Windows (PowerShell)
irm https://github.com/orjinalp/clsw/releases/latest/download/install.ps1 | iex

# npm
npm install -g @orjinalp/clsw

# Homebrew
brew install orjinalp/tap/clsw
```

## Kullanım

```sh
clsw list          # hesaplar, limitler, reset hakları
clsw claude3       # claude ile claude3'ün hesaplarını değiştir
clsw ali@example.com   # o hesabı claude profiline getir
clsw process       # açık Claude oturumları
clsw cost          # istem başına maliyet
clsw help          # tüm komutlar
```

Yeni sürüm çıkınca `clsw` açılışta sorar ve günceller.
