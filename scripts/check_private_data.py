"""Check that no installation-specific data leaks into the files that would be committed.

The forbidden terms are derived at runtime from the private data itself (nothing sensitive is written
in this script):

- every tenant name, meter point name and serial of the private import mapping
  (``<private dir>/import-mapping.toml``),
- every serial and column description of the private spreadsheet (``<private dir>/*.xlsx``, sheet
  ``Odečty`` rows 2–3) and its readings with at least 4 digits (real consumption figures; years
  1900–2100 excluded) – reported separately as "reading", since a short number can match by chance,
- extra terms listed in ``<private dir>/forbidden-terms.txt`` (one per line, ``#`` comments; e.g.
  surnames that only occur in notes, the server's IP addresses),
- from the root ``.env``: every ``ADMIN_EMAILS`` address, the host name of ``BASE_URL`` and the
  ``ALLOWED_DOMAIN``, and the IP addresses the ``BASE_URL`` host resolves to (when DNS is reachable).

The would-be-committed tree is every file not matched by ``.gitignore`` (via ``rg --files
--no-require-git`` when ripgrep is installed, otherwise a built-in matcher), minus the private dir.
Paths are checked too (a file named after the host). Matching: serials and addresses exactly (not
inside longer numbers/names), names case-insensitively at a word start (so Czech inflected forms are
found too); names of up to three characters case-sensitively as whole words.

    uv run python scripts/check_private_data.py [--private-dir DIR] [--env FILE] [--root DIR]

Exit code 0 = clean, 1 = hits (printed as ``path:line: [term] text``), 2 = no private data to check.
Matches of real reading values are listed separately for a manual look (a 4-digit number can occur by
chance, e.g. in a computed test value); they fail the check only with ``--strict-numbers``. Lock files and
minified vendor files are not searched for them.
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import re
import shutil
import socket
import subprocess
import sys
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.private import ENV_VAR, MAPPING_FILE, private_dir  # noqa: E402

ALWAYS_SKIP = {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"}


# --- terms --------------------------------------------------------------------------------------
def _serial(v: object) -> str:
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def terms_from_private(pdir: Path) -> dict[str, str]:
    """term -> kind ("name" | "serial" | "literal" | "reading")."""
    terms: dict[str, str] = {}
    mapping = pdir / MAPPING_FILE
    if mapping.is_file():
        data = tomllib.loads(mapping.read_text(encoding="utf-8"))
        for t in data.get("tenants", []):
            terms[t["name"]] = "name"
        for c in data.get("columns", []):
            for key in ("serial", "serial_override"):
                if c.get(key):
                    terms[str(c[key])] = "serial"
            for key in ("point", "parent", "tenant"):
                if c.get(key):
                    terms[c[key]] = "name"
    for xlsx in sorted(pdir.glob("*.xlsx")):
        import openpyxl

        wb = openpyxl.load_workbook(xlsx, read_only=True, data_only=True)
        if "Odečty" in wb.sheetnames:
            ws = wb["Odečty"]
            for row_no, row in enumerate(ws.iter_rows(min_row=2, max_row=3, min_col=2, values_only=True), 2):
                for v in row:
                    if v not in (None, ""):
                        terms[_serial(v)] = "serial" if row_no == 2 else "name"
            for row in ws.iter_rows(min_row=5, min_col=2, values_only=True):
                for v in row:
                    if isinstance(v, int | float) and abs(v) >= 1000 and not 1900 <= v <= 2100:
                        terms.setdefault(_serial(v), "reading")
        wb.close()
    extra = pdir / "forbidden-terms.txt"
    if extra.is_file():
        for line in extra.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                terms[line] = "literal" if re.search(r"[\d.:@]", line) else "name"
    return terms


def terms_from_env(env: Path, *, resolve: bool = True) -> dict[str, str]:
    terms: dict[str, str] = {}
    if not env.is_file():
        return terms
    values: dict[str, str] = {}
    for line in env.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*?)\s*$", line)
        if m:
            values[m.group(1)] = m.group(2).strip().strip("'\"")
    for e in re.split(r"[,;\s]+", values.get("ADMIN_EMAILS", "")):
        if "@" in e:
            terms[e.lower()] = "literal"
    host = (urlsplit(values.get("BASE_URL", "")).hostname or "").lower()
    if host and host not in ("localhost", "127.0.0.1", "::1") and not host.endswith("example.org"):
        terms[host] = "literal"
        if resolve:
            try:
                for info in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP):
                    terms[str(info[4][0])] = "literal"
            except OSError:
                print(f"(DNS of {host} not resolvable – IP addresses only from forbidden-terms.txt)")
    domain = values.get("ALLOWED_DOMAIN", "").lower()
    if domain and not domain.endswith("example.org"):
        terms[domain] = "literal"
    return terms


def compile_term(term: str, kind: str) -> re.Pattern[str]:
    esc = re.escape(term)
    if kind == "serial":
        return re.compile(rf"(?<![0-9A-Za-z]){esc}(?![0-9])")
    if kind == "reading":  # a whole number, not part of a longer number / decimal / identifier
        return re.compile(rf"(?<![\w.,]){esc}(?![\d])")
    if kind == "literal":  # e-mails, hosts, domains, IPs: not part of a longer name/number
        return re.compile(rf"(?<![\w-]){esc}(?![\w-])", re.IGNORECASE)
    if len(term) <= 3:
        return re.compile(rf"(?<!\w){esc}(?!\w)")
    return re.compile(rf"(?<!\w){esc}", re.IGNORECASE)


# --- files --------------------------------------------------------------------------------------
Pattern = tuple[str, str, bool, bool]  # (base dir "a/b/", pattern, negated, dir only)


def _gitignore_patterns(directory: Path, base: str) -> list[Pattern]:
    out: list[Pattern] = []
    gi = directory / ".gitignore"
    if gi.is_file():
        for line in gi.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            neg = line.startswith("!")
            line = line.lstrip("!")
            dir_only = line.endswith("/")
            out.append((base, line.strip("/"), neg, dir_only))
    return out


def _ignored(rel: str, is_dir: bool, patterns: list[Pattern]) -> bool:
    """Subset of gitignore semantics (enough for this repo): later patterns win, ``!`` re-includes,
    a pattern with a slash is anchored to its .gitignore's directory, otherwise it matches any name."""
    ignored = False
    for base, pat, neg, dir_only in patterns:
        if (dir_only and not is_dir) or not rel.startswith(base):
            continue
        sub = rel[len(base) :]
        target = sub if "/" in pat else sub.rsplit("/", 1)[-1]
        if fnmatch.fnmatchcase(target, pat):
            ignored = not neg
    return ignored


