# LKHC Odečty – specifikace (MVP)

> Stav k 11.11.2026: dokument popisuje aplikaci tak, jak je (ne původní zadání). Hlavní změny proti
> původní verzi: obchůzka (Round) už není uložená entita – vše je odvozené z dat odečtů (§3, §5–§7);
> Python 3.15; přísnější párování fotek v sérii (§5); potvrzené opravy zdrojových dat při importu (§3);
> pravdivostní data pro vyhodnocení extrakce (§5); v UI se značkou ≈ vyznačují jen předběžné
> (extrapolované) hodnoty, zbytkový nájemce jen v součtu T1+T2 (§3); hlavní menu a data bez mezer (§6).

Webová aplikace pro správu a prezentaci odečtů elektroměrů na letišti (aeroklub, přihlášení přes Google Workspace).
Nahrazuje Google Sheet `Odečty elektřiny.xlsx` (soukromá data instalace v `private/`; zdroj importu a volitelné
testovací orákulum – testy používají syntetickou tabulku se stejnou strukturou, `tests/synthetic.py`).

UI je **česky**. Kód, identifikátory, komentáře a commit zprávy anglicky.

## 1. Cíle

1. **Maximálně zjednodušit měsíční obchůzku**: v mobilu vyfotit elektroměr → model přečte výrobní číslo a stavy → uživatel jen potvrdí. Alternativně nahrát dávku fotek z galerie (datum z EXIF) nebo zadat ručně.
2. **Přehled aktuální situace**: spotřeba po nájemcích a měřácích, trend, bilance (hlavní elektroměr vs. součet podružných), upozornění na anomálie a chybějící odečty.
3. **Roční vyúčtování pro nájemce**: kWh po nájemcích a tarifech T1/T2 za zvolené období (ekvivalent listů `Final_2024`/`Final_2025`), export XLSX + tisková verze (HTML → PDF přes tisk prohlížeče). Ceny / rozúčtování faktury **nejsou v MVP**, ale model s nimi má počítat (nezabetonovat kWh‑only).
4. **Připravenost na live elektroměry**: hlavní elektroměr má dálkový odečet (portál distributora), pár podružných měřáků je "na šupla" (nejsou zatím evidovány). Integrace se teď neimplementuje, ale datový model a výpočty nepředpokládají, že všechny odečty vznikají při obchůzce ve stejný den.

## 2. Stack a nasazení

- Python 3.15 (nejnovější stabilní, pro kterou mají všechny závislosti wheely), `uv`, FastAPI, SQLAlchemy 2.x + Alembic, **SQLite** (WAL, soubor ve volume), pytest.
- Serverově renderované UI: Jinja2 + HTMX + trocha Alpine.js, Chart.js. Všechny JS/CSS knihovny **vendorované** ve `app/static/vendor/` (žádné CDN za běhu). Jednoduchý vlastní CSS, mobile‑first, světlý/tmavý režim přes `prefers-color-scheme`.
- Stránka obchůzky je malá samostatná vanilla‑JS aplikace (PWA: manifest + service worker), funguje offline s frontou v IndexedDB (na letišti nemusí být signál).
- Jeden Docker image, `docker-compose.yml` (app + volume `data/` pro DB a fotky), běží za reverse proxy na vlastním VPS. Konfigurace přes env (`.env`), viz §8.
- Bez Node build kroku.

## 3. Doménový model

Pojmy:

- **Tenant (nájemce / odběratel)** – nájemci hangárů, kanceláří, pokoje, kiosku a zbytkový nájemce (provozovatel). Jeden tenant má příznak `is_remainder`: jeho spotřeba = hlavní elektroměr − součet všech ostatních alokovaných.
- **MeterPoint (měřicí místo)** – logické místo ("Kancelář v hangáru"). Má název, popis/umístění, pořadí na obrazovce obchůzky, `role` (`main` | `sub` | `control`), volitelně `parent_point_id` (strom měření, pro kontrolní součty), `active`.
- **Meter (fyzický elektroměr)** – výrobní číslo, patří k MeterPointu, `installed_on` / `removed_on`, `digits` (počet míst počitadla pro přetečení, default 5 → modulo 100000; lze přepsat), seznam registrů (`T1`, případně `T2`), `source_type` (`manual` | `live`), `source_config` (JSON, pro budoucí integrace), poznámka (např. "měří špatně").
  Výměna elektroměru = nový Meter na stejném MeterPointu.
