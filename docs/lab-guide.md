# Lab guide: een nacht bij Polderfris

Een verhalende rondgang door het FortiGate OT Lab. Elk hoofdstuk is een scène uit
één nacht bij een (fictieve) frisdrankfabriek, met een opdracht in het
**Testcenter** van de controller en een blik in de logs van de FortiGate.

- Doelgroep: engineers, securityteams en klanten die willen zien wat een FortiGate
  in een OT-netwerk doet.
- Duur: ongeveer 90 minuten voor alle hoofdstukken; hoofdstuk 0 t/m 2 zijn ook los
  als demo van 20 minuten te geven.
- Voorbereiding: het lab is volledig uitgerold en in de Lab-tab staat alles op groen
  (FortiGate-kaart groen = REST API antwoordt).

## Het verhaal

Polderfris maakt limonade. Op de fabrieksvloer staan drie processen:

| Proces | PLC (L1) | Field I/O (L0) |
|---|---|---|
| Buffertank met pomp | `plc-tank` 10.10.0.10 (Modbus) | `io-level` 10.0.0.10, niveausensor |
| Menger | `plc-s7-mixer` 10.10.0.20 (Siemens S7) | (geïntegreerd) |
| Transportband | `plc-conveyor` 10.10.0.30 (Modbus) | `io-motor` 10.0.0.11, motordrive |

Jarenlang was het netwerk plat: wie op het kantoornetwerk zat, kon bij elke PLC.
Na een ransomware-incident bij een zusterbedrijf heeft Polderfris een FortiGate
geplaatst en de fabriek volgens het Purdue-model in zones verdeeld. Vannacht wordt
dat ontwerp op de proef gesteld.

**De personages**

| Wie | Rol | Waar in het lab |
|---|---|---|
| Sanne | OT-engineer; de fabriek moet draaien | `scada-hmi`, `ews` (L2) |
| Mark | CISO; wil bewijs dat het veilig is | Testcenter en FortiGate-logs |
| Remco | Systeemintegrator, onderhoud op afstand | `dmz-jump` (L3.5) |
| Ghost | Aanvaller, al binnen op het kantoornetwerk | `probe-test` (testsegment) |

**Hoe een hoofdstuk werkt**

1. **Scène**: lees het verhaal voor.
2. **Voorspel**: laat de groep raden wat er gebeurt. Lukt het, of houdt de FortiGate het tegen?
3. **Actie**: draai de genoemde test(s) in het Testcenter, of een ad-hoc commando.
4. **Onthulling**: bekijk de uitkomst en de logs. De controller toont per test welke
   policy of signature ingreep.
5. **Les**: koppel het aan IEC 62443 en aan de praktijk.

## Zo werk je in dit lab

Als kandidaat zit je, net als de controller, op het gewone netwerk (L4/L5). De enige
weg naar de fabriek loopt via de FortiGate, en daar staat terecht geen regel die
het kantoornetwerk bij de PLC's laat. Je werkt daarom via drie ingangen. Alle
gegevens staan bovenaan het **Testcenter**, onder *Zo kom je in het lab*.

```
                         jouw laptop (L4/L5)
              ┌──────────────┼───────────────────────┐
              │ (1) HTTPS     │ (2) SSH :2222          │ (3) web UI controller
              ▼               ▼                        ▼
         FortiGate-GUI    FortiGate (VIP)          controller ──► Proxmox
          logs, policies      │                                    │ pct exec
                              ▼                                    ▼
                        dmz-jump (L3.5)                    ín elk device
                              │ SSH                     (tests, ad-hoc, kijkvenster)
                              ▼
                           ews (L2) ──► PLC's (L1)
```

| Ingang | Rol | Gaat door de FortiGate? | Wat doe je ermee |
|---|---|---|---|
| **1. FortiGate-GUI** (`https://<WAN-IP>`) | Mark / security-team | ja, als beheerverkeer | logs, policies en assets bekijken: *wat gebeurde er?* |
| **2. Jump host** (`ssh -p 2222 remco@<WAN-IP>`) | Remco / contractor | ja, elke hop | echte remote access: jump host, dan door naar de EWS |
| **3. Controller** (Testcenter en kijkvenster) | iedereen "op de vloer" | nee, via Proxmox buitenom | in de huid kruipen van een device en vanaf dáár verkeer maken |

**Ingang 3 is de belangrijkste.** Kies je in het Testcenter een test "vanaf
`probe-ops`", dan voert de controller het commando uit ín die container in L3,
via Proxmox en niet via het netwerk. Het verkeer dat dat commando maakt, gaat vanaf
daar wél door de FortiGate. Zo speel je de historian-server, de HMI of de laptop
van Ghost zonder dat er een gat in de firewall nodig is.

- **Testcenter**: de voorbereide tests, met per test de verwachting, de uitkomst en
  de FortiGate-logregels die erbij horen.
- **Ad-hoc test**: kies een device en typ een `otprobe`-commando, zoals
  `modbus-read 10.10.0.10 0 4`.
- **Kijkvenster**: knoppen voor de HMI, historian, MES en de DMZ-servers. De
  controller haalt de pagina op in het device zelf en ververst hem elke 5 seconden.
  Het venster is alleen-lezen.

