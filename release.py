"""Yeni sürüm çıkarır: python release.py 1.0.1 "değişiklik 1" "değişiklik 2"

Sırayla: çalışma ağacı temiz ve main'de mi, testler, __version__ yazılır, commit, notlu etiket, push.
Programları GitHub Actions derler ve Release'i o açar (.github/workflows/release.yml).
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "claudeswap.py"


def run(*cmd, capture=False):
    r = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=capture)
    if r.returncode:
        sys.exit(f"Durdu: {' '.join(cmd)}\n{(r.stdout or '') + (r.stderr or '')}")
    return (r.stdout or "").strip()


def main(args):
    for st in (sys.stdout, sys.stderr):
        st.reconfigure(encoding="utf-8", errors="replace")
    if len(args) < 2 or not re.fullmatch(r"\d+\.\d+\.\d+", args[0]):
        sys.exit(__doc__)
    version, notes = args[0], args[1:]
    tag = "v" + version
    src = SRC.read_text(encoding="utf-8")
    current = re.search(r'^__version__ = "(.+)"', src, re.M).group(1)
    as_tuple = lambda v: tuple(map(int, v.split(".")))
    if as_tuple(version) <= as_tuple(current):
        sys.exit(f"{version}, mevcut sürümden ({current}) büyük olmalı.")
    if run("git", "branch", "--show-current", capture=True) != "main":
        sys.exit("main dalında değilsin.")
    if run("git", "status", "--porcelain", capture=True):
        sys.exit("Commit edilmemiş değişiklik var; önce onları commit et.")
    if run("git", "tag", "-l", tag, capture=True):
        sys.exit(f"{tag} etiketi zaten var.")
    run(sys.executable, "-m", "unittest", "discover", "-s", "tests")
    SRC.write_text(src.replace(f'__version__ = "{current}"', f'__version__ = "{version}"', 1),
                   encoding="utf-8", newline="\n")
    run("git", "commit", "-qam", f"Sürüm {tag}")
    run("git", "tag", "-a", tag, "-m", "\n".join(f"- {n}" for n in notes))
    run("git", "push", "-q", "origin", "main", tag)
    print(f"{tag} push'landı. Derleme: gh run watch --repo orjinalp/clsw (bitince Release açılır)")


if __name__ == "__main__":
    main(sys.argv[1:])