- **Reading (odečet)** – `meter_id`, `register` (`T1`/`T2`), `taken_at` (datetime, pro ruční odečty stačí den), `value` (Decimal, kWh), `source` (`manual` | `photo` | `import` | `live`), `photo_id` (volitelně), `created_by`, `reported_by`, `note`, `status` (`confirmed` | `pending`). Unikátní `(meter_id, register, taken_at)`; ruční/importované odečty mají čas 00:00 dne.
- **PointSkip** – měřicí místo označené v daný den jako „nepřístupné / přeskočeno“ s poznámkou (`meter_point_id`, `day`, `note`).
- *Obchůzka není entita.* Dřívější `Round` (datum, kdo, stav) zrušila migrace 0003 – dny odečtů, stav obchůzky i hranice „jako v sheetu“ se odvozují z dat odečtů (archiv zrušených obchůzek je v auditu, akce `migrate_0003`).
- **Photo** – soubor na disku (`data/photos/YYYY/MM/<uuid>.jpg` + náhled), EXIF čas, kdo nahrál, stav extrakce (`queued`/`done`/`failed`/`skipped`), surový výsledek extrakce (JSON), vazba na Meter (po rozpoznání/potvrzení).
- **Allocation (alokace)** – `tenant_id`, `meter_point_id`, `valid_from`, `valid_to`, `factor` (default +1; umožní i podíly či odečítání). Měřicí místa s rolí `control` nebo bez alokace se do nájemců nepočítají.
- **User** – email, jméno, role (`admin` | `reader`), poslední přihlášení.
- **Settlement (vyúčtování)** – rok/název, `period_start`, `period_end` (libovolná data; období uvnitř = zarovnané kalendářní měsíce), stav (`draft`/`final`), při finalizaci uložený snapshot výsledků (JSON), aby se pozdější opravy odečtů nepropsaly do uzavřeného roku. Do budoucna sem přijdou ceny.

### Výpočet spotřeby (jádro, čistý modul bez DB – `app/domain/`)

- Spotřeba registru mezi dvěma odečty = `(curr − prev) mod 10^digits` (přetečení počitadla; v datech reálně nastalo u 5místného počitadla).
  Záporný/nesmyslně velký rozdíl (větší než polovina rozsahu) se nepovažuje za přetečení, ale hlásí jako anomálie.
- **Surová tabulka po dnech odečtů** (ekvivalent listu `Spotřeba`): řádky = dny, kdy byl odečten aspoň jeden elektroměr (u importu přesně řádky sheetu – ověřeno testem); období = dvojice po sobě jdoucích dnů; když měřák v daný den odečten nebyl, jeho spotřeba se celá připíše dni, kdy byl odečten příště (jako v sheetu). Pozdě nahlášený odečet nájemce tvoří vlastní (řídký) řádek – pohled ukazuje, co bylo kdy přečteno, neinterpretuje.
- **Hodnota k datu** `value_at(meter, register, t)`: přesný odečet, jinak lineární interpolace mezi sousedními odečty (označeno `estimated`). Slouží pro vyúčtování k libovolným hranicím a pro live měřáky. Spotřeba MeterPointu za interval se sčítá přes všechny jeho Metery (výměny).
- **Rozpočítání na nájemce** (ekvivalent `Rozpočítání se sazbou`): pro každé období a tarif `tenant = Σ factor × spotřeba(alokovaná místa)`, remainder tenant = `main − Σ ostatní`. Záporný zbytek je validní výsledek (v sheetu se děje, když se podružné měřáky odečtou v jiný den než hlavní), ale UI ho zvýrazní.
- **Zbytkový nájemce jen v součtu T1+T2** (prezentace): jednotarifní podružné elektroměry se počítají celé do T1, takže T1 zbytku vychází příliš nízko (v zimě i záporně) a T2 příliš vysoko – smysl má jen součet. Výpočet po registrech i struktura snapshotu vyúčtování zůstávají; dashboard, nájemci, Spotřeba (zarovnaná období), vyúčtování (stránka, protokol, tisk) i exporty XLSX/CSV ukazují u zbytku jen T1+T2 (buňky T1/T2 „–“ / prázdné s vysvětlením, „Výpočet zbytku“ v součtech). Ostatní nájemci a všechny měřáky zůstávají po tarifech; surový pohled „po dnech odečtů“ (orákulum proti sheetu) také. Upozornění „záporný zbytek“ hodnotí součet T1+T2.
- Bilance kontrolních měřáků: `control` místo vs. součet jeho potomků → zobrazit rozdíl.

