# LKHC Odečty

Webová aplikace pro odečty elektroměrů na letišti LKHC (přihlášení účty Google Workspace aeroklubu).
Nahrazuje Google Sheet `Odečty elektřiny.xlsx`:

- **Měsíční obchůzka v mobilu** (`/obchuzka`, instalovatelná PWA, funguje i offline): vyfotit elektroměr,
  zadat / potvrdit stav. Nebo nahrát **dávku fotek** z galerie (`/odecty/davka`, datum z EXIF),
  případně fotky **sdílet z WhatsAppu** (stavy od nájemců).
- **Rozpoznání stavů z fotek** vision modelem (OpenRouter nebo lokální Ollama / llama.cpp) –
  nic se nezapíše bez potvrzení člověkem ve **frontě kontroly** (`/odecty/kontrola`). Karty kontroly se
  skládají až podle rozpoznaných elektroměrů: jedna karta = fotky téhož elektroměru (nebo elektroměrů
  vyfocených společně na jedné fotce) z jednoho dne, seřazené podle času obchůzky; fotka bez rozpoznaného
  elektroměru je samostatná karta s návrhy z fotek pořízených těsně před/po. Čas pořízení fotky nikdy
  nespojuje různé elektroměry.
- **Přehledy**: spotřeba po nájemcích a měřicích místech v zarovnaných kalendářních měsících
  (stav na hranici je skutečný odečet, nebo dopočtený z okolních odečtů; hodnoty interpolované mezi dvěma
  odečty se neoznačují, značkou **≈** je vyznačen jen *předběžný* extrapolovaný odhad za posledním
  odečtem, který se ještě změní), bilance hlavního elektroměru, upozornění na chybějící odečty a anomálie.
- **Zbytkový nájemce** (hlavní elektroměr − ostatní) se všude ukazuje jen v součtu **T1+T2**: jednotarifní
  podružné elektroměry se počítají celé do T1, takže rozdělení zbytku na tarify nedává smysl (jeho T1 by
  vycházelo příliš nízko, v zimě i záporně, T2 příliš vysoko). Surový pohled „po dnech odečtů“ (jako list
  v původní tabulce) zůstává po tarifech.
- **Roční vyúčtování** pro nájemce: tabulka po obdobích × nájemcích × T1/T2, protokoly, export XLSX,
  tisková verze (PDF přes tisk prohlížeče), finalizace se snapshotem (pozdější opravy odečtů se do
  uzavřeného roku nepropíší).
- **Správa** (`/sprava`): pro adminy nájemci, měřicí místa, elektroměry (výměny, desetinná místa displeje),
  alokace míst nájemcům, uživatelé a role, audit všech změn; pro všechny (i čtenáře) odkazy na přehledy
  **Místa**, **Nájemci** a **Fotky**.
- Hlavní menu: Přehled, Odečty, Spotřeba, Obchůzka, Dávka (jen admin), Kontrola, Vyúčtování, Správa.
  Data se zobrazují česky bez mezer: `3.7.2026`, `3.7.2026 14:05`, měsíc `7.2026`.

Specifikace: [`docs/SPEC.md`](docs/SPEC.md), rozhraní služeb a výpočtů: [`docs/SERVICES.md`](docs/SERVICES.md).

Technicky: Python 3.15, FastAPI, SQLAlchemy + Alembic, SQLite (WAL) a fotky v jednom datovém adresáři,
serverově renderované šablony (Jinja2 + HTMX + Alpine.js, Chart.js), bez Node build kroku. Jeden Docker
kontejner za vaší reverse proxy.

---

## 1. První nasazení (VPS, Docker)

> Produkční nasazení na VPS (sdílená reverse proxy, nasazovací skripty, zálohy mimo server) popisuje
> [`deploy/README.md`](deploy/README.md); níže je obecný postup s `docker-compose.yml` v kořeni repozitáře.

### 1.1 Google přihlášení (OAuth klient)

Přihlásit se mohou jen účty Google Workspace domény z `ALLOWED_DOMAIN` (kontroluje se na serveru claim `hd`
i `email_verified`; bez nastavené domény se nepřihlásí nikdo).

