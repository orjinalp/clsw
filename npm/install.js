#!/usr/bin/env node
// Platformun clsw programını (klasör arşivi) GitHub Release'ten (bu paketin sürümü) vendor/ altına açar.
// SHA-256 yayın sırasında pakete yazılan checksums.json ile doğrulanır. Açmak için sistemin tar'ı kullanılır
// (macOS, Linux ve Windows 10+ ile gelir; Windows'taki bsdtar zip de açar).
"use strict";
const { execFileSync } = require("child_process");
const crypto = require("crypto");
const fs = require("fs");
const https = require("https");
const path = require("path");

const REPO = "orjinalp/clsw";
const ROOT = __dirname;
const VERSION = require(path.join(ROOT, "package.json")).version;

function assetName() {
  const arch = { x64: "x64", arm64: "arm64" }[process.arch];
  const os = { darwin: "macos", linux: "linux", win32: "windows" }[process.platform];
  const known = ["clsw-macos-arm64", "clsw-linux-x64", "clsw-linux-arm64", "clsw-windows-x64"];
  const name = os && arch ? `clsw-${os}-${arch}` : null;
  if (!known.includes(name)) {
    throw new Error(`clsw bu platform için derlenmiyor: ${process.platform}-${process.arch}`);
  }
  return name + (os === "windows" ? ".zip" : ".tar.gz");
}

function binaryPath() {
  return path.join(ROOT, "vendor", "clsw", process.platform === "win32" ? "clsw.exe" : "clsw");
}

function get(url, redirects = 5) {
  return new Promise((resolve, reject) => {
    https
      .get(url, { headers: { "User-Agent": `clsw-npm/${VERSION}` } }, (res) => {
        if ([301, 302, 303, 307, 308].includes(res.statusCode) && res.headers.location && redirects > 0) {
          res.resume();
          resolve(get(new URL(res.headers.location, url).toString(), redirects - 1));
        } else if (res.statusCode !== 200) {
          res.resume();
          reject(new Error(`${url} → HTTP ${res.statusCode}`));
        } else {
          const chunks = [];
          res.on("data", (c) => chunks.push(c));
          res.on("end", () => resolve(Buffer.concat(chunks)));
          res.on("error", reject);
        }
      })
      .on("error", reject);
  });
}

async function install() {
  const name = assetName();
  const dest = binaryPath();
  let sums = {};
  try {
    sums = require(path.join(ROOT, "checksums.json"));
  } catch (_) {}
  const base = (process.env.CLSW_DOWNLOAD_URL || `https://github.com/${REPO}/releases/download/v${VERSION}`).replace(/\/$/, "");
  const url = `${base}/${name}`;
  const data = url.startsWith("file://") ? fs.readFileSync(new URL(url)) : await get(url);
  const sha = crypto.createHash("sha256").update(data).digest("hex");
  if (sums[name] && sums[name] !== sha) {
    throw new Error(`${name} SHA-256 tutmuyor (beklenen ${sums[name]}, gelen ${sha})`);
  }
  if (!sums[name] && VERSION !== "0.0.0") {
    throw new Error(`checksums.json içinde ${name} yok`);
  }
  const vendor = path.join(ROOT, "vendor");
  const tmp = path.join(ROOT, "vendor.tmp");
  fs.rmSync(tmp, { recursive: true, force: true });
  fs.mkdirSync(tmp, { recursive: true });
  const archive = path.join(tmp, name);
  fs.writeFileSync(archive, data);
  execFileSync("tar", ["-xf", archive, "-C", tmp], { stdio: "ignore" });
  fs.rmSync(archive);
  if (!fs.existsSync(path.join(tmp, "clsw", path.basename(dest)))) {
    throw new Error("arşivde clsw programı yok");
  }
  fs.rmSync(vendor, { recursive: true, force: true });
  fs.renameSync(tmp, vendor);
  return dest;
}

module.exports = { install, binaryPath };

if (require.main === module) {
  install().catch((e) => {
    // Kurulum durmasın: program ilk çalıştırmada yeniden indirilmeye çalışılır.
    console.warn(`clsw: program indirilemedi (${e.message}). İlk çalıştırmada tekrar denenecek.`);
  });
}