### Zarovnaná období a díry v datech (HLAVNÍ režim výpočtu)

Odečty **nepřicházejí ve stejný čas**: k některým měřákům (typicky u některých nájemců) se správce nedostane a stavy mu posílá nájemce jindy, někdy měsíc chybí úplně. Surová tabulka ("připsat příštímu období") pak dává zkreslené výsledky – odtud záporné hodnoty zbytku v sheetu. Ta zůstává jen jako sekundární pohled "surově po dnech odečtů" a jako orákulum proti sheetu. Všechny přehledy, dashboard i vyúčtování používají **zarovnaná období**:

- Hranice období jsou společné pro všechny měřáky: kalendářní měsíce (default, hranice = půlnoc 1. dne), nebo dny odečtů hlavního elektroměru (volitelně; hlavní elektroměr i zbytek jsou pak na hranicích přesné), nebo libovolná data.
- Stav každého registru na hranici = `value_at`:
  - přesný odečet (tolerance: odečet ve stejný den jako hranice se bere jako přesný),
  - jinak **interpolace** mezi nejbližším odečtem před a po → `estimated`,
  - na otevřeném konci (po hranici ještě žádný odečet není) **extrapolace** z průměrného denního odběru posledního známého intervalu (prodlouženého zpět aspoň na 28 dní, aby velmi krátký poslední interval nezkreslil odhad) → `provisional`; elektroměr odebraný bez konečného odečtu se extrapoluje do data odebrání a dál je konstantní → `estimated` (pozdější odečet ho už nezpřesní), období celé po odebrání jsou přesně 0; s příchodem dalšího odečtu se sama přepočítá. Před prvním odečtem měřáku (nový měřák) je spotřeba 0 od `installed_on`, nikoli extrapolace dozadu.
- Strategie interpolace je vyměnitelná (`Interpolator` protokol). MVP: lineární v čase. Připravit druhou strategii "podle profilu" (rozdělit spotřebu mezi dvěma odečty úměrně spotřebě referenčního měřáku ve stejném tarifu – typicky hlavního, který bude live); nemusí být zapnutá, ale rozhraní na ni musí stačit.
- Každá vypočtená hodnota nese kvalitu: `exact` / `estimated` / `provisional` + metadata (z jakých odečtů a dat vznikla, kolik dní se interpolovalo). Agregace na nájemce nese nejhorší kvalitu svých složek a podíl odhadnutých kWh. **Zobrazení** (jen prezentace, data i logika kvality beze změny): hodnoty interpolované mezi dvěma skutečnými odečty (`estimated`) jsou běžný případ a ukazují se stejně jako přesné (bez značky, podbarvení a legendy; původ zůstává jen v tooltipu po najetí myší); vyznačuje se pouze `provisional` – značka **≈**, kurzíva, tooltip/poklepání s původem a legenda „předběžně – extrapolovaný odhad za posledním odečtem“. V exportech je předběžná buňka podbarvená, má komentář s původem a sloupec „Kvalita“ uvádí „předběžně“ (u přesných a interpolovaných je prázdný). Součet nebo agregace je vyznačena, když je předběžná aspoň jedna složka (nejhorší kvalita). Pravidlo je v `app.formatting.quality_mark` / `is_marked`.
- Součet spotřeb přes sousední období musí přesně sedět na rozdíl skutečných odečtů (interpolace jen rozděluje, nic nepřidává ani neztrácí) – testovat jako invariant.
- Pozdě doručený odečet se zadá se **skutečným datem pořízení** (ne datem zadání); přepočet je automatický, protože se nic odvozeného neukládá (kromě snapshotu finalizovaného vyúčtování).
- Vyúčtování nejde finalizovat, dokud některá složka na hranicích období je `provisional` (lze vynutit s vědomým potvrzením a poznámkou); `estimated` je povoleno a v UI se nevyznačuje (protokol u měřáku jen nenápadně uvede původ stavu na hranici, stejně jako u jiných stavů, které nejsou odečtem daného dne).
- Upozornění "chybí odečet" se počítá per měřák (dní od posledního odečtu vs. obvyklý interval), nikoli podle obchůzek.

