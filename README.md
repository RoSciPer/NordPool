# Nord Pool Shelly — automātiska ūdens/apkures vadība pēc lētajām cenām

**v2** — FastAPI serveris + Shelly grafiki + Telegram bots (bez ierīces IP/paroles botā).

Sistēma, kas Tavu Shelly slēdzi automātiski ieslēdz tikai tad, kad
elektrība Latvijā ir vislētākā. Lietotājs neraksta nevienu rindiņu koda —
viss notiek pa 3 soļiem caur Telegram botu.

```
                ┌──────────────────┐                ┌──────────────────┐
                │  Elering API     │                │   Tavs Shelly    │
                │  (15-min cenas)  │                │   (mājās)        │
                └────────┬─────────┘                └─────────┬────────┘
                         │   ↓ pēc 15:30                       ↑ 17:00-21:00
                         │   pieprasa cenas                    │ paņem grafiku
                         ▼                                     │
                ┌─────────────────────────────────────────────┴───┐
                │  FastAPI serveris (lvprices.bidsolana.bid)      │
                │   • /api/v1/prices                              │
                │   • /api/v1/schedule?token=…                    │
                │   • /api/v1/heartbeat                           │
                └─────────────────────────────────────────────────┘
                         ▲
                         │ pārvalda žetonus un profilus
                ┌────────┴─────────┐
                │  Telegram bots   │ ← lietotājs šeit konfigurē
                └──────────────────┘
```

## Kā tas strādā cilvēka valodā

1. **Mēs serverī katru dienu ap 15:30** ieelpojam nākamo dienu cenas no Eleringa
   API (Nord Pool publicē rītdienas cenas ap pulksten 13-14, mēs paņemam ar
   drošības rezervi un mēģinām vēlreiz katru stundu, ja vēl nav).
2. **Tavs Shelly** ar mūsu nelielo JS skriptu pats vēršas pie servera **vienreiz
   diennaktī starp 17:00 un 21:00**. Minūti, kad konkrēti vērsties, izvēlas
   deterministisks hash no Tava unikālā žetona — tā 1000 ierīču nesistas
   serverī vienlaicīgi.
3. Serveris **uz vietas aprēķina Tavam profilam piemērotāko grafiku** — atrod
   lētākos 15-min logus tieši Tavām vajadzībām (piem., "boilerim jābūt karstam
   uz 7:00, sild vismaz 3 h"), un atgriež kompaktu sarakstu ar ieslēgšanas/
   izslēgšanas notikumiem nākamajām 24-48 h.
4. **Shelly katru minūti** salīdzina savu pulksteni ar grafiku un lokāli
   ieslēdz/izslēdz releju. Internets vairs nav vajadzīgs — viss saglabāts
   ierīces atmiņā.
5. Ja serveris jeb internets uz Tavu mājas tīklu pazūd, Shelly izmanto **drošības
   stundas** (noklusēti 02:00–05:00) — tas ir pieņemams kompromiss naktī.

## Kas šajā repozitorijā kur?

| Fails | Loma |
|---|---|
| `server.py` | FastAPI serveris, kas servē cenas un grafikus Shelly ierīcēm |
| `telegram_bot.py` | TG bots — visa lietotāja saskarne |
| `shelly_script_template.js` | JS kods, ko bots personalizē un atsūta lietotājam |
| `core_prices.py` | Cenu ievākšana no Eleringa + SQLite kešs |
| `core_storage.py` | Ierīces un sildīšanas profili (žetoni) |
| `core_planner.py` | Algoritms — kā no profila + cenām ražot grafiku |
| `nordpool_shelly_gui.py` | Vecais Tkinter GUI — paliek kā desktop manuālā vadība |
| `prices.db` | SQLite — cenas + ierīces + profili (vienots fails, viegli backup-ot) |
| `nordpool_server.service` | systemd unit FastAPI serverim |
| `nordpool_bot.service` | systemd unit Telegram botam |

---

## Instalācija no nulles uz tīra Linux servera

### 1) Repo + Python env

```bash
sudo mkdir -p /opt/projects && cd /opt/projects
sudo git clone <šis-repo> NordPool
cd NordPool

sudo python3 -m venv venv
sudo venv/bin/pip install -r requirements.txt
```

### 2) Konfigurācija

```bash
sudo cp .env.example .env
sudo nano .env   # ieliec TELEGRAM_BOT_TOKEN un SERVER_BASE_URL
```

### 3) FastAPI serveris + Telegram bots kā servisi

```bash
sudo cp nordpool_server.service /etc/systemd/system/
sudo cp nordpool_bot.service    /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nordpool_server.service nordpool_bot.service
```

Pārbaude:

```bash
curl http://127.0.0.1:8080/api/v1/health
```

### 4) HTTPS apakšdomēns (lvprices.bidsolana.bid)

Shelly Plus Gen2+ atbalsta HTTPS, bet pieprasa **derīgu publisku
sertifikātu**. Vienkāršākā pieeja — Caddy:

```bash
sudo apt install -y caddy
```

`/etc/caddy/Caddyfile`:

```
lvprices.bidsolana.bid {
    reverse_proxy 127.0.0.1:8080
    encode gzip
}
```

```bash
sudo systemctl reload caddy
```

Caddy automātiski paņems Let's Encrypt sertifikātu (vajag DNS A ierakstu
`lvprices.bidsolana.bid → tava-servera-IP`). Tas ir vienreizējs darbs.