Het contractor-account (`remco`) bestaat alleen op de jump host en de EWS. Het
wachtwoord wordt één keer gegenereerd en staat in het Testcenter.

---

## Hoofdstuk 0: Rondleiding over de vloer

*22:00. Sanne leidt je rond. "Alles wat je vannacht ziet, draait echt: de tank vult,
de pomp slaat aan, de band loopt. Als hier iets stilvalt, staan er morgen dertig
mensen niks te doen."*

**Actie**
- Open de Lab-tab en loop de Purdue-lagen van boven naar beneden langs: van jouw
  IT-netwerk via de FortiGate naar L3.5 tot en met L0.
- Open in het Testcenter het kijkvenster **SCADA HMI**: live tankniveau, setpoint
  en de transportband. Laat het open staan; je ziet de tank vullen en leeglopen.
- Open ook de **historian**: die verzamelt dezelfde data, maar dan een laag hoger.
- Lees de tank-PLC direct uit (ad-hoc, device `ews`):
  - `modbus-read 10.10.0.10 0 4`: HR0 niveau, HR1 setpoint, HR2 pomp aan/uit.

**Les**
- In OT gaat beschikbaarheid boven vertrouwelijkheid: security mag het proces niet hinderen.
- Elke zone heeft een eigen interface op de FortiGate (port2 t/m port7). Het
  segmentatiepunt is dus ook het inspectiepunt.

## Hoofdstuk 1: Wat hangt er eigenlijk aan het netwerk?

*22:15. Mark komt binnen met een vraag die niemand kan beantwoorden: "Hoeveel PLC's
hebben we, en wie praat ermee?" Er is geen assetlijst.*

**Actie**
- Testcenter: **Historian leest OPC UA bij de gateway** (`historian-opcua`).
- In de FortiGate-GUI:
  - de asset-weergave (device identification staat aan op alle OT-poorten);
  - de Application Control-logs, waar OPC UA, Modbus en S7 als applicatie
    zichtbaar zijn en niet als "TCP 4840" of "TCP 502".

**Onthulling**
De FortiGate herkent de OT-protocollen aan de inhoud van het verkeer. Het
OT-monitor-profiel blokkeert niets, maar logt alles.

**Les**
- Zichtbaarheid komt eerst: je kunt niet beschermen wat je niet ziet.
- Begin in de praktijk met een monitor-profiel, en blokkeer pas als je weet wat normaal is.
- IEC 62443: FR6, *timely response to events*. Logging is de basis.

## Hoofdstuk 2: Ghost kijkt rond

*23:05. Een medewerker klikt op een nep-factuur. Ghost heeft nu een laptop op het
kantoornetwerk en doet wat elke aanvaller als eerste doet: rondkijken.*

**Voorspel**: ziet Ghost de PLC's?

**Actie** (Testcenter)
- **Netwerkscan vanaf IT richting L1** (`recon-scan`): scan van 10.10.0.0/24 op 102, 502 en 4840.
- **IT/testcenter leest direct een PLC uit** (`it-to-plc`).

**Onthulling**
Beide pogingen worden geblokkeerd. In de forward traffic-log staat elke poging met
bron-IP, doel en poort, afgehandeld door de implicit deny.

**Les**
- *Zones en conduits*: tussen het IT-netwerk en L1 bestaat geen conduit, dus er komt
  niets doorheen.
- Gelogde denies zijn goud waard: een scan vanaf IT is een vroeg signaal van een aanval.
- IEC 62443: FR5, *restricted data flow*.

## Hoofdstuk 3: Remco moet erbij

*23:40. De transportband hapert. Remco, de integrator, moet op afstand meekijken.
Vroeger had hij een TeamViewer-sessie rechtstreeks op de EWS.*

**Actie**: dit hoofdstuk doe je echt, vanaf je eigen laptop (ingang 2).
1. Log in als Remco: `ssh -p 2222 remco@<WAN-IP>`. Je komt uit op `dmz-jump` (10.35.0.10).
2. Probeer vanaf de jump host direct een PLC: `otprobe modbus-read 10.10.0.10 0 4`.
3. Ga door naar de EWS: `ssh remco@10.20.0.30`.
4. Lees vanaf de EWS de S7-menger uit: `otprobe s7-read 10.10.0.20`.
5. Ter controle in het Testcenter: **Remote access via DMZ jump host**
   (`remote-access`) en **DMZ-host probeert L1 direct te bereiken** (`dmz-bypass`).

**Onthulling**
- Stap 1 gaat door de VIP (WAN :2222 naar 22), stap 3 door de conduit "DMZ jump → L2 EWS".
- Stap 2 wordt geblokkeerd: de DMZ heeft geen conduit naar L1.
- Stap 4 lukt, omdat de EWS het engineering-conduit heeft.
- In de forward traffic-log van de FortiGate staat elke hop als aparte sessie,
  zodat je precies ziet welke route Remco nam.