### Výchozí data z XLSX (list `Odečty`)

Sloupce jsou dvojice T1/T2; řádek 2 = výrobní číslo, řádek 3 = popis (malým písmenem začínající popis se
při importu zvětší), od řádku 5 data (datum jako text `d.m.yyyy`). Které sloupce patří kterému nájemci a
místu je **soukromá konfigurace instalace**: `private/import-mapping.toml` (formát v docstringu
`app/importer/mapping.py`, vzor `app/importer/import-mapping.example.toml`; konkrétní tabulka sloupců této
instalace je v soukromém `private/SPEC-private.md`). Mapování popisuje i zvláštnosti, které tabulka
obsahuje a importér musí zvládnout:

- hlavní elektroměr (`main`, T1+T2, budoucí live) a kontrolní měřák (`control`) s podřízeným místem (`parent`),
- podružné měřáky jen s T1 i s T1+T2, počitadla se 6 místy (šířka se odvodí z dat) i 5místné počitadlo,
  které v datech přeteklo,
- výměnu elektroměru (starý končí posledním odečtem, nový na stejném místě začíná od 0),
- stejné výrobní číslo ve dvou sloupcích (vyřazený měřák a jeho náhrada, nejspíš překlep v sheetu) → druhý
  Meter na stejném místě s číslem `<číslo>-B` a poznámkou k ověření, alokace platí od jeho prvního odečtu,
- nealokovaný podružný měřák (spadá do zbytku), komentáře u popisů (i Excel „threaded comment“),
- měřáky neodečtené v některé dny, měřák bez odečtů po určitém datu (předběžné hodnoty).

Vzorce sheetu (pro kontrolu): nájemce = součet jeho sloupců v `Spotřeba`, zbytkový nájemce = hlavní − vše
ostatní (zvlášť T1 a T2).

List `Stará data` (2020–2023, nepravidelný formát, "XXXX", desetinné čárky) se v MVP **neimportuje**.

**Potvrzené opravy zdrojových dat** (`[[date_corrections]]` v soukromém mapování): XLSX zůstává beze změny;
chyby potvrzené vlastníkem dat (např. překlep v datu řádku) se aplikují při importu (výchozí) a import je
vypíše. `--bez-oprav` importuje tabulku tak, jak je (orákulum). Import odmítne běžet do databáze s daty
zadanými v aplikaci (bez `--force`) a do databáze se starým, neopraveným datem (nejdřív
`python -m app.tools.move_reading_date`).

**Testovací orákulum**: cache hodnoty listů `Spotřeba` a `Rozpočítání se sazbou` v XLSX (openpyxl vrací u Google‑exportu vzorce tvaru `IFERROR(__xludf.DUMMYFUNCTION(...), <hodnota>)` – hodnotu je třeba vyparsovat z posledního argumentu; u `Rozpočítání se sazbou` jsou od řádku 4 přímo hodnoty). Surová tabulka po dnech odečtů a rozpočítání z importovaných dat (tabulka tak, jak je, bez oprav) musí vyjít shodně. Listy `Final_*` jsou ručně upravované kopie – jako orákulum nepoužívat.

## 4. Autentizace a role

- Google OIDC (Authlib), povolena jen doména z `ALLOWED_DOMAIN` (jen v `.env`, bez výchozí hodnoty – prázdná = nikdo se nepřihlásí; v produkci povinná): kontrola claimu `hd` **a** `email_verified` na serveru (ne jen parametr `hd` v URL).
- Session v podepsané cookie (HttpOnly, Secure, SameSite=Lax). CSRF ochrana na mutujících požadavcích.
- Role: `admin` (zápis, správa, vyúčtování) a `reader` (jen čtení). E‑maily v `ADMIN_EMAILS` jsou admin; ostatní z domény se při prvním přihlášení založí jako `reader`. Admin může role měnit v UI.
- `AUTH_DEV_BYPASS=1` (jen pro lokální vývoj/testy, při startu hlasitě varuje a odmítne se zapnout, pokud `APP_ENV=production`): přihlásí fiktivního admina.

## 5. Pořizování odečtů