def committed_files(root: Path) -> list[Path]:
    rg = shutil.which("rg")
    if rg:
        res = subprocess.run(
            [rg, "--files", "--hidden", "--no-require-git", "--glob", "!.git"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode in (0, 1):
            return sorted(root / p for p in res.stdout.splitlines() if p)
    files: list[Path] = []
    inherited: dict[str, list[Pattern]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        rel_dir = "" if rel_dir == "." else rel_dir + "/"
        patterns = inherited.pop(rel_dir, []) + _gitignore_patterns(Path(dirpath), rel_dir)
        dirnames[:] = [d for d in dirnames if d not in ALWAYS_SKIP and not _ignored(rel_dir + d, True, patterns)]
        for d in dirnames:
            inherited[rel_dir + d + "/"] = patterns
        files += [Path(dirpath) / f for f in filenames if not _ignored(rel_dir + f, False, patterns)]
    return sorted(files)


# --- main ---------------------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--private-dir", help=f"private data dir (default ${ENV_VAR}, else ./private)")
    ap.add_argument("--env", type=Path, default=ROOT / ".env", help="the installation's .env (default ./.env)")
    ap.add_argument("--root", type=Path, default=ROOT, help="tree to check (default: the project)")
    ap.add_argument("--no-dns", action="store_true", help="do not resolve the BASE_URL host")
    ap.add_argument("--strict-numbers", action="store_true", help="reading-value matches also fail the check")
    args = ap.parse_args(argv)

    pdir = private_dir(args.private_dir).resolve()
    terms = {**terms_from_private(pdir), **terms_from_env(args.env, resolve=not args.no_dns)}
    if not terms:
        print(f"Nothing to check: no private data in {pdir} and no {args.env}.", file=sys.stderr)
        return 2
    kinds = {k: sum(1 for v in terms.values() if v == k) for k in ("name", "serial", "literal", "reading")}
    print(f"Forbidden terms derived from {pdir} and {args.env}: {kinds}")
    patterns = [(t, compile_term(t, k)) for t, k in sorted(terms.items())]

    root = args.root.resolve()
    skip = {pdir, args.env.resolve()}
    hits = 0
    numbers: list[str] = []
    files = [
        f
        for f in committed_files(root)
        if not any(f.resolve() == s or s in f.resolve().parents for s in skip)
        and not ALWAYS_SKIP.intersection(f.relative_to(root).parts)
    ]
    for f in files:
        rel = f.relative_to(root).as_posix()
        for term, rx in patterns:
            if rx.search(rel):
                print(f"{rel}: [{term}] (file name)")
                hits += 1
        try:
            raw = f.read_bytes()
        except OSError:
            continue
        if b"\0" in raw[:8192]:
            text = raw.decode("latin-1")  # binary: still look for longer ASCII serials / addresses
            for term, rx in patterns:
                if terms[term] != "name" and len(term) >= 5 and rx.search(text):
                    print(f"{rel}: [{term}] (binary file)")
                    hits += 1
            continue
        skip_numbers = rel.endswith((".lock", ".min.js"))
        for no, line in enumerate(raw.decode("utf-8", "replace").splitlines(), 1):
            for term, rx in patterns:
                if terms[term] == "reading":
                    if not skip_numbers and rx.search(line):
                        numbers.append(f"{rel}:{no}: [{term}] {line.strip()[:160]}")
                elif rx.search(line):
                    print(f"{rel}:{no}: [{term}] {line.strip()[:160]}")
                    hits += 1
    print(f"{len(files)} files checked, {hits} hit(s).")
    if numbers:
        print(f"{len(numbers)} number(s) equal to a real reading – check manually:")
        print("\n".join(f"  {n}" for n in numbers))
    return 1 if hits or (args.strict_numbers and numbers) else 0


if __name__ == "__main__":
    raise SystemExit(main())
