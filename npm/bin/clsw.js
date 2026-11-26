#!/usr/bin/env node
// npm başlatıcısı: platformun clsw programını çalıştırır (yoksa önce indirir).
// Program "güncelle" derse (çıkış kodu 75) kapanınca npm ile günceller ve aynı komutu yeni sürümle yeniden açar:
// Windows'ta çalışan .exe kilitlidir, npm onun üzerine yazamaz; güncellemeyi bu yüzden başlatıcı yapar.
"use strict";
const { spawn, spawnSync } = require("child_process");
const fs = require("fs");
const path = require("path");
const { install, binaryPath } = require("../install.js");

const UPDATE_EXIT = 75;
const PACKAGE = "@orjinalp/clsw";

function run(bin, args, env) {
  return new Promise((resolve) => {
    const child = spawn(bin, args, { stdio: "inherit", env });
    // Ctrl+C terminalde zaten programa da gider; ikinci kez göndermeyelim.
    const ignore = () => {};
    const forward = (sig) => () => {
      try {
        child.kill(sig);
      } catch (_) {}
    };
    const handlers = { SIGINT: ignore, SIGTERM: forward("SIGTERM"), SIGHUP: forward("SIGHUP") };
    for (const [sig, h] of Object.entries(handlers)) process.on(sig, h);
    const done = (result) => {
      for (const [sig, h] of Object.entries(handlers)) process.removeListener(sig, h);
      resolve(result);
    };
    child.on("error", (e) => done({ error: e }));
    child.on("exit", (code, signal) => done({ code, signal }));
  });
}

function npmUpdate() {
  const npm = process.platform === "win32" ? "npm.cmd" : "npm";
  const spec = process.env.CLSW_NPM_PACKAGE || `${PACKAGE}@latest`;   // ayna/deneme için değiştirilebilir
  // Windows'ta npm.cmd kabukla çalışır: paket adı ya da .tgz yolu dışında hiçbir karaktere izin verme.
  if (!/^(@orjinalp\/clsw(@[\w.\-]+)?|[\w.\-\/\\:]+\.tgz)$/.test(spec)) {
    console.error("clsw: CLSW_NPM_PACKAGE geçersiz");
    return false;
  }
  const r = spawnSync(npm, ["install", "-g", spec], { stdio: "inherit", shell: process.platform === "win32" });
  return r.status === 0;
}

async function main() {
  let bin = binaryPath();
  if (!fs.existsSync(bin)) {
    try {
      bin = await install();
    } catch (e) {
      console.error(`clsw: program indirilemedi: ${e.message}`);
      process.exit(1);
    }
  }
  const args = process.argv.slice(2);
  const env = Object.assign({}, process.env, { CLSW_INSTALLER: "npm", CLSW_LAUNCHER_UPDATE: "1" });
  const r = await run(bin, args, env);
  if (r.error) {
    console.error(`clsw: çalıştırılamadı: ${r.error.message}`);
    process.exit(1);
  }
  if (r.signal) {
    process.kill(process.pid, r.signal);
    return;
  }
  if (r.code === UPDATE_EXIT && !process.env.CLSW_AFTER_UPDATE) {
    if (!npmUpdate()) {
      console.error(`clsw: güncelleme başarısız oldu; elle: npm install -g ${PACKAGE}@latest`);
      process.exit(1);
    }
    // Yeni paket bu betiğin yerine kuruldu: aynı komutu yeni başlatıcıyla çalıştır (bir kez).
    const again = spawnSync(process.execPath, [path.resolve(__filename), ...args], {
      stdio: "inherit",
      env: Object.assign({}, process.env, { CLSW_AFTER_UPDATE: "1" }),
    });
    process.exit(again.status === null ? 1 : again.status);
  }
  process.exit(r.code === null ? 1 : r.code);
}

main();