### Obchůzka (PWA, `/obchuzka`)
- Seznam aktivních měřicích míst v pořadí obchůzky se stavem (hotovo / chybí), poslední odečet a datum. Stav je **odvozený**: místo je hotové, když jeho aktivní elektroměry mají odečet (nebo místo záznam „přeskočeno“) s dnešním datem; žádná relace „obchůzky“ se neukládá.
- U místa: tlačítko foto (`<input type=file accept=image/* capture=environment>`), po vyfocení se fotka zmenší na klientu (max ~1600 px, JPEG) a odešle; pole pro stavy T1/T2 s numerickou klávesnicí, předvyplněná nápověda posledního stavu a očekávaného rozsahu.
- Po návratu extrakce se pole předvyplní, uživatel potvrdí. Validace: stav nesmí klesnout (kromě přetečení), varování při odchylce od obvyklé denní spotřeby (např. > 3× nebo skok řádů) – varování lze vědomě potvrdit.
- Offline: seznam míst cache, fotky a zadané hodnoty ve frontě IndexedDB, synchronizace po připojení; viditelný stav fronty.
- Možnost označit "nepřístupné / přeskočeno" s poznámkou (PointSkip pro místo a den; pozdější odečet téhož dne ho nahradí).
- Fotka z obchůzky se ukládá s elektroměrem místa, u kterého uživatel stiskl foto. Když extrakce na fotce přečte výrobní číslo jiného (známého nebo neznámého) elektroměru, obchůzka zobrazí varování a hodnotu nepředvyplní; ve frontě kontroly je taková fotka nepřiřazená s oběma elektroměry jako návrhy – nepřepíše se ani jedním směrem.

### Dávka fotek (`/odecty/davka`)
- Nahrání více fotek naráz; čas z EXIF (`DateTimeOriginal`), jinak čas nahrání (editovatelný).
- **Fotky z WhatsAppu** (takto posílají stavy někteří nájemci): PWA je registrovaná jako **Web Share Target** (`share_target` v manifestu, POST `multipart/form-data` s obrázky) – ve WhatsAppu "Sdílet → LKHC Odečty" pošle fotky rovnou do fronty kontroly (Android/Chrome s nainstalovanou PWA; jinde zůstává běžný upload). Sdílení funguje i offline přes frontu service workeru. WhatsApp fotky komprimuje a maže EXIF; název souboru `IMG-YYYYMMDD-WAnnnn.jpg` nese datum → použít jen jako **návrh** data (uživatel potvrdí), čas neznámý.
- **Odečty od nájemců** (fotka přeposlaná z messengeru/e‑mailu obvykle EXIF nemá): pokud čas pořízení chybí, kontrola si datum **vyžádá** (žádné tiché "dnes"); lze zadat i odečet bez fotky s libovolným datem a poznámkou "nahlásil nájemce". U odečtu se eviduje `reported_by` (volný text).
- Fronta ke kontrole (`/odecty/kontrola`): fotka vedle rozpoznaných hodnot, přiřazený měřák (podle výr. čísla, fuzzy shoda; ručně změnitelné), potvrzení po jednom i hromadně. Nic se nezapíše jako `confirmed` bez potvrzení člověkem. Hromadně lze potvrdit jen karty, kde je každý elektroměr určen vlastním důkazem (výr. číslo / místo obchůzky / ruční volba) a nic nevyžaduje potvrzení.

### Co ukázaly reálné fotky (`examples/*.zip`, 4 sady, 87 fotek, 3000×4000 JPEG s EXIF; pro jednu sadu má databáze skutečné hodnoty pro vyhodnocení)

Archivy jsou velké a obsahují soukromá data – do gitu ani do Docker image nepatří (`examples/` v `.gitignore`/`.dockerignore`). Slouží jako ruční testovací sada pro extrakci.