1. [Google Cloud Console](https://console.cloud.google.com/) → vytvořte projekt **v organizaci vašeho Workspace**
   (přihlášeni jako správce Workspace).
2. *APIs & Services → OAuth consent screen* (Google Auth Platform → Branding/Audience):
   typ uživatelů **Internal** (aplikace jen pro účty vaší domény, bez ověřování Googlem), název „LKHC Odečty“,
   rozsahy stačí základní `openid`, `email`, `profile`.
3. *Credentials → Create credentials → OAuth client ID* → typ **Web application**.
   - **Authorized redirect URI**: `https://<vaše-doména>/auth/callback`
     (přesně `BASE_URL` + `/auth/callback`, např. `https://odecty.example.org/auth/callback`).
   - Pro lokální vývoj s Googlem případně i `http://localhost:8000/auth/callback`.
   - „Authorized JavaScript origins“ nejsou potřeba.
4. Client ID a Client secret vložte do `.env` (`GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`).

### 1.2 Konfigurace

```sh
git clone … lkhc-odecty && cd lkhc-odecty        # nebo zkopírujte repozitář na server
cp .env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # → SECRET_KEY
nano .env
```

Povinně: `SECRET_KEY`, `BASE_URL` (veřejná https adresa), `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`,
`ALLOWED_DOMAIN` (doména Workspace), `ADMIN_EMAILS` (e‑maily prvních adminů, oddělené čárkou). Všechny proměnné jsou vysvětlené
v [`.env.example`](.env.example). Aplikace odmítne nastartovat s výchozím klíčem, bez `ALLOWED_DOMAIN`
nebo s `AUTH_DEV_BYPASS=1`, pokud `BASE_URL` není localhost.

### 1.3 Spuštění

```sh
docker compose up -d --build
docker compose logs -f app        # „Migrace … hotová“ / „Databáze je aktuální“, pak Uvicorn running
```

- Data (databáze `app.db`, `photos/`, `backups/`) jsou v pojmenovaném volume `lkhc-data` → `/data`.
- **Migrace databáze** spouští vstupní skript kontejneru automaticky při každém startu
  (`python -m app.migrate`); před migrací existující databáze uloží její snímek do
  `/data/backups/pre-migrate-<revize>-<čas>.db`.
- Kontejner poslouchá jen na `127.0.0.1:8000` hostitele; healthcheck volá `GET /healthz`
  (monitoring používejte s GET, ne HEAD).
- Běží **jeden** worker uvicornu – fronta rozpoznávání fotek běží v procesu aplikace a SQLite má
  jednoho zapisovatele. Nespouštějte víc replik.

### 1.4 Reverse proxy (TLS)

Caddy (sám zajistí certifikát a pošle `X-Forwarded-Proto`):

```caddy
odecty.example.org {
    encode gzip
    request_body {
        max_size 50MB
    }
    reverse_proxy 127.0.0.1:8000
}
```

nginx: `proxy_pass http://127.0.0.1:8000;`, `proxy_set_header Host $host;`,
`proxy_set_header X-Forwarded-Proto $scheme;`, `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;`,
`client_max_body_size 50m;`.

Uvicorn běží s `--proxy-headers` a v `docker-compose.yml` je `FORWARDED_ALLOW_IPS="*"` – to je
v pořádku jen proto, že port je publikovaný pouze na `127.0.0.1` (dosáhne na něj jen proxy). Díky tomu
aplikace generuje `https://` odkazy. Pokud proxy běží v jiném kontejneru, viz komentář v `docker-compose.yml`.

### 1.5 Import původní tabulky

Data konkrétní instalace (původní tabulka, mapování jejích sloupců na nájemce a místa, potvrzené opravy)
do repozitáře ani do image nepatří. Leží v **soukromém adresáři** `private/` (v `.gitignore`; jinde ho
určí proměnná `LKHC_PRIVATE_DIR` nebo volba `--private-dir`):

- `private/<tabulka>.xlsx` – export Google Sheetu,
- `private/import-mapping.toml` – který sloupec listu `Odečty` patří kterému nájemci / místu, role
  hlavního a kontrolního elektroměru, výměny a duplicitní výrobní čísla, barvy nájemců a potvrzené opravy
  zdrojových dat. Formát popisuje docstring [`app/importer/mapping.py`](app/importer/mapping.py), vzor je
  [`app/importer/import-mapping.example.toml`](app/importer/import-mapping.example.toml).

Jednou po prvním startu (tabulka se do image nekopíruje, předá se na stdin – funguje i na hostitelích se
SELinuxem, kde by obyčejný bind mount souboru skončil „Permission denied“; mapování se čte z připojeného
soukromého adresáře, `,z` je potřeba jen se SELinuxem):

```sh
docker compose run --rm -T -v "$PWD/private:/private:ro,z" app \
    python -m app.importer --private-dir /private - < "private/Odečty elektřiny.xlsx"
```

Importuje list `Odečty` (nájemci, měřicí místa, elektroměry, alokace, odečty). Chybí-li mapování, import
skončí srozumitelnou chybou (`--mapping SOUBOR` ho zadá přímo). **Potvrzené opravy zdrojových dat**
(`[[date_corrections]]` v mapování, např. překlep v datu řádku) se aplikují automaticky a import je vypíše.
Samotné XLSX se nemění; `--bez-oprav` importuje tabulku přesně tak, jak je. Opakovaný import
je idempotentní, ale **odmítne se spustit, jakmile jsou v databázi data zadaná v aplikaci** (mohl by
obnovit smazané odečty/alokace nebo zdvojit přejmenovaný elektroměr). Vědomě jen s `--force` a po záloze.

### 1.6 První přihlášení a co nastavit

1. Otevřete `BASE_URL`, přihlaste se Googlem účtem z `ADMIN_EMAILS` → jste admin. Ostatní uživatelé
   z domény se při prvním přihlášení založí jako **čtenáři**; role mění admin v *Správa → Uživatelé*.
2. **Desetinná místa displeje**: import zakládá všechny elektroměry s `0` desetinnými místy. U elektroměrů
   s LCD displejem, který ukazuje desetiny (např. Maneler, Inepro, Schrack), je nastavte v
   *Správa → Měřicí místa a elektroměry → elektroměr* („Desetinná místa na displeji“, případně „Nápověda ke
   čtení displeje“). Dokud to nastavené není, kontrola hlásí u takových odečtů „posunutou desetinnou čárku“
   – varování obsahuje odkaz přímo na nastavení daného elektroměru.
3. Zkontrolujte alokace (*Správa → Alokace*) a poznámky z importu (např. elektroměr s výrobním číslem
   doplněným o `-B`, pokud je v tabulce stejné číslo dvakrát – ověřit na místě).

### 1.7 Oprava data odečtů (překlep v datu)

Datum odečtu v UI změnit nejde. Pro potvrzený překlep přesuňte všechny odečty (a označení „přeskočeno“)
daného dne nástrojem (nejdřív sám udělá zálohu, vše zapíše do auditu, při kolizi s existujícím odečtem
v cílový den nic nezmění):

```sh
docker compose exec app python -m app.tools.move_reading_date --from 2026-02-03 --to 2026-03-03 --dry-run
docker compose exec app python -m app.tools.move_reading_date --from 2026-02-03 --to 2026-03-03 --note "překlep"
```

Jde-li o chybu ve zdrojové tabulce, přidejte ji i do `[[date_corrections]]` soukromého
`private/import-mapping.toml`, aby ji opakovaný import nevrátil.

## 2. Rozpoznávání stavů z fotek

Bez konfigurace (`EXTRACTOR=none`) se stavy zadávají ručně, fotky se jen ukládají. Pro automatické čtení
nastavte OpenAI‑kompatibilní endpoint s podporou obrázků (v `.env`, pak `docker compose up -d`).
Modelu se posílá jen zmenšený JPEG (delší strana `EXTRACTOR_MAX_SIDE`, bez EXIF/GPS) a seznam známých
elektroměrů (výrobní čísla, názvy míst, poslední stavy) – žádné e‑maily ani poznámky.

> Přesnost rozpoznávání zatím nebyla změřena na skutečném modelu. Před ostrým použitím spusťte
> vyhodnocení (2.3) na fotkách z obchůzek a model vyberte podle výsledku.

### 2.1 OpenRouter

```dotenv
EXTRACTOR=openai_compat
EXTRACTOR_BASE_URL=https://openrouter.ai/api/v1
EXTRACTOR_API_KEY=sk-or-...
EXTRACTOR_MODEL=google/gemini-4-argon
```

### 2.2 Lokální model (Ollama / llama.cpp)

Ollama na hostiteli:

```sh
ollama pull qwen2.5vl:7b
# Ollama musí poslouchat i pro kontejnery: v systemd unitě Environment="OLLAMA_HOST=0.0.0.0"
```

V `docker-compose.yml` odkomentujte `extra_hosts: - "host.docker.internal:host-gateway"` a v `.env`:

```dotenv
EXTRACTOR=openai_compat
EXTRACTOR_BASE_URL=http://host.docker.internal:11434/v1
EXTRACTOR_API_KEY=
EXTRACTOR_MODEL=qwen2.5vl:7b
EXTRACTOR_TIMEOUT=300
```

llama.cpp: `llama-server -m model.gguf --mmproj mmproj.gguf --host 0.0.0.0 --port 8080`, pak
`EXTRACTOR_BASE_URL=http://host.docker.internal:8080/v1`. Malé modely na CPU jsou pomalé (desítky sekund na
fotku) – nevadí, rozpoznávání běží na pozadí a nahrávání na něj nečeká. Fotky ve frontě se po restartu
dokončí.

### 2.3 Vyhodnocení modelu na složce fotek

```sh
# lokálně (z repozitáře; nápovědy bere z DB v DATA_DIR)
EXTRACTOR=openai_compat EXTRACTOR_BASE_URL=... EXTRACTOR_API_KEY=... EXTRACTOR_MODEL=... \
    uv run python -m app.extraction.eval ~/fotky/obchuzka.zip --out vysledek.json

# v kontejneru (konfigurace z .env, DB z volume)
docker compose run --rm -v /cesta/k/fotkam:/fotky:ro,z app \
    python -m app.extraction.eval /fotky --out /data/eval.json
```

(`,z` je potřeba jen na hostitelích se SELinuxem – Fedora, RHEL; Docker tím složce změní SELinux štítek.)

Vstup je složka nebo ZIP (např. export z Immich). `--no-hints` / `--no-values` vyhodnotí bez nápověd.
Nic se nezapisuje kromě volitelného reportu.

**Se skutečnými hodnotami** (sada fotek z jednoho dne, pro který jsou v databázi odečty – fotky ani pravdu
nikdo ručně neoznačuje, pravda se vezme z databáze):

```sh
# jednou: pravda po elektroměrech k datu fotek (soukromá data → private/)
uv run python -m app.extraction.truth 2026-03-01 --out private/truth-2026-03-01.json
# s klíčem k modelu (OpenRouter / Ollama, viz výše); fotky jsou v examples/ (také mimo git):
EXTRACTOR=openai_compat EXTRACTOR_BASE_URL=... EXTRACTOR_API_KEY=... EXTRACTOR_MODEL=... \
    uv run python -m app.extraction.eval examples/<export-fotek>.zip \
    --expected private/truth-2026-03-01.json --out private/eval-MODEL.json
```

Vypíše výsledek po fotkách (co model přečetl, ke kterému elektroměru to app přiřadila a jak to dopadlo),
stav po elektroměrech (`correct` / `wrong` / `mixed` / `unreadable` / `not_found`) a počet **jistě špatných**
hodnot (model si byl jistý, ale číslo nesedí – nejhorší případ). Sheet obsahuje celé kWh oříznuté z displeje,
proto se hodnota, která sedí až po oříznutí desetin, počítá jako správná (`truncated`, zvlášť od `exact`);
`scaled` znamená, že číslo sedí jen s posunutou desetinnou čárkou – nejspíš je u elektroměru špatně nastavený
počet desetinných míst. Nápovědy pro model se berou jen z odečtů *před* dnem fotek, takže pravda do promptu
neprosákne. Bez klíče lze cestu vyzkoušet s `EXTRACTOR=fake` (vše vyjde „nečitelné“).

Další volby: `--concurrency N` (souběžná volání modelu, pozor na limity – OpenRouter má u nových účtů
např. 20 požadavků/min na model), `--max-side PX` (velikost obrázku pro model), `--autocontrast` (roztažení
kontrastu – pomáhá u zamlženého skla), `--verify` / `--no-verify` (druhé čtení, viz 2.4; bez volby podle
`EXTRACTOR_VERIFY`). `--replay private/eval-MODEL.json` model **nevolá**: znovu použije jeho
odpovědi z dřívějšího reportu a jen je znovu spáruje a vyhodnotí – tak se zdarma změří vliv nastavení
elektroměrů (desetinná místa, nápověda ke čtení displeje, výr. číslo) nebo změn párování. Souhrn obsahuje
i tokeny a cenu (`cost_usd`, pokud ji endpoint vrací – OpenRouter ano), počet hodnot nepřiřazených k žádnému
elektroměru (`unassigned`) a `plausible_wrong` = jistě špatné hodnoty, které navíc leží v očekávaném rozsahu,
takže je nezachytí ani kontrola věrohodnosti ve frontě kontroly.

### 2.4 Druhé čtení (`EXTRACTOR_VERIFY`)

```dotenv
EXTRACTOR_VERIFY=1
```

Každou fotku, na které model přečetl číslice, pošle aplikace modelu **ještě jednou** (nezávislý požadavek se
stejným obrázkem i promptem; první odpověď model nevidí). Hodnota se bere jako jistá jen tehdy, když ji druhé
čtení zopakuje (stejné číslice u stejného elektroměru a registru; elektroměry obou čtení se párují podle
výrobního čísla, jinak podle pořadí, pokud jich obě čtení viděla stejně). Jinak – i když druhý požadavek
selže (chyba, timeout, useknutá odpověď) – dostane hodnota příznak **„model si není jistý“**: ve frontě kontroly
se nedá hromadně potvrdit, varování je nutné odškrtnout a u hodnoty je vidět, co ukázalo druhé čtení
(obě odpovědi jsou i v detailu fotky); v obchůzce je předvyplněná hodnota označená stejným varováním.
Fotka bez přečtených číslic se podruhé neposílá.

**Zdvojnásobí cenu (i čas) rozpoznání každé fotky.** Spotřeba tokenů a cena obou volání se sčítá
(`raw.usage` ve výsledku fotky). Výchozí je vypnuto (`EXTRACTOR_VERIFY=0`).

## 3. Mobil: obchůzka a sdílení z WhatsAppu

1. V Androidu otevřete v **Chrome** `https://<doména>/obchuzka`, přihlaste se.
2. Menu ⋮ → **Instalovat aplikaci** / *Přidat na plochu*. Ikona „LKHC Odečty“ otevře obchůzku přes celou
   obrazovku. Seznam míst a rozpracované odečty fungují i bez signálu (fronta se odešle po připojení,
   stav fronty je vidět nahoře).
3. **Sdílení z WhatsAppu** (stavy, které posílají nájemci): v chatu podržte fotku(y) → *Sdílet* →
   **LKHC Odečty**. Aplikace se otevře s fotkami „připravenými“, vyplníte kdo nahlásil a potvrdíte
   *Odeslat ke kontrole*. Funguje jen s nainstalovanou PWA (Android/Chrome); jinde použijte
   *Dávka* (nahrání dávky fotek). WhatsApp maže EXIF – datum z názvu souboru (`IMG-YYYYMMDD-WA…`) je jen **návrh**
   (je to datum odeslání), ve frontě kontroly musíte skutečné datum odečtu potvrdit.
4. Zapisovat (obchůzka, nahrávání, kontrola, správa) mohou jen **admini**; čtenáři vidí přehledy.

## 4. Live elektroměry (dálkový odečet)

Datový model i výpočty počítají s odečty v libovolný čas; integrace se v MVP neimplementuje. Postup, jak
přidat zdroj (např. portál distributora pro hlavní elektroměr), je popsaný v docstringu
[`app/sources/__init__.py`](app/sources/__init__.py) (protokol `ReadingSource`, registrace,
`source_type = live` + `source_config` u elektroměru ve Správě).

## 5. Zálohy a obnova

Databáze běží ve WAL režimu – **nekopírujte `app.db` běžící aplikace jako soubor** (data mohou být ještě
v `app.db-wal`). Použijte zálohovací příkaz (SQLite online backup API):

```sh
# snímek DB + fotky do /data/backups/lkhc-<čas>.tar.gz a kopie ven do ./zalohy
./scripts/backup.sh /var/backups/lkhc-odecty

# cron (denně ve 3:00), KEEP = kolik archivů nechat ve volume
0 3 * * *  cd /opt/lkhc-odecty && KEEP=7 ./scripts/backup.sh /var/backups/lkhc-odecty >> /var/log/lkhc-backup.log 2>&1

# jen databáze (rychlé, např. před ručním zásahem)
docker compose exec app python -m app.backup
```

Adresář se zálohami kopírujte i mimo server (rsync, restic, …).

**Obnova** z archivu `lkhc-<čas>.tar.gz`:

```sh
docker compose stop app
docker compose run --rm -T --no-deps --entrypoint sh app \
    -c 'cd /data && rm -f app.db app.db-wal app.db-shm && tar xz' < lkhc-20261110-030000.tar.gz
docker compose up -d
```

(Archiv obsahuje `app.db` a `photos/`; starší schéma databáze se při startu samo zmigruje.)

## 6. Aktualizace

```sh
cd /opt/lkhc-odecty
./scripts/backup.sh /var/backups/lkhc-odecty     # pro jistotu
git pull                                          # nebo nahrajte novou verzi
docker compose up -d --build
docker compose logs -f app                        # migrace proběhne při startu, se zálohou předem
```

Návrat zpět: předchozí verze kódu + obnova snímku `/data/backups/pre-migrate-…db` (jako `app.db`).

## 7. Známá omezení

- **Odhlášení neruší relaci na serveru**: session je podepsaná cookie; odhlášení ji smaže v prohlížeči,
  ale její dřívější kopie platí dál až do vypršení (nejdéle 14 dní od přihlášení). Deaktivace uživatele
  nebo změna role ve Správě ale platí okamžitě.
- **Sdílený telefon a offline fronta obchůzky**: neodeslané odečty/fotky ve frontě telefonu zůstávají i po
  odhlášení a odešlou se pod tím, kdo se přihlásí příště. Obchůzku dělejte na vlastním telefonu, nebo
  frontu před předáním telefonu odešlete. (Při odhlášení se maže jen uložená stránka a seznam míst.)
- Datum odečtu nejde v UI změnit (viz 1.7).
- Fotky pořízené pár sekund po sobě se ve frontě kontroly jen zobrazují pohromadě; fotka bez čitelného
  výrobního čísla se k elektroměru nepřiřadí podle sousední fotky – vyberete ho z návrhů (★).
- Přesnost rozpoznávání fotek zatím nebyla změřena na skutečném modelu (viz 2.3).

## 8. Vývoj

```sh
uv sync                                                         # Python 3.15 (uv ho stáhne)
uv run python -m app.migrate                                   # nebo: uv run alembic upgrade head
uv run python -m app.importer "private/Odečty elektřiny.xlsx"  # mapování z private/import-mapping.toml
AUTH_DEV_BYPASS=1 uv run uvicorn app.main:app --reload         # http://localhost:8000, fiktivní admin
uv run pytest
uv run ruff check
uv run python scripts/check_private_data.py                    # nic soukromého mimo .gitignore?
```

Bez soukromých dat: `uv run python -m tests.synthetic /tmp/fake` vytvoří syntetickou tabulku s mapováním
(vymyšlení nájemci a výrobní čísla, stejná struktura jako originál) a `uv run python -m app.importer
--private-dir /tmp/fake /tmp/fake/synthetic.xlsx` ji naimportuje.

**Testy** běží na téže syntetické tabulce (generuje ji `tests/synthetic.py`), soukromá data nepotřebují.
Orákulum proti skutečné tabulce (`tests/test_import_oracle.py`, varianta `[real]`) se spustí jen
s dostupným soukromým adresářem (`private/` nebo `LKHC_PRIVATE_DIR`), jinak se viditelně přeskočí.

`AUTH_DEV_BYPASS=1` přihlásí každého jako fiktivního admina – jen lokálně (s `BASE_URL` na localhost).
`EXTRACTOR=fake` + `EXTRACTOR_FAKE_RESPONSES=odpovedi.json` (`{"<sha256 fotky>": {odpověď modelu}}`)
umožní projít celý tok fotek bez modelu.

Soukromá data nepatří do gitu ani do image: `private/` (tabulka, mapování importu, pravda pro vyhodnocení,
poznámky), `examples/` (fotky), `data/`, `.env` (viz `.gitignore` a `.dockerignore`).
`scripts/check_private_data.py` odvodí zakázané výrazy ze soukromých souborů (jména nájemců, výrobní čísla,
e‑maily, adresy serveru) a prohledá vše, co by šlo do gitu.