**Les**
- De DMZ (L3.5) *termineert* verbindingen: niets loopt in één keer van buiten naar binnen.
- Remote access loopt via één punt dat je kunt bewaken, en daarna via vaste conduits.
- Verdieping: zet er MFA en sessie-opname op de jump host bij. Die zitten niet in dit lab.

## Hoofdstuk 4: Zelfde poort, andere bedoeling

*00:30. Ghost heeft de historian-server overgenomen. De historian mag Modbus
praten met de PLC's: hij leest immers de procesdata. "Dan ben ik er toch al?",
denkt Ghost.*

**Voorspel**: een firewall die alleen naar poorten kijkt, laat dit door. Wat doet de FortiGate?

**Actie** (Testcenter, in deze volgorde)
1. **L3 leest Modbus-registers** (`l3-modbus-read`): toegestaan.
2. **L3 schrijft een Modbus-setpoint** (`l3-modbus-write`): Ghost zet het
   tank-setpoint op 999%.
3. **OPC UA gateway probeert te schrijven naar PLC** (`opcua-gw-write`).
4. **HMI zet een setpoint** (`hmi-write`): Sanne doet hetzelfde, maar dan legitiem.

**Onthulling**
Alle vier gaan over TCP 502. De FortiGate kijkt in het Modbus-verkeer zelf, naar
de *function code*:
- lezen (read holding registers) mag via het OT-readonly-profiel;
- schrijven (write register) wordt geblokkeerd, zichtbaar in de Application Control-log;
- de HMI mag wel schrijven, en ook die write staat in de log als audit trail.

**Les**
- Poortfiltering is niet genoeg: dezelfde poort draagt zowel "kijken" als "ingrijpen".
- Deep packet inspection op OT-protocollen maakt *least privilege* per functie mogelijk.
- IEC 62443: FR2 *use control* en FR3 *system integrity*.

## Hoofdstuk 5: De rode knop

*01:15. Ghost wil schade doen die opvalt: de menger stilzetten met een
S7 "CPU STOP". Eén pakket, en de productie ligt plat.*

**Actie** (Testcenter)
- **S7 CPU STOP vanaf L3** (`s7-stop-unauth`).
- **Engineering workstation leest S7 CPU-status** (`s7-read-ews`).

**Onthulling**
Vanaf L3 bestaat er geen S7-conduit, dus de STOP komt er niet door. De EWS heeft
een engineering-conduit naar L1 en kan de CPU-status wel lezen.

**Les**
- Engineering-acties (programmeren, starten en stoppen) horen bij één plek: de EWS.
- Het wie-mag-wat-model uit hoofdstuk 4 geldt ook voor het S7-protocol.

## Hoofdstuk 6: Lateral movement

*02:00. Ghost probeert het via een andere route. Een besmette servicelaptop hangt
in het PLC-segment, en een onbekende host duikt op in L2.*

**Actie** (Testcenter)
- **Lateral movement: PLC-segment naar historian** (`l1-to-l3`).
- **Willekeurige L2-host naar field I/O** (`l2-to-l0`).

**Onthulling**
Beide worden geblokkeerd. L1 begint geen verbindingen naar boven, en alleen de
PLC's en de EWS mogen bij L0.

**Les**
- Verkeer volgt het Purdue-model: bekende richtingen en bekende partners, de rest is deny.
- Segmentatie beperkt de *blast radius*: één besmet device is niet meteen de hele fabriek.

## Afsluiting: het rapport voor Mark

*06:00. De ochtendploeg komt binnen. De tank staat op niveau, de band loopt. Mark
wil voor het managementoverleg één overzicht.*

**Actie**
- Draai in het Testcenter **alle tests**. Elke test is groen als de uitkomst
  overeenkomt met de verwachting (toegestaan of geblokkeerd).
- Loop de resultaten langs met de logs als bewijs.

**Terugblik**

| Hoofdstuk | Wat aangetoond | IEC 62443 |
|---|---|---|
| 1 | OT-protocollen zichtbaar, assets herkend | FR6 |
| 2 | Geen pad van IT naar OT, verkenning gelogd | FR5 |
| 3 | Remote access via één gecontroleerd punt | FR1, FR5 |
| 4 | Lezen ja, schrijven nee op dezelfde poort | FR2, FR3 |
| 5 | Engineering-acties alleen vanaf de EWS | FR2 |
| 6 | Geen lateral movement tussen lagen | FR5 |

---

## Uitbreidingen (nog niet in het lab)

Deze scènes passen in het verhaal, maar vragen nog bouwwerk:

- **"Breek het"**: zet de write-blokkade tijdelijk uit en laat zien hoe Ghost de
  tank laat overlopen (het procesmodel en de HMI tonen het effect). Een sterke
  voor/na-demo.
- **Virtual patching**: een kwetsbare dienst op een PLC aanvallen en een
  IPS-signature laten ingrijpen. De IPS-sensor met OT-signatures staat er al; een
  exploit-simulatie die erop triggert nog niet.
- **Asset inventory als test**: het Testcenter laten controleren dat de FortiGate
  alle devices herkent.
- **Uitbraak naar internet**: alleen de patch-server in de DMZ mag naar buiten;
  laat een PLC proberen naar huis te bellen (C2).