- **Mechanické bubínkové elektroměry** (Křižík, ZPA): poslední červený bubínek je desetina; sheet eviduje celé kWh (12345|6 → 12345). Na krytu bývají **ručně psané staré odečty** – ignorovat.
- **LCD moduly na DIN liště** (Maneler aj.): displej bývá **otočený o 90°**, výr. číslo ("No 12345678") slabě na boku; desetinná místa (0012345 = 123,45).
- **Více elektroměrů na jedné fotce** (dva moduly vedle sebe) → extrakce vrací *seznam* elektroměrů na fotce.
- **Dva displeje / registry na jednom přístroji** (Maneler 9907D), **cyklující displej** (Schrack MGDIZ: "A+ T1" / "T2"; hlavní elektroměr distributora s OBIS kódy 1.8.1/1.8.2) → jedna fotka = jeden registr; správce proto fotí **sérii** snímků téhož měřáku během pár sekund. Výsledky ze série se slučují.
- **Nečitelné snímky** (rozmazané, zamlžené sklo hlavního elektroměru, odlesk blesku) → extrakce musí umět říct "nečitelné" a nehádat; v sérii se použije čitelný snímek.
- **Fotka bez čitelného výr. čísla** (detail počitadla) → přiřadí se jen tehdy, když hodnota sedí do očekávaného rozsahu právě jednoho aktivního elektroměru (návrh „podle očekávaného stavu“, hromadně nepotvrditelný); jinak zůstane nepřiřazená s pořadím návrhů (nejdřív elektroměry ze stejné skupiny, do kterých hodnota sedí). Sousední fotka sama o sobě důkaz není: v jedné rozvodnici je několik elektroměrů a správce je fotí pár sekund po sobě (reálně: čtyři elektroměry během 16 s; jindy řetězec 7 fotek a 5 elektroměrů).
- **Neznámé výr. číslo** (na novějších fotkách je nový elektroměr, který v sheetu není) → nabídnout založení nového elektroměru / výměnu na měřicím místě přímo z kontroly.

Důsledky pro model: `Meter` má `decimals_shown` (kolik desetinných míst ukazuje displej; pro správné čtení a kontrolu řádu), `display_note` (volná nápověda pro extrakci, např. "displej cykluje T1/T2, OBIS 1.8.1 = T1"), a přetečení se počítá z celých míst (`digits`). Ukládá se hodnota tak, jak je přečtena (Decimal včetně desetin), sheetová data jsou celá čísla.

### Extrakce z fotek (`app/extraction/`)
- Rozhraní `ReadingExtractor.extract(image_bytes, hints) -> ExtractionResult` (`meters: [{serial | null, serial_confidence, registers: [{register | null, value, confidence, label_seen}]}]`, `readable: bool`, `problems`, `raw`, `model`). `hints` = seznam známých elektroměrů (výr. číslo, typ, registry, počet míst a desetin, `display_note`, poslední stav a očekávaný rozsah) – pomáhá modelu i párování.
- Nápověda modelu uvádí poslední stav a dolní mez rozsahu záměrně o 1 kWh nižší než skutečnost. Hodnota shodná se skutečným posledním odečtem je tedy přečtená z displeje (elektroměr stojí); hodnota shodná s číslem z nápovědy je opsaná a dostane příznak `same_as_hint`.
- Před odesláním modelu: EXIF otočení, zmenšení (delší strana ~1600–2000 px); originál se uchovává.
- **Série**: snímky pořízené do ~60 s po sobě tvoří skupinu jen **pro zobrazení** ve frontě kontroly. Registry dvou snímků se sloučí do jednoho návrhu jen tehdy, když jsou oba nezávisle přiřazené ke stejnému elektroměru (výr. číslo na každém, nebo silná identifikace jednoho a hodnota druhého, která sedí jen do očekávaného rozsahu tohoto elektroměru a žádného jiného). Při rozporu hodnot → ke kontrole, nehlasovat potichu.
- Implementace:
  - `openai_compat` – libovolný OpenAI‑kompatibilní `chat/completions` endpoint s obrázkem (`EXTRACTOR_BASE_URL`, `EXTRACTOR_API_KEY`, `EXTRACTOR_MODEL`). Pokrývá **OpenRouter** i lokální **Ollama / llama.cpp** s malým vision modelem na CPU. Strukturovaný JSON výstup, robustní parsování, timeout, retry. Volitelně (`EXTRACTOR_VERIFY`) **druhé čtení**: fotka s přečtenými číslicemi se pošle modelu ještě jednou (nezávislý požadavek, stejný prompt); hodnota, kterou druhé čtení nezopakuje (nebo když druhý požadavek selže), dostane příznak `low_confidence` – v kontrole je pochybná a vyžaduje potvrzení. Dvojnásobná cena za fotku.
  - `none` – bez extrakce (jen ruční zadání), default když není konfigurace.
  - `fake` – deterministický pro testy.
