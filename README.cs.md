# buzerkostka-claude-indicator

Udělá z [Pajeníčkové **BUZERKOSTKY**](https://pajenicko.cz/zaciname-buzerkostka)
— ESP32 s jednou WS2812 RGB LED, ovládanou přes MQTT — fyzickou stavovou
kontrolku pro [Claude Code](https://claude.com/claude-code).

Místo koukání do terminálu se podíváš na kostku:

| Stav | Světlo | Význam |
| --- | --- | --- |
| `idle` | zelená, pomalu dýchá | čekám na tvůj prompt |
| `working` | plná červená | Claude něco dělá (včetně kompakce kontextu) |
| `permission` | oranžová, bliká 2 Hz | **čeká se na tvoji odpověď** — oprávnění, dotaz, idle upozornění |
| *(žádná session)* | tma | nic neběží |

Tři barvy, a to schválně: je to stejný jazyk jako
[indikátor v notchi pro macOS](../macos-claude-indicator), takže kostka
a obrazovka vždycky říkají totéž. Všechno se dá překonfigurovat — barvy,
efekty, časování i to, který Claude Code event odpovídá kterému stavu — a
bohatší paleta (zelený blik po dokončení, magenta při chybě) je otázka
pár řádků JSONu, viz [Přizpůsobení](#přizpůsobení).

> 🇬🇧 **[English version →](README.md)**

---

## Co potřebuješ

- BUZERKOSTKU v síti, dosažitelnou přes MQTT (nebo přes HTTP v LAN)
- Claude Code
- `python3` ≥ 3.8 — **a nic dalšího**

Projekt nemá žádné závislosti. MQTT klient je napsaný nad standardní
knihovnou, takže žádné `pip install`, žádné virtualenv, žádný lockfile,
co za rok shnije. Běží i na `python3`, který je v macOS předinstalovaný.

## Instalace

```bash
git clone https://github.com/fejtcz/buzerkostka-claude-indicator
cd buzerkostka-claude-indicator
./install.sh
```

`install.sh` nalinkuje `buzerkostka` do `~/.local/bin`, provede tě
konfigurací, nabídne test hardwaru a zaregistruje hooky do Claude Code.
Otevři novou session a kostka začne žít.

<details>
<summary>Nebo jako plugin Claude Code</summary>

```
/plugin marketplace add fejtcz/buzerkostka-claude-indicator
/plugin install buzerkostka-indicator@mafy-hardware
```

Plugin nese hooky s sebou, takže `buzerkostka install` přeskočíš — pořád
ale potřebuješ konfigurák s údaji k brokeru:

```bash
~/.claude/plugins/*/buzerkostka-indicator/bin/buzerkostka setup
```
</details>

<details>
<summary>Nebo přes pipx / pip</summary>

```bash
pipx install git+https://github.com/fejtcz/buzerkostka-claude-indicator
buzerkostka setup
buzerkostka install
```
</details>

## Konfigurace

`buzerkostka setup` zapíše `~/.config/buzerkostka/config.json`. Jediné
dvě věci, které se nedají uhodnout, jsou tvůj broker a topic kostky:

```json
{
  "transport": "mqtt",
  "mqtt": {
    "host": "192.168.1.10",
    "port": 1883,
    "username": "claude",
    "password": "…"
  },
  "device": {
    "topic": "devices/kostka/A0B1C2D3E4F5",
    "channel_order": "rgb"
  }
}
```

Neznáš topic? Buď ho najdeš ve webovém portálu kostky, nebo:

```bash
buzerkostka discover      # a pak kostku vypni a zapni
```

Kostka se při každém připojení ohlásí na `devices/register/kostka` a
`discover` ti topic rovnou uloží.

### Další způsoby, jak kostku ovládat

| `transport` | Kdy použít |
| --- | --- |
| `mqtt` | Vlastní broker (Mosquitto, Home Assistant). Nejnižší latence, doporučeno. |
| `mqtt` → `buzer.cz` | Veřejný broker z výchozího firmwaru. Funguje hned, ale snímky animace jdou přes internet. |
| `http` | Úplně bez brokeru — `GET /set-color` přímo na IP kostky. Chce pevnou IP a snížit `render.fps` na ~8. |
| `console` | Bez hardwaru. Vykresluje ANSI barvy v terminálu — dobré na ladění palet. |

### Jde to i úplně bez konfiguráku

```bash
export BUZERKOSTKA_MQTT_HOST=192.168.1.10
export BUZERKOSTKA_TOPIC=devices/kostka/A0B1C2D3E4F5
```

Tohle jsou nastavení, která mají override přes prostředí; všechno ostatní
žije v konfiguráku, jehož kompletně vyplněnou kopii najdeš v
[`config.example.json`](config.example.json).

| Proměnná | Klíč v configu |
| --- | --- |
| `BUZERKOSTKA_TRANSPORT` | `transport` |
| `BUZERKOSTKA_MQTT_HOST` | `mqtt.host` |
| `BUZERKOSTKA_MQTT_PORT` | `mqtt.port` |
| `BUZERKOSTKA_MQTT_USERNAME` | `mqtt.username` |
| `BUZERKOSTKA_MQTT_PASSWORD` | `mqtt.password` |
| `BUZERKOSTKA_MQTT_TLS` | `mqtt.tls` |
| `BUZERKOSTKA_HTTP_HOST` | `http.host` |
| `BUZERKOSTKA_HTTP_PORT` | `http.port` |
| `BUZERKOSTKA_TOPIC` | `device.topic` |
| `BUZERKOSTKA_DEVICE_PASSWORD` | `device.password` |
| `BUZERKOSTKA_CHANNEL_ORDER` | `device.channel_order` |
| `BUZERKOSTKA_BRIGHTNESS` | `render.brightness` |
| `BUZERKOSTKA_FPS` | `render.fps` |
| `BUZERKOSTKA_ENABLED` | `enabled` |

Další čtyři proměnné stojí mimo samotný konfigurák:
`BUZERKOSTKA_CONFIG` (kde konfigurák leží), `BUZERKOSTKA_STATE_DIR` (kde
leží socket, lock a log), `BUZERKOSTKA_DISABLE` (ignorovat všechny hook
eventy) a `BUZERKOSTKA_DEBUG` (hook klient mluví na stderr).

## Ověření

```bash
buzerkostka doctor    # config, spojení, démon, hooky — všechno najednou
buzerkostka test      # červená / zelená / modrá / bílá / zhasnuto
buzerkostka demo      # přehraje všechny stavy, ať je vidíš
buzerkostka status    # co kontrolka právě ukazuje a proč
```

**Pokud `test` ukazuje špatné barvy**, pere se s tebou pořadí kanálů ve
firmwaru. Spusť `buzerkostka calibrate` — zeptá se tě, co doopravdy vidíš,
a mapování si dopočítá. Celý příběh je v
[`docs/color-order.md`](docs/color-order.md).

## Jak to funguje

Firmware kostky neumí žádné efekty. Rozumí přesně jedné věci: *„buď
teď tahle RGB barva."* Cokoliv, co dýchá nebo bliká, se musí vykreslovat
snímek po snímku na počítači — což hook Claude Code, což je proces žijící
milisekundy, nedokáže.

Proto je práce rozdělená:

```
  hook v Claude Code  ──►  bin/buzerkostka-event  ──►  unix socket
    (async, ~25 ms)         (přečte stdin, skončí)          │
                                                            ▼
                                                buzerkostka daemon
                                       ┌──────────────────────────────┐
                                       │ stavový automat per session  │
                                       │ agregace podle priority      │
                                       │ renderer animací, 20 fps     │
                                       │ jedno trvalé MQTT spojení    │
                                       └──────────────┬───────────────┘
                                                      │  {"r":255,"g":0,"b":0}
                                                      ▼
                                                 BUZERKOSTKA
```

Podstatná rozhodnutí:

- **Démon se spouští sám.** První hook ho nastartuje, s cooldownem, aby
  rozbitý config nezpůsobil smršť procesů. `buzerkostka daemon` nikdy
  nemusíš psát ručně.
- **Hooky jsou registrované jako `async`.** Nemůžou ovlivnit rozhodnutí
  o oprávnění ani zpomalit tah — integrace je tím prokazatelně jen
  pozorovatel. Navíc vždycky končí s kódem `0`: odpojená kostka ti nikdy
  nesmí rozbít session.
- **Víc sessions, jedna kostka.** Každý tab má vlastní záznam a vyhrává
  ten s nejvyšší prioritou — tab čekající na potvrzení přebije tři tiše
  pracující. `buzerkostka status` ukáže celý obrázek.
- **Statická barva nestojí nic.** Renderovací smyčka spí, dokud se snímek
  opravdu nezmění: plná červená se publikuje jednou, ne 20× za sekundu.
  Vytížení CPU v klidu je 0 %.
- **Nic se nezasekne.** Každý stav má timeout, takže Claude Code zabitý
  `SIGKILL`em — který nikdy nepošle `SessionEnd` — spadne zpátky na idle
  místo aby nechal kostku napořád červenou.
- **„Ne“ se pozná taky.** Odmítnutí oprávnění nebo Esc uprostřed tahu
  nevyvolá v Claude Code vůbec žádný hook — ani `PostToolUse`, ani `Stop`,
  ani `PermissionDenied`. Daemon proto čte transcript session a hlídá v něm
  značku o přerušení, takže kostka spadne na idle do jedné sekundy. Vypíná
  se přes `"daemon": {"watch_transcript": false}`.

## Které eventy se sledují

`buzerkostka install --tier` určuje, kolik detailu chceš:

| Tier | Hooků | Co navíc |
| --- | --- | --- |
| `minimal` | 6 | `SessionStart`, `UserPromptSubmit`, `PostToolUse`, `Notification:permission_prompt`, `Stop`, `SessionEnd` |
| `standard` *(výchozí)* | 14 | přidá `PermissionRequest`, `PermissionDenied`, `PostToolUseFailure`, `StopFailure`, idle upozornění, elicitation dialogy a kompakci |
| `verbose` | 18 | přidá `PreToolUse`, start/stop subagentů, teammate idle |

`PostToolUse` je záměrně v každém tieru: je to první event poté, co
schválíš oprávnění, takže bez něj by kostka blikala oranžově až do konce
tahu.

`PermissionDenied` **není** hook pro „uživatel dal Ne“ — Claude Code ho
posílá jen když požadavek odmítne klasifikátor auto módu. Když odmítne
člověk, nepřijde nic, a přesně proto existuje sledování transcriptu.

Scope funguje jako u každého jiného nastavení Claude Code:

```bash
buzerkostka install --scope user      # ~/.claude/settings.json (výchozí)
buzerkostka install --scope project   # .claude/settings.json, do gitu
buzerkostka install --scope local     # .claude/settings.local.json, gitignored
buzerkostka install --print-only      # jen vypíše JSON
```

Instalace je idempotentní — nahrazuje jen vlastní hooky, cizích se
nedotkne — a vždycky nechá zálohu `settings.json` s časovým razítkem.

## Přizpůsobení

Uprav `~/.config/buzerkostka/config.json` a dej `buzerkostka reload`.

**Klidnější světlo do sdílené kanceláře:**

```json
{ "render": { "brightness": 0.35 } }
```

**Ať „working" pulzuje, aby šlo poznat od zaseknuté session:**

```json
{ "states": { "working": {
    "effect": "breathe", "period": 2.5, "min": 0.35, "max": 1.0
} } }
```

**Zajímají tě jen dotazy na potvrzení?** Zbytek namapuj na `off`:

```json
{ "events": {
    "session-start": "off",
    "prompt-submit": "off",
    "stop": "off",
    "permission": "permission"
} }
```

**Chceš víc barev?** Přidej stavy a nasměruj na ně eventy. Tohle dá jeden
zelený blik po dokončení tahu a čtyři magentové bliky při selhání, vždy s
návratem k tomu, co session dělala pod tím:

```json
{ "states": {
    "done":  { "color": "#00FF40", "effect": "flash", "count": 1, "period": 0.6, "duty": 0.7, "priority": 50 },
    "error": { "color": "#FF00FF", "effect": "flash", "count": 4, "period": 0.25, "duty": 0.5, "priority": 90 }
  },
  "events": {
    "stop":       { "state": "done",  "base": "idle" },
    "stop-error": { "state": "error", "base": "idle" },
    "tool-error": { "state": "error" }
} }
```

**Dostupné efekty:** `solid`, `off`, `breathe`, `blink`, `flash`
(přechodný — přehraje se `count`krát a pak odkryje stav pod sebou),
`rainbow`.

**Priority** rozhodují, která session vyhraje, když se neshodnou. Vyšší je
naléhavější; ve výchozím nastavení `off 0 < idle 10 < working 30 < permission 80`,
mezi tím je místo pro tvoje vlastní stavy.

## Příkazy

```
buzerkostka setup            interaktivní první konfigurace
buzerkostka install          zaregistruje hooky do Claude Code
buzerkostka uninstall        zase je odebere
buzerkostka doctor           diagnostika celé sestavy
buzerkostka status [--json]  co kontrolka dělá a proč
buzerkostka test             pošle červenou/zelenou/modrou/bílou/zhasnuto
buzerkostka calibrate        zjistí pořadí kanálů kostky
buzerkostka demo [stavy…]    přehraje všechny stavy
buzerkostka discover         najde topic kostky na brokeru
buzerkostka color '#RRGGBB'  okamžitě zobrazí jednu barvu
buzerkostka state working    vynutí stav, na testování
buzerkostka event stop       ručně pošle jeden hook event
buzerkostka pause | resume   ztlumí kontrolku bez zastavení démona
buzerkostka off              zahodí všechny sessions a zhasne
buzerkostka reload           znovu načte config.json v běžícím démonovi
buzerkostka stop | restart   životní cyklus démona
buzerkostka logs [-f]        log démona
buzerkostka config --path    kde leží konfigurák
```

## Když něco nefunguje

**Vůbec nic se neděje.** Spusť `buzerkostka doctor`. Zkontroluje config,
broker, démona i to, jestli jsou hooky zaregistrované.

**Špatné barvy.** `buzerkostka calibrate`, případně
[`docs/color-order.md`](docs/color-order.md).

**Kontrolka se zasekla.** `buzerkostka status` ukáže každou session i to,
jak dlouho je v aktuálním stavu. `buzerkostka off` je všechny zahodí.

**Hooky se nespouští.** Načítají se při startu session — otevři novou,
nebo si je zkontroluj přes `/hooks`. `BUZERKOSTKA_DEBUG=1` donutí hook
klienta mluvit na stderr, `buzerkostka logs -f` sleduje démona.

**Tvoje verze Claude Code odmítá `"async": true`.** Přeinstaluj přes
`buzerkostka install --no-async`. Hooky pak běží synchronně, každý stojí
zhruba 25 ms.

**`doctor` hlásí, že state adresář není zapisovatelný.** Na některých
strojích skončí `~/.local/state` ve vlastnictví roota — takhle ho vytvoří
`sudo gem install` a pár dalších nástrojů. Démon spadne zpátky na
soukromý adresář v `$TMPDIR` a funguje dál, takže je to varování, ne
chyba. Když to chceš dát do pořádku:

```bash
sudo chown -R "$(id -u)":"$(id -g)" ~/.local/state
```

**Dočasně to vypnout.** `buzerkostka pause`, nebo nastav
`BUZERKOSTKA_DISABLE=1` v prostředí, kde běží Claude Code.

**Ovládání kostky ze vzdáleného stroje.** Nasměruj `mqtt.host` na broker,
na který dosáhnou oba stroje; kostce je jedno, odkud snímky přicházejí.
Jednu kostku může sdílet víc počítačů, jen o sobě navzájem nevědí.

## Vývoj

```bash
pip install pytest
pytest                       # 129 testů, ~15 s, bez hardwaru
BUZERKOSTKA_CONFIG=/tmp/c.json buzerkostka --config /tmp/c.json demo
```

Testy pokrývají MQTT formát proti falešnému brokeru, renderer efektů,
agregaci sessions, slučování `settings.json` a kompletní end-to-end běh se
skutečným procesem démona a skutečnými hooky. Pro vývoj bez kostky nastav
`"transport": "console"` a kreslí se do terminálu.

## Poděkování

Hardware a firmware: [Pajeníčko](https://pajenicko.cz) —
[BuzerKostka](https://github.com/Pajenicko/BuzerKostka).
Tenhle projekt je nezávislá integrace, není spojený s Pajeníčkem ani
s Anthropicem.

## Licence

GPL-3.0-or-later, viz [LICENSE](LICENSE).