---

## Kā lietotājs pievieno savu Shelly (3 minūtes)

1. **Telegram botā** spied `/setup`.
2. Bots prasa: ierīces nosaukums → ierīces tips (Boileris / Apkure / Cits).
3. Bots atsūta **gatavu `.js` failu** un soli-pa-solim instrukciju.
4. Lietotājs Shelly tīmekļa saskarnē (`http://<shelly-ip>`):
   - Settings → Scripts → **+Add script** → Edit
   - Ielīmē augšupielādētā `.js` faila saturu
   - Save → Start → (rekomendē Run on startup)

Viss. Pirmajā minūtē Shelly žurnālā parādīsies `[NP]` rindas un ielādēs
pirmo grafiku. Pēc tam tas ies fonā, lietotājam vairs nav jādara nekas.

### Komandas Telegram botā

| Komanda | Ko dara |
|---|---|
| `/setup` | Pievienot jaunu Shelly (sūta personalizētu skriptu) |
| `/connect` | Atkārtoti nosūta JS skripta failu (ja pazaudēts) |
| `/devices` | Saraksts ar pieslēgtajām ierīcēm un to tiešsaisti |
| `/schedule` | Plānotie ON intervāli šodien un rīt |
| `/profile` | Apskata ierīces profilu (cikos jābūt sasilstam, cik h) |
| `/vacation` | Atvaļinājuma režīms — viss izslēgts |
| `/prices` | Stundu cenas šodien |
| `/price_table` | 15-min cenu tabula |
| `/status` | Kopējais kopsavilkums |
| `/delete_device` | Noņem ierīci no servera |

---

## Algoritms: kā tiek izvēlēti lētākie 15-min logi

Vienam "sildīšanas logam" (piem. _"līdz 07:00, vismaz 3 h, meklē no 00:00"_):

1. Paņem visus 15-min slotus laika intervālā `[search_from, ready_by)`
   uz mērķa datumu.
2. Sakārto pēc cenas augošā secībā.
3. Atlasa pirmos `ceil(min_hours × 4)` slotus.
4. Tie kļūst par ON. Pārējie 24h slotos = OFF.

Vairāku logu apvienojums (piem. rīta + vakara boilers) = ON slotu savienība.

### Svētkos

Pievienota `holidays` pakete (Latvijas oficiālie). Pēc noklusējuma svētkus
uzskatām par **brīvdienu** (boileris siltāks pēc 9:00, nevis 7:00). Tā kā
algoritms tāpat skatās uz **cenu profilu**, vasarā svētkos parasti dienas
vidū iznāks pati lētākā cena — un sistēma to atradīs automātiski.

---

## Drošība

- **Žetons** ir 32 zīmju URL-safe virkne (~190 bitu entropija). Tas iet
  HTTPS GET parametros tikai Shelly→serveris virzienā. Nav personīgu datu.
- **Bots neglabā un nevar piekļūt** ierīces lokālajai vadībai — tas ir
  būtisks uzlabojums salīdzinājumā ar veco versiju, kur botā bija
  Shelly IP/parole.
- Pazaudē žetonu? `/delete_device` izveido jaunu un atsūta jaunu skriptu.

---

## Roadmap (2. fāze)

- 💰 Ietaupījumu uzskaite (TG paziņojumi: "šodien ietaupīji 0.61 €")
- ☀️ Saules paneļu īpašnieku režīms (sild kad pārpalikums)
- ✏️ Inline profila rediģēšana (tagad tikai aplūkošana — vienkārši profili)
- 📊 Mēneša statistika ar grafikiem
- 🔔 Brīdinājumi (sevišķi dārgs/lēts logs rītdien)
- 🏠 Vairāku ierīču grupas ("visa māja", "pirts" u.tml.)

---

## Tehniskās piezīmes

- SQLite WAL — vienlaicīga lasīšana no servera + bota = bez bloķēšanas.
- Cenu API atbild EUR/MWh — mēs visur lietojam EUR/kWh (dalām ar 1000).
- Eiropas/Rīgas laika josla visur — `zoneinfo` lieto sistēmas tzdata.
- FastAPI atspoguļo `client.host` proxy headers caur uvicorn `--proxy-headers`.