- Extrakce běží na pozadí (jednoduchá in‑process fronta s perzistentním stavem v tabulce Photo; po restartu se `queued` dojedou). Upload nikdy nečeká na model.
- Párování: přesná shoda výr. čísla → Levenshtein ≤ 2 → hodnota v očekávaném rozsahu právě jednoho aktivního elektroměru (víc kandidátů = nejednoznačné, nepřiřazeno) → jinak "nepřiřazeno" s návrhy.
- **Vyhodnocení modelu** (`python -m app.extraction.eval`): na složce/ZIPu fotek; `--expected` buď pravda po fotkách, nebo pravda po elektroměrech pro jeden den z databáze (`python -m app.extraction.truth <den> --out private/truth-<den>.json`). Výstup: výsledek po fotkách, stav po elektroměrech (správně / špatně / smíšeně / nečitelné / nenalezeno), počet *jistě špatných* hodnot; hodnota, která se shoduje až po oříznutí desetin na celé kWh (sheet ukládá celé kWh), je správná, ale počítá se zvlášť.

### Ruční zadání a opravy
- Mřížka odečtů (`/odecty`): řádky = data, sloupce = měřáky/registry (jako sheet), inline editace pro admina, zobrazení zdroje a odkaz na fotku. Historie změn (audit log: kdo, kdy, stará → nová hodnota).

### Live zdroje (jen rozhraní)
- `ReadingSource` protokol (`fetch(meter, since) -> list[Reading]`) + registr podle `source_type`; plánovač se v MVP neimplementuje. V UI u měřáku zobrazit typ zdroje. Dokumentovat v `docs/` jak přidat integraci.

## 6. Přehledy

- **Dashboard (`/`)**: kdy byl naposledy odečten hlavní elektroměr a nejnovější odečet vůbec (a kolik dní od nich); spotřeba za poslední období a od začátku roku po nájemcích (T1/T2/celkem, zbytek jen celkem); graf měsíční spotřeby po nájemcích (skládaný) s porovnáním s loňskem; bilance hlavní vs. podružné; upozornění (chybějící odečty, anomálie, záporný zbytek, fotky čekající na kontrolu).
- **Měřák / měřicí místo (`/mista/{id}`)**: historie odečtů, graf stavu a spotřeby, fotky, výměny elektroměrů.
- **Nájemce (`/najemci/{id}`)**: spotřeba po obdobích a měřácích, graf.
- **Spotřeba (`/spotreba`)**: zarovnaná období (měsíce nebo mezi odečty hlavního elektroměru) a sekundárně surová tabulka po dnech odečtů (ekvivalent listů `Spotřeba` a `Rozpočítání`), přepínač se sazbou / bez sazby, filtr roku, export XLSX/CSV.
- Grafy podle skillu/dobrých zvyklostí: čitelné v obou režimech, konzistentní barvy nájemců napříč aplikací.
- **Hlavní menu**: Přehled, Odečty, Spotřeba, Obchůzka, Dávka (jen admin), Kontrola, Vyúčtování, Správa (+ uživatel / odhlášení). Místa, Nájemci a Fotky nejsou samostatné záložky: odkazuje na ně stránka Správa (`/sprava`), která je přístupná i čtenářům (vidí jen tyto přehledy; sekce administrace zůstávají jen pro adminy) a na těchto stránkách je zvýrazněná záložka Správa.
- **Data** všude v UI a exportech česky bez mezer: `3.7.2026`, `3.7.` (bez roku), `7.2026` (měsíc), `3.7.2026 14:05`; strojové formáty (CSV/JSON klíče, hodnoty `input type=date`, názvy souborů) zůstávají ISO. Centrálně `app.formatting.format_date` / `format_month` / `format_datetime` (filtry `|date_cs`, `|month_cs`, `|datetime_cs`), v JS `lkhc.fmtDate` / `fmtDateTime` / `fmtMonth`.

## 7. Vyúčtování (`/vyuctovani`)

- Nové vyúčtování: rok → default přesné hranice 1.1.–1.1. v zarovnaných měsíčních obdobích (stavy na hranicích dopočtené, předběžné vyznačeny); volitelně dvě vlastní data – pomocník nabídne dny odečtu hlavního elektroměru nejbližší 1.1. (jako dřív v sheetu: např. 2.1.2025–4.1.2026); období uvnitř jsou vždy zarovnané kalendářní měsíce (první a poslední mohou být neúplné). Finalizované snapshoty starších vyúčtování „podle obchůzek“ se zobrazují beze změny.
- Výstup: tabulka období × nájemce × T1/T2 (zbytek jen T1+T2) + součty (jako `Final_YYYY`), a pro každého nájemce protokol: měřáky, počáteční a konečný stav, spotřeba, případné výměny/poznámky.
- Export XLSX (souhrn + list pro každého nájemce), tisková HTML verze protokolu.
- Finalizace uloží snapshot; finalizované lze znovu otevřít jen adminem (audit).

