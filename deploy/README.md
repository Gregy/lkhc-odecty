# Nasazení na VPS – runbook

Od „čerstvý VPS s IP adresou“ po „aplikace běží na `https://<APP_HOST>` a zálohuje se na Google Drive“.
Kroky jsou v pořadí, v jakém se dělají; u každého je přesný příkaz, co musíte dodat a jak ověřit výsledek.
Zdroj: `https://github.com/Gregy/lkhc-odecty`. Ověřeno na vpsFree.cz (vpsAdminOS, Debian 13, 1,7 GB RAM).

**Hodnoty vaší instalace** (adresa VPS, doména, e-maily) nejsou v repozitáři: patří do
`deploy/local.env` (git je ignoruje; vzor `deploy/local.env.example`) a na VPS do `.env` souborů.
V návodu jsou jako `<VPS>` (IP nebo jméno VPS), `<APP_HOST>` (např. `odecty.example.org`), `<DOMÉNA>`
(Google Workspace), `<ADMIN_EMAIL>`.

Zápis příkazů: `laptop$` = váš počítač v kořeni repozitáře, `root@vps#` = VPS jako root,
`deploy@vps$` = VPS jako uživatel `deploy`.

## Obsah

1. [Jak to je postavené](#1-jak-to-je-postavené)
2. [Co musíte dodat](#2-co-musíte-dodat)
3. [První nasazení krok za krokem](#3-první-nasazení-krok-za-krokem)
4. [Zálohy na Google Drive](#4-zálohy-na-google-drive)
5. [Aktualizace](#5-aktualizace)
6. [Návrat zpět (rollback)](#6-návrat-zpět-rollback)
7. [Obnova ze zálohy](#7-obnova-ze-zálohy)
8. [Druhá aplikace na stejném VPS](#8-druhá-aplikace-na-stejném-vps)
9. [Provoz](#9-provoz)
10. [Co je otestované a co ne](#10-co-je-otestované-a-co-ne)

---

## 1. Jak to je postavené

```
internet ──80/443 tcp+udp (IPv4+IPv6)──► Caddy  (/srv/proxy, jediný kontejner s porty)
                                          │  síť Dockeru "proxy" 10.250.0.0/24: Caddy = .2, aplikace .128–.255
                                          ├──► lkhc-odecty:8000   (/srv/lkhc-odecty, bez portů)
                                          └──► další aplikace…    (/srv/<app>, bez portů)
systemd timer 03:30 ──► /srv/lkhc-odecty/backup.sh ──► rclone (kontejner) ──► Google Drive
apt 01:00 seznamy, 04:15 upgrade všeho, 04:45 restart VPS, jen když ho balík vyžaduje
```

Na VPS:

| Cesta | Co | Práva |
|---|---|---|
| `/srv/proxy/compose.yml`, `caddy/Caddyfile`, `caddy/sites/*.caddy`, `reload.sh` | sdílená reverse proxy | deploy |
| `/srv/proxy/.env` | `ACME_EMAIL`, `LKHC_ODECTY_HOST` (= `<APP_HOST>`) | deploy |
| `/srv/proxy/caddy/global.d/` | přepínače `pre-dns.caddy`, `acme-staging.caddy` (normálně prázdné, jen `*.example`) | deploy |
| `/srv/lkhc-odecty/compose.yml`, `deploy.sh`, `backup.sh`, `restore.sh` | aplikace (kopie z repozitáře) | deploy |
| `/srv/lkhc-odecty/.env` | **tajné**: `SECRET_KEY`, Google client secret, klíč OpenRouter; `BASE_URL`, `ALLOWED_DOMAIN`, `ADMIN_EMAILS` | deploy, **0600** |
| `/srv/lkhc-odecty/backup/backup.env` | nastavení záloh (není tajné) | deploy, 0640 |
| `/srv/lkhc-odecty/backup/rclone/rclone.conf` | **tajné**: refresh token Google Drive (+ hesla crypt) | deploy, adresář 0700, soubor **0600** |
| `/srv/lkhc-odecty/backup/status.json` | výsledek poslední zálohy | deploy |
| `/srv/lkhc-odecty/deploy-history` | historie nasazení | deploy |
| volume `lkhc-odecty-data` | `app.db`, `photos/`, `backups/` (snímky DB) | uid 10001 |
| volume `proxy_caddy-data` | certifikáty a účet Let's Encrypt – **nemazat** | root |

Rozhodnutí (jedna věta proč):

| Volba | Proč |
|---|---|
| Caddy se soubory `sites/*.caddy`, ne labely | Konfigurace je čitelná v souborech, jde validovat (`caddy validate`) a nic se nemění „samo“ při startu kontejneru. |
| Externí síť `proxy`, Caddy na pevné `.2`, dynamické adresy jen `.128–.255` | Aplikace věří `X-Forwarded-*` jen z Caddy; rozsah zajišťuje, že `.2` po restartu nezabere aplikace (na VPS se to bez něj stalo). |
| Aplikace nepublikují žádné porty | Publikované porty Dockeru obcházejí INPUT firewall; co není publikované, zvenku nejde. |
| `"ip": "127.0.0.1"` v `/etc/docker/daemon.json` | Pojistka: port publikovaný omylem bez adresy (`8080:80`) skončí jen na localhostu. |
| nftables jen na INPUT, ne UFW | Pravidla INPUT/UFW na publikované porty Dockeru nepůsobí (DNAT jde přes FORWARD); vlastní tabulka `inet host_fw` chrání služby hostitele a IPv6 a nesahá do pravidel Dockeru. |
| Image bez registru, `docker save \| ssh docker load` (varianta B) | Při 1,7 GB RAM bez swapu je build na VPS vedle běžící aplikace zbytečné riziko OOM; nic dalšího k provozu. |
| Tagy `live` / `previous` + datované | `docker image ls lkhc-odecty` ukáže celý stav, rollback = přetagování. |
| Automatické aktualizace všeho + restart 04:45 při potřebě | Malý server bez obsluhy: aktuální balíčky jsou důležitější než pár sekund výpadku v noci; po restartu se vše samo vrátí (ověřeno). |
| Bez `live-restore` v Dockeru | Upgrade Dockeru restartuje kontejnery stejnou cestou jako restart VPS (otestovaná); `live-restore` by přidal druhou, méně vyzkoušenou cestu kvůli pár sekundám v noci. |
| Zálohy: systemd timer + rclone v kontejneru | Viditelné v `systemctl list-timers`/`--failed` a journalu, `Persistent=` dožene zmeškaný běh, nic neběží trvale, rclone má připnutou verzi. |
| Fotky `rclone copy` (bez mazání), DB datované snímky s retencí | Fotky jen přibývají → přírůstkově; DB je malá → celé snímky 14 denních / 8 týdenních / 12 měsíčních. |
| Drive přes OAuth uživatele, vlastní klient, scope `drive.file` | Bez dlouhodobého klíče, rclone vidí jen soubory, které sám vytvořil (viz 4.1). |
| Bez `rclone crypt` (připraveno, otestováno) | Data jsou ve vlastním Workspace, kde je i původní tabulka; hesla navíc = riziko nepoužitelných záloh (viz 4.2). |

## 2. Co musíte dodat

1. **SSH klíč** (veřejný) pro roota – ve vpsAdminu, nebo `ssh-copy-id root@<VPS>`.
2. **`deploy/local.env`** na svém počítači (`cp deploy/local.env.example deploy/local.env`, vyplnit).
3. **DNS** pro `<APP_HOST>`: záznam `A` → IPv4 VPS a `AAAA` → IPv6 VPS.
4. **Google OAuth klient pro přihlášení** (typ *Web application*, redirect `https://<APP_HOST>/auth/callback`)
   → `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`. Postup: [hlavní README, 1.1](../README.md#11-google-přihlášení-oauth-klient).
   Dokud chybí, aplikace běží a `/login` hlásí „Přihlášení přes Google není nakonfigurováno“.
5. **Data pro první import**: export tabulky a soukromé mapování (viz 3.9).
6. **Google Drive pro zálohy**: povolené Drive API a **vlastní** OAuth klient *Desktop app* ve stejném
   projektu, jedno přihlášení v prohlížeči (4.3).
7. Kdykoli později: **klíč OpenRouter** (3.10), volitelně URL **healthchecks.io** (4.6).

## 3. První nasazení krok za krokem

### 3.1 VPS

Ve vpsAdminu: **Debian 13**, váš SSH klíč (*Uživatel → Veřejné klíče*, nasadí se při reinstalaci) nebo:

```sh
laptop$ ssh-copy-id root@<VPS>
laptop$ ssh root@<VPS> 'cat /etc/debian_version; free -m; ip -br addr'
```

Záchranná cesta pro celý návod: **vzdálená konzole ve vpsAdminu** (funguje i bez SSH).

### 3.2 DNS

Záznamy `A` a `AAAA` pro `<APP_HOST>`. Ověření z veřejných resolverů:

```sh
laptop$ dig +short A <APP_HOST> @1.1.1.1 ; dig +short AAAA <APP_HOST> @8.8.8.8
laptop$ curl -s -H 'accept: application/dns-json' 'https://dns.google/resolve?name=<APP_HOST>&type=AAAA'   # bez dig
```

**AAAA jen tehdy, když IPv6 VPS opravdu funguje** – Let's Encrypt ověřuje i přes IPv6.

### 3.3 Příprava hostitele (`bootstrap-host.sh`)

```sh
laptop$ scp deploy/bootstrap-host.sh root@<VPS>:
laptop$ ssh root@<VPS> bash bootstrap-host.sh
```

Skript vypisuje každý krok (`written:` / `unchanged:`), dá se pouštět opakovaně: balíčky, časová zóna
Europe/Prague, Docker Engine + compose plugin z `download.docker.com`, `daemon.json` (rotace logů 10 MB × 5,
porty bez adresy jen na 127.0.0.1), journal max. 200 MB, síť `proxy`, uživatel `deploy` (skupina docker,
sudo bez hesla, rootovy SSH klíče), adresáře v `/srv`, automatické aktualizace (9.2), firewall nftables
(SSH, 80, 443 tcp+udp, ICMP; zbytek zahodit).

Ověření:

```sh
root@vps# docker run --rm hello-world | sed -n 2p && docker image rm hello-world
root@vps# docker network inspect proxy -f '{{range .IPAM.Config}}{{.Subnet}} {{.IPRange}}{{end}}'   # 10.250.0.0/24 10.250.0.128/25
root@vps# nft list table inet host_fw ; cat /etc/docker/daemon.json
```

> Skupina `docker` (a sudo) = práva roota. `deploy` není „neprivilegovaný“ uživatel, jen se nepracuje jako root.

### 3.4 SSH jen s klíčem

1. **V novém terminálu** (root zůstane přihlášený):
   ```sh
   laptop$ ssh deploy@<VPS> 'id; sudo -n true && echo sudo OK; docker ps'
   ```
2. Teprve potom:
   ```sh
   laptop$ ssh root@<VPS> bash bootstrap-host.sh --harden-ssh
   ```
   Odmítne pokračovat, pokud journal neukazuje úspěšné přihlášení `deploy` klíčem, nebo nefunguje sudo.
   Nastaví `PasswordAuthentication no`, `KbdInteractiveAuthentication no`, `PermitRootLogin prohibit-password`
   (root dál jen klíčem; `ROOT_LOGIN=no bash bootstrap-host.sh --harden-ssh` root přes SSH úplně vypne)
   a sshd jen reloadne.
3. Ověření z dalšího terminálu:
   ```sh
   laptop$ ssh deploy@<VPS> true && ssh root@<VPS> true && echo klíče OK
   laptop$ ssh -o PubkeyAuthentication=no root@<VPS>     # Permission denied (publickey)
   ```
   Zpět: `rm /etc/ssh/sshd_config.d/00-hardening.conf && systemctl reload ssh` (i z konzole vpsAdminu).

### 3.5 Soubory na VPS

```sh
laptop$ tar -C deploy --exclude=./test --exclude=./local.env -cf - . | ssh deploy@<VPS> 'rm -rf ~/deploy && mkdir ~/deploy && tar -xf - -C ~/deploy'
deploy@vps$ R=~/deploy
```

(Varianta A, git na VPS: `git clone https://github.com/Gregy/lkhc-odecty.git /srv/lkhc-odecty/src` a
`R=/srv/lkhc-odecty/src/deploy`; u privátního repozitáře přes read-only *deploy key* – viz 5.)

### 3.6 Reverse proxy

```sh
deploy@vps$ install -m 0644 $R/proxy/compose.yml /srv/proxy/
deploy@vps$ install -m 0644 $R/proxy/env.example /srv/proxy/.env && nano /srv/proxy/.env   # ACME_EMAIL, LKHC_ODECTY_HOST
deploy@vps$ install -m 0755 $R/proxy/reload.sh /srv/proxy/
deploy@vps$ install -m 0644 $R/proxy/caddy/Caddyfile /srv/proxy/caddy/
deploy@vps$ install -m 0644 $R/proxy/caddy/sites/lkhc-odecty.caddy /srv/proxy/caddy/sites/
deploy@vps$ install -m 0644 $R/proxy/caddy/global.d/*.example /srv/proxy/caddy/global.d/
```

Certifikát podle stavu DNS (přepínače jsou soubory v `global.d/`, návod je v každém `*.example`):

- **DNS ještě neukazuje na VPS:** `cp /srv/proxy/caddy/global.d/pre-dns.caddy{.example,}` – Caddy použije
  vlastní CA a Let's Encrypt vůbec nekontaktuje (neúspěšné pokusy se počítají do limitů).
- **DNS platí:** nejdřív staging: `cp /srv/proxy/caddy/global.d/acme-staging.caddy{.example,}`.

```sh
deploy@vps$ cd /srv/proxy && docker compose up -d
deploy@vps$ docker compose logs caddy | grep -E 'certificate obtained|error'    # ... issuer: acme-staging-v02...
```

Ze stagingu na produkci (platný staging certifikát by zůstal v cache, proto smazat a restartovat):

```sh
deploy@vps$ rm /srv/proxy/caddy/global.d/acme-staging.caddy
deploy@vps$ cd /srv/proxy && docker compose exec caddy rm -rf /data/caddy/certificates/acme-staging-v02.api.letsencrypt.org-directory
deploy@vps$ docker compose restart caddy && sleep 15 && docker compose logs --since 1m caddy | grep 'certificate obtained'
laptop$ echo | openssl s_client -connect <APP_HOST>:443 -servername <APP_HOST> 2>/dev/null | openssl x509 -noout -issuer -dates
laptop$ curl -sI http://<APP_HOST>/ | head -3     # 308 → https://<APP_HOST>/
```

Obnovu certifikátu dělá Caddy sám (podle ARI od Let's Encrypt, zhruba ve 2/3 platnosti); v logu
`certificate obtained` / `renewed`. Stačí nemazat volume `proxy_caddy-data`.

### 3.7 Konfigurace aplikace

```sh
deploy@vps$ install -m 0600 $R/lkhc-odecty/env.production /srv/lkhc-odecty/.env
deploy@vps$ install -m 0644 $R/lkhc-odecty/compose.yml /srv/lkhc-odecty/
deploy@vps$ install -m 0755 $R/deploy.sh $R/backup/backup.sh $R/backup/restore.sh /srv/lkhc-odecty/
deploy@vps$ key=$(head -c 48 /dev/urandom | base64 | tr -d '\n/+=') && sed -i "s|^SECRET_KEY=.*|SECRET_KEY=$key|" /srv/lkhc-odecty/.env
deploy@vps$ nano /srv/lkhc-odecty/.env    # BASE_URL=https://<APP_HOST>, ALLOWED_DOMAIN=<DOMÉNA>, ADMIN_EMAILS=<ADMIN_EMAIL>, GOOGLE_*
```

`BASE_URL` se musí shodovat s `LKHC_ODECTY_HOST` v `/srv/proxy/.env`. Ověření: `ls -l /srv/lkhc-odecty/.env`
→ `-rw------- deploy deploy`.

### 3.8 První nasazení (varianta B)

```sh
laptop$ deploy/push-image.sh                      # cíl z DEPLOY_HOST v deploy/local.env
laptop$ deploy/push-image.sh --tag <TAG>          # poslat už sestavený lkhc-odecty:<TAG> (bez buildu)
```

Skript sestaví image (`linux/amd64`), pošle ho `docker save | gzip | ssh docker load`, zkopíruje `compose.yml`,
`deploy.sh`, `backup.sh`, `restore.sh` a spustí na VPS `deploy.sh --image <TAG>`: ten počká na healthcheck
kontejneru a pak ještě na `GET <BASE_URL>/healthz` přes Caddy na VPS. Ověření:

```sh
deploy@vps$ /srv/lkhc-odecty/deploy.sh --status
laptop$ curl -sI https://<APP_HOST>/healthz | grep -iE '^HTTP|strict-transport'   # 200, max-age=31536000
```

### 3.9 Import dat a první přihlášení

Data jdou přes SSH, na VPS nezůstanou (export na stdin):

```sh
IMPORT="python -m app.importer -"        # přesný tvar příkazu (mapování ze soukromého adresáře): hlavní README
laptop$ ssh <DEPLOY_HOST> "cd /srv/lkhc-odecty && docker compose run --rm -T app $IMPORT" < private/<export>.xlsx
```

Pokud import potřebuje soukromý soubor s mapováním, zkopírujte ho do `/srv/lkhc-odecty/private/` (0600) a
připojte: `docker compose run --rm -T -v /srv/lkhc-odecty/private:/private:ro app $IMPORT --private-dir /private`.

Pak `https://<APP_HOST>` → přihlášení účtem z `ADMIN_EMAILS` → admin. Další nastavení po importu:
[hlavní README, 1.6](../README.md#16-první-přihlášení-a-co-nastavit).

### 3.10 OpenRouter (až budete mít klíč)

```sh
deploy@vps$ nano /srv/lkhc-odecty/.env       # EXTRACTOR=openai_compat, EXTRACTOR_API_KEY=sk-or-...
deploy@vps$ /srv/lkhc-odecty/deploy.sh --restart
```

Totéž po doplnění `GOOGLE_CLIENT_ID`/`SECRET`. (`docker compose restart` nestačí – `.env` se načte jen při
novém vytvoření kontejneru, to dělá `--restart`.)

## 4. Zálohy na Google Drive

`backup.sh` každou noc v 03:30:

1. konzistentní snímek SQLite (online backup API aplikace) do volume `/data/backups/nightly/` (3 poslední),
2. snímek → `Drive:lkhc-odecty-zalohy/db/lkhc-RRRRMMDD-HHMMSS.db`,
3. `photos/` → `Drive:lkhc-odecty-zalohy/photos/` přírůstkově (nic se nemaže),
4. retence `db/`: nejnovější snímek z každého ze 14 dní, 8 týdnů a 12 měsíců; cizí soubory nemaže,
5. stav do `/srv/lkhc-odecty/backup/status.json` (+ kopie ve volume `/data/backups/offsite-status.json`).

Selhání: nenulový exit → jednotka `failed` (`systemctl --failed`), `BACKUP FAILED in step …` v journalu,
`"result": "failed"` ve stavu, `backup.sh check` vrátí 1.

### 4.1 Přihlášení rclone ke Drive: doporučení

Sdílený client_id rclone během roku 2026 přestává fungovat; **vlastní OAuth klient je povinný**.

| | (i) OAuth uživatele, vlastní klient, `drive.file` – **doporučeno** | (ii) service account + sdílený disk |
|---|---|---|
| Kde jsou zálohy | Můj disk správce, složka `lkhc-odecty-zalohy` | sdílený disk organizace |
| Co je na VPS | refresh token (`rclone.conf`) | JSON klíč service accountu (neexpiruje) |
| Co uniklý soubor z VPS dovolí | jen soubory, které vytvořil rclone | celý sdílený disk |
| Nastavení navíc | Drive API + OAuth klient *Desktop app* (projekt už existuje kvůli přihlášení) | Drive API, SA, klíč, sdílený disk, členství SA |
| Překážky | – | nové organizace mají klíče SA zakázané politikou `iam.disableServiceAccountKeyCreation`; sdílené disky nejsou ve všech edicích; do Mého disku jen s domain-wide delegation (široké oprávnění) |
| Slabina | vázané na osobu: při odchodu převést data a znovu autorizovat (5 min) | správa a rotace klíče |

Povinný vlastní klient (i) nijak neznevýhodňuje: projekt v Google Cloud už existuje kvůli přihlášení do
aplikace a s cílovou skupinou **Internal** odpadá zveřejňování aplikace i 7denní expirace tokenů (ta platí
jen pro *External* ve stavu *Testing*). Proto (i). Až bude správců víc, přejděte na (ii) – 4.5.

### 4.2 Šifrování (`rclone crypt`): nezapínat

Zálohy obsahují jména nájemců a spotřebu. Leží ve vlastním Workspace spolku v nesdílené složce, kde je
i původní tabulka se stejnými daty nešifrovaně; Google je zpracovatel podle smlouvy Workspace. Crypt by
chránil hlavně před někým, kdo už má přístup k tomu Drive, a přidal by dvě hesla, bez kterých jsou zálohy
nepoužitelné a která musí přežít ztrátu VPS. Zapnout dává smysl u sdíleného disku s více členy:

```sh
deploy@vps$ rc config        # n → gdrive-crypt → crypt → remote gdrive:lkhc-odecty-zalohy-crypt
                             # → filename_encryption standard → directory_name_encryption true
                             # → password g, password2 g → OBĚ HESLA do správce hesel
deploy@vps$ sed -i 's|^BACKUP_REMOTE=.*|BACKUP_REMOTE=gdrive-crypt:|' /srv/lkhc-odecty/backup/backup.env
```

Obnova pak vyžaduje stejnou sekci `[gdrive-crypt]` v `rclone.conf` (obě hesla).

### 4.3 Nastavení (varianta i)

1. **Google Cloud Console**, projekt organizace (ten z přihlášení do aplikace):
   - *APIs & Services → Library → Google Drive API → Enable*.
   - *Google Auth Platform → Audience*: **Internal** (zkontrolujte; je společné pro celý projekt).
   - *Data Access → Add or remove scopes*: `https://www.googleapis.com/auth/drive.file`.
   - *Clients → Create client → Application type **Desktop app***, název „rclone zálohy“ → zapište
     **Client ID** a **Client secret**.
2. **Na VPS** zkratka pro rclone v kontejneru (stejná verze jako zálohy):
   ```sh
   deploy@vps$ rc() { docker run --rm -it --network host --user "$(id -u):$(id -g)" -e XDG_CACHE_HOME=/tmp \
       -e RCLONE_CONFIG=/config/rclone/rclone.conf -v /srv/lkhc-odecty/backup/rclone:/config/rclone \
       rclone/rclone:1.76.0 "$@"; }
   ```
3. **Přihlášení přes SSH tunel** (na laptopu není potřeba rclone):
   ```sh
   laptop$ ssh -L 53682:127.0.0.1:53682 <DEPLOY_HOST>
   deploy@vps$ rc config
   ```
   Odpovědi: `n` (new remote) → name `gdrive` → storage `drive` → `client_id`, `client_secret` z bodu 1 →
   scope **`drive.file`** → `service_account_file` prázdné → advanced `n` →
   *Use web browser to automatically authenticate?* **`y`** → rclone vypíše
   `http://127.0.0.1:53682/auth?state=…` → otevřete na laptopu (jde tunelem), přihlaste se jako
   `<ADMIN_EMAIL>` a povolte → *Configure this as a Shared Drive?* `n` → `y` (keep) → `q`.

   Bez tunelu: odpovědět `n`, rclone vypíše příkaz `rclone authorize "drive" …` – spusťte **přesně ten**
   (obsahuje váš client_id) na počítači s prohlížečem a výsledek vložte do výzvy `config_token>`.
4. Kontrola:
   ```sh
   deploy@vps$ ls -la /srv/lkhc-odecty/backup/rclone/       # rclone.conf: -rw------- deploy
   deploy@vps$ rc mkdir gdrive:lkhc-odecty-zalohy && rc lsd gdrive:
   ```
5. Nastavení a zapnutí timeru (jednotky jsou nainstalované, timer vypnutý, dokud není rclone):
   ```sh
   deploy@vps$ install -m 0640 $R/backup/backup.env.example /srv/lkhc-odecty/backup/backup.env   # pokud tam ještě není
   root@vps#   install -m 0644 ~deploy/deploy/backup/lkhc-odecty-backup.{service,timer} /etc/systemd/system/ && systemctl daemon-reload
   root@vps#   systemctl enable --now lkhc-odecty-backup.timer && systemctl start lkhc-odecty-backup.service
   ```
6. Ověření:
   ```sh
   root@vps#   journalctl -u lkhc-odecty-backup -n 20 --no-pager      # … lkhc-backup: OK: 1 DB snapshots, N photos
   deploy@vps$ /srv/lkhc-odecty/backup.sh check                     # OK: last successful backup 0 h ago
   root@vps#   systemctl list-timers lkhc-odecty-backup.timer
   ```

### 4.4 Ruční záloha a kontrola

```sh
root@vps#   systemctl start lkhc-odecty-backup.service     # nebo jako deploy: /srv/lkhc-odecty/backup.sh run
deploy@vps$ /srv/lkhc-odecty/backup.sh check                # exit 1 = poslední běh selhal / starší než 36 h / nikdy
```

### 4.5 Varianta (ii): service account + sdílený disk

1. Google Cloud: Drive API; *IAM → Service accounts → Create* („lkhc-zalohy“), bez rolí; *Keys → Add key →
   JSON* (politiku `iam.disableServiceAccountKeyCreation` musí správce případně povolit výjimkou).
2. Drive: sdílený disk „LKHC zálohy“, e-mail service accountu jako **Správce obsahu**; ID disku je v URL.
3. VPS: `install -m 0600 klic.json /srv/lkhc-odecty/backup/rclone/sa.json` a v `rclone.conf`:
   ```ini
   [gdrive]
   type = drive
   scope = drive
   service_account_file = /config/rclone/sa.json
   team_drive = <ID sdíleného disku>
   ```
   Pak 4.3/4–6.

### 4.6 Upozornění e-mailem (volitelné)

- **Zabudované:** healthchecks.io (zdarma), check s periodou 1 den a tolerancí 6 h, URL do `HC_PING_URL=`
  v `backup.env`. E-mail přijde při chybě **i když záloha vůbec neproběhne** (VPS stojí).
- Návrh bez třetí strany (nepostaveno): `OnFailure=` v jednotce + `msmtp` přes SMTP relay Workspace.

Druhá vrstva: denní snapshoty celého VPS u vpsFree (14 dní, obnova ve vpsAdminu) – „jako po výpadku
proudu“; čisté snímky DB jsou ve volume v `/data/backups/nightly/`.

## 5. Aktualizace

```sh
laptop$ deploy/push-image.sh
```

`deploy.sh` na VPS: snímek DB do `/data/backups/pre-deploy/` (zapamatuje si revizi schématu) → `live` →
`previous`, nový → `live` → `docker compose up -d --wait` (vstupní skript kontejneru udělá vlastní snímek
a migruje) → `GET <BASE_URL>/healthz` přes Caddy → při neúspěchu **sám vrátí** předchozí image, a pokud nová
verze změnila schéma DB, i snímek DB (zmigrovaná DB zůstane v `/data/backups/failed-deploy-*`). Tagy
`RRRRMMDD-HHMMSS-<commit>`, drží se 5 posledních. Vlastní úpravy compose do
`/srv/lkhc-odecty/compose.override.yml` (soubor `compose.yml` se při nasazení přepisuje).

**Varianta A** (build na VPS z gitu) funguje (`deploy.sh --git [REF]`, otestováno lokálně), ale při 1,7 GB RAM
bez swapu ji nedoporučuji: build běží vedle aplikace a případný OOM by trefil i ji. Pro privátní repozitář
je potřeba read-only deploy key:

```sh
deploy@vps$ ssh-keygen -t ed25519 -N '' -C 'deploy (read-only)' -f ~/.ssh/github_lkhc_odecty   # .pub → GitHub: Settings → Deploy keys (bez zápisu)
deploy@vps$ printf 'Host github-lkhc-odecty\n  HostName github.com\n  User git\n  IdentityFile ~/.ssh/github_lkhc_odecty\n  IdentitiesOnly yes\n' >> ~/.ssh/config
deploy@vps$ git clone github-lkhc-odecty:Gregy/lkhc-odecty.git /srv/lkhc-odecty/src && /srv/lkhc-odecty/deploy.sh --git
```

**Co se aktualizuje samo a co ne:**

| Co | Jak |
|---|---|
| Balíčky Debianu (main, updates, security, backports) i Docker (`docker-ce`, `containerd.io`) | samy každou noc (9.2); upgrade Dockeru restartuje kontejnery, vrátí se samy |
| Základ image aplikace (`python:3.15-slim`) | při každém `push-image.sh` (`docker build --pull`) – nasazujte aspoň jednou měsíčně, i bez změn kódu |
| Caddy (`caddy:2.11.9-alpine`, připnuto) | ručně: změnit tag v `/srv/proxy/compose.yml` (a v repozitáři), `cd /srv/proxy && docker compose up -d`, `reload.sh` není třeba |
| rclone (`rclone/rclone:1.76.0`) | ručně: `RCLONE_IMAGE` v `backup.env` |

Návrh (nepostaveno): týdenní timer, který u proxy udělá `docker compose pull && up -d` s plovoucím tagem
`caddy:2-alpine`. Aplikaci záměrně nikdy nepřestavovat automaticky.

## 6. Návrat zpět (rollback)

- **Automaticky** při neúspěšném nasazení (5). Exit 1, v `deploy-history` řádky `FAILED` a `rolled-back`.
- **Ručně** na předchozí verzi: `deploy@vps$ /srv/lkhc-odecty/deploy.sh --rollback`
  Když novější verze zmigrovala DB, `--rollback` odmítne a vypíše snímky z doby před nasazením:
  ```sh
  deploy@vps$ /srv/lkhc-odecty/deploy.sh --rollback --restore-db /data/backups/pre-deploy/lkhc-<čas>.db
  ```
  Změny od snímku se ztratí (současná DB zůstane v `/data/backups/rollback-*`).
- Libovolná starší verze: `deploy.sh --image <tag>` (tagy: `docker image ls lkhc-odecty`).

## 7. Obnova ze zálohy

Na stejném nebo **novém** VPS (nový: kapitola 3 po 3.8, rclone 4.3/1–4, `backup.env`; s crypt i
`[gdrive-crypt]` se stejnými hesly):

```sh
deploy@vps$ /srv/lkhc-odecty/restore.sh list                  # snímky DB na Drive
deploy@vps$ /srv/lkhc-odecty/restore.sh                       # nejnovější snímek + fotky (zeptá se)
deploy@vps$ /srv/lkhc-odecty/restore.sh --db lkhc-<čas>.db --no-photos
```

Skript: zastaví aplikaci → současné `app.db*` do `/data/backups/pre-restore-<čas>/` → stáhne snímek a
chybějící fotky (nic nemaže; bez fotek na Drive obnoví jen DB) → vlastník uid 10001 → `PRAGMA
integrity_check` → `app.db` → start (starší schéma se zmigruje) → healthcheck → počty odečtů a fotek.
Když selže před výměnou DB, spustí aplikaci znovu na původní DB.

Bez skriptu (jen stažený soubor): [hlavní README, 5](../README.md#5-zálohy-a-obnova).
Obnovu jednou za čas vyzkoušejte (lokálně `deploy/test/e2e-local.sh all`).

## 8. Druhá aplikace na stejném VPS

1. DNS `A`/`AAAA` pro nové jméno.
2. Compose podle šablony:
   ```sh
   root@vps#   install -d -m 0750 -o deploy -g deploy /srv/neco
   deploy@vps$ cp $R/app-template/compose.yml /srv/neco/compose.yml && nano /srv/neco/compose.yml
   ```
   Pravidla: vlastní `name:`, síť `proxy` jako `external`, **žádné `ports:`**, unikátní alias v síti
   `proxy`, `mem_limit`. Tajnosti do `/srv/neco/.env` (0600).
3. `deploy@vps$ cd /srv/neco && docker compose up -d`
4. Web podle `$R/app-template/site.caddy`:
   ```sh
   deploy@vps$ printf 'neco.example.org {\n\timport common\n\treverse_proxy neco:80\n}\n' > /srv/proxy/caddy/sites/neco.caddy
   deploy@vps$ /srv/proxy/reload.sh          # validace + reload bez výpadku
   ```
5. Ověření: `curl -sI https://neco.example.org/`, `docker port <kontejner>` musí být prázdné.

**Paměť (1,7 GB, bez swapu):** Caddy limit 128 MB (běžně ~15 MB), LKHC Odečty 768 MB (běžně ~110–130 MB),
rclone 256 MB jen během zálohy, systém + Docker ~250 MB. Na další aplikace zbývá zhruba 300–400 MB
limitů; pro víc aplikací zvětšete VPS. Překročení limitu zabije jen ten kontejner (Docker ho znovu spustí);
bez limitu by OOM hostitele mohl zasáhnout cokoli.

## 9. Provoz

### 9.1 Přehled

| Co | Příkaz |
|---|---|
| Stav aplikace | `deploy@vps$ /srv/lkhc-odecty/deploy.sh --status` |
| Logy aplikace | `deploy@vps$ cd /srv/lkhc-odecty && docker compose logs -f --tail 100 app` |
| Logy proxy | `deploy@vps$ cd /srv/proxy && docker compose logs --tail 100 caddy` |
| Záloha | `deploy@vps$ /srv/lkhc-odecty/backup.sh check`, `journalctl -u lkhc-odecty-backup` |
| Paměť | `docker stats --no-stream` |
| Disk | `df -h /; docker system df` (úklid: `docker builder prune`, `docker image prune`) |
| Firewall | `root@vps# nft list table inet host_fw` (čítač `dropped`) |
| Snímek DB před ručním zásahem | `cd /srv/lkhc-odecty && docker compose exec app python -m app.backup` |

### 9.2 Automatické aktualizace a restarty

`unattended-upgrades` instaluje každou noc vše z Debianu (main, updates, security, backports) i z repozitáře
Dockeru (`/etc/apt/apt.conf.d/52unattended-upgrades-local`), maže nepoužívané závislosti a když balík
vyžaduje restart (`/var/run/reboot-required`; jádro je hostitele vpsAdminOS, takže jde o libc/systemd
apod.), restartuje VPS v **04:45**. Rozvrh: seznamy 01:00–01:30, upgrade 04:15–04:25 (po záloze 03:30).

```sh
root@vps# systemctl list-timers apt-daily.timer apt-daily-upgrade.timer
root@vps# unattended-upgrade --dry-run --debug 2>&1 | grep -E 'Allowed origins|pkgs that look'
root@vps# less /var/log/unattended-upgrades/unattended-upgrades.log
```

Po restartu se vše vrací samo: Docker je povolený, kontejnery mají `restart: unless-stopped`, migrace při
startu je idempotentní, timer záloh má `Persistent=true` (ověřeno dvěma restarty skutečného VPS).

## 10. Co je otestované a co ne

**Lokálně** (`deploy/test/e2e-local.sh all`; Docker 29; Caddy s vlastní CA na portech 18080/18443; Drive
nahrazen rclone remote typu `local`): přesměrování http→https, HSTS, cookie `Secure; HttpOnly; SameSite=lax`,
OAuth `redirect_uri`, https URL z `X-Forwarded-Proto`, skutečná IP klienta, podvržené `X-Forwarded-For` od
sousedního kontejneru ignorováno; 8 fotek ~3,5–4 MB jedním požadavkem, pomalé nahrávání 150 kB/s, share target
bez service workeru (~59 MB → 303), 110 MB → 413; druhá aplikace podle šablony; `push-image.sh`,
`deploy.sh --git`, vynucené selhání healthchecku s automatickým rollbackem včetně snímku DB, ruční rollback
přes migraci; záloha (retence na 400denní řadě, stavový soubor, viditelné selhání) i s crypt; obnova do
smazaného volume (počty, byte-identické fotky, vlastník). V kontejneru Debian 13 se systemd: bootstrap
dvakrát, firewall zvenku, `--harden-ssh`, unattended-upgrades (povolené zdroje), záloha přes systemd.

**Na skutečném VPS (vpsFree, vpsAdminOS):** bootstrap (Docker na ZFS/overlayfs, cgroup v2, limity paměti),
`--harden-ssh`, firewall zvenku, Let's Encrypt staging → produkce (TLS-ALPN-01 přes IPv4 i IPv6), HTTPS
s platným řetězcem, HTTP/2 i HTTP/3 zvenku přes IPv4, přesměrování, HSTS, cookie, hláška bez Google klienta,
`push-image.sh --tag`, aktualizace + ruční rollback + znovu nasazení, import dat, běh `unattended-upgrade`,
dva restarty VPS (druhý po opravě rozsahu adres sítě `proxy`).

**Neotestováno:** HTTPS přes IPv6 z vlastního počítače (ten nemá IPv6; IPv6 ověřil jen Let's Encrypt při
vydání a `curl -6` z VPS na vlastní adresu), Google přihlášení a `rclone config` proti Google Drive,
GitHub deploy key, automatický restart ve 04:45 (logika `unattended-upgrades`, ne náš kód), skutečné pomalé
mobilní sítě do limitů timeoutů (10/15 min).