## 8. Konfigurace (env)

`APP_ENV`, `SECRET_KEY`, `BASE_URL`, `DATA_DIR` (default `./data`), `DATABASE_URL` (volitelně), `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `ALLOWED_DOMAIN`, `ADMIN_EMAILS`, `AUTH_DEV_BYPASS`, `EXTRACTOR` (`none`|`openai_compat`|`fake`), `EXTRACTOR_BASE_URL`, `EXTRACTOR_API_KEY`, `EXTRACTOR_MODEL`, `EXTRACTOR_TIMEOUT`, `EXTRACTOR_MAX_SIDE`, `EXTRACTOR_VERIFY`, `TZ` (default `Europe/Prague`; nastavuje i hodiny procesu). Kontejner navíc `FORWARDED_ALLOW_IPS`, `SKIP_MIGRATIONS`. Vše vysvětluje `.env.example`. Mimo `localhost` aplikace odmítne výchozí `SECRET_KEY` i `AUTH_DEV_BYPASS`, i když se zapomene `APP_ENV=production`.

## 9. Struktura repa a vlastnictví souborů

```
pyproject.toml, uv.lock, alembic.ini, alembic/
app/
  main.py            # app factory, registrace routerů, middleware      [foundation]
  config.py db.py models.py auth.py deps.py audit.py                    [foundation]
  domain/            # čisté výpočty: consumption.py allocation.py      [foundation]
  services/          # dotazy nad DB pro výpočty (periods, tenants…)    [foundation]
  importer/          # import XLSX + CLI `python -m app.importer`; mapování z private/  [foundation]
  tools/             # move_reading_date (přesun odečtů na jiné datum)  [finalize]
  migrate.py backup.py formatting.py                                    [finalize]
  routers/
    dashboard.py spotreba.py mista.py najemci.py vyuctovani.py sprava.py  [reports]
    odecty.py obchuzka.py fotky.py                                        [capture]
  extraction/        # extractor rozhraní + implementace + fronta       [capture]
  sources/           # ReadingSource protokol (live, jen rozhraní)      [foundation]
  templates/
    base.html _macros.html login.html                                   [foundation]
    dashboard/ spotreba/ mista/ najemci/ vyuctovani/ sprava/            [reports]
    odecty/ obchuzka/ fotky/                                            [capture]
  static/
    vendor/ css/app.css js/app.js                                       [foundation]
    js/charts.js css/reports.css                                        [reports]
    obchuzka/ (PWA: app.js, sw.js, manifest, ikony) css/capture.css     [capture]
tests/               # test_domain_*, test_import_*, test_auth_*        [foundation]
                     # test_reports_*                                   [reports]
                     # test_capture_*, test_extraction_*                [capture]
Dockerfile docker-entrypoint.sh docker-compose.yml .dockerignore .env.example scripts/backup.sh README.md docs/  [finalize]
```

Foundation vytvoří **všechny routery jako funkční stuby** (registrované v `main.py`, vrací stránku "připravuje se" v `base.html`) a navigaci se všemi odkazy, aby další fáze nemusely sahat do sdílených souborů. Kdo potřebuje změnu ve sdíleném souboru mimo své vlastnictví, udělá ji minimální a zmíní ji ve své závěrečné zprávě.

## 10. Kvalita

- `uv run pytest` zelené; doménová logika pokrytá testy včetně orákula z XLSX, přetečení, výměny elektroměru, chybějících odečtů, interpolace.
- `uv run ruff check` čisté.
- Aplikace jde spustit lokálně: `AUTH_DEV_BYPASS=1 uv run uvicorn app.main:app` po `uv run python -m app.migrate` a `uv run python -m app.importer "private/Odečty elektřiny.xlsx"` (mapování z `private/import-mapping.toml`). V kontejneru migrace spouští vstupní skript (se zálohou před migrací).
- Žádná tajemství ani data instalace v repu; `data/`, `private/`, `examples/` a `.env` v `.gitignore`. Kontrola: `uv run python scripts/check_private_data.py` (zakázané výrazy odvodí ze soukromých souborů).
