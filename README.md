# FortiGate OT Lab op Proxmox

Een volledig virtueel OT-lab volgens het Purdue-model, met een FortiGate-VM (FortiOS 7.6 + OT-licentie)
als **enige** verbinding tussen je fysieke netwerk en de OT-segmenten. Een controller-container met
web UI rolt alles uit en werkt daarna als testcenter.

```
  Fysiek netwerk (L4/L5, vmbr0)
            │ port1
     ┌──────┴──────┐
     │  FortiGate  │  zones & conduits, OT application control, OT IPS
     └┬──┬──┬──┬──┬┘
 port2│  │3 │4 │5 │6      port7
 L3.5 DMZ  L3 Ops  L2 Supervisory  L1 Control  L0 Process   Test Center
 vmbr935   vmbr930 vmbr920         vmbr910     vmbr900      vmbr999
 jump,     histo-  SCADA-HMI,      Modbus PLC  level sensor  probe
 patch,    rian,   OPC UA gw,      S7 PLC,     motor drive  (rogue/IT)
 mirror    MES     EWS             conveyor PLC
```

De OT-bridges hebben geen fysieke poort en de Proxmox host heeft er geen IP op. De controller beheert
devices **out-of-band** via de hypervisor (`pct exec`), dus niet via het netwerk: de FortiGate blijft
de enige netwerkweg naar binnen.

## Installatie

Op de Proxmox host (als root):

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/kay-arne/fortigate-ot-lab/main/install.sh)"
```

Net als bij de Proxmox community scripts kies je eerst **Standaard** of **Geavanceerd**. In de
geavanceerde modus vraagt de installer (whiptail-menu's met wat er op je host beschikbaar is):

| Vraag | Default |
|---|---|
| Naamprefix voor alle VM's en containers | `otlab` (→ `otlab-controller`, `otlab-fgt`, `otlab-plc-tank`, …) |
| Container ID, hostname, cores, RAM, disk | eerstvolgend vrij ID, `<prefix>-controller`, 2, 2048 MiB, 16 GB |
| Storage voor de controller en voor het LXC-template | eerste actieve storage met `rootdir` / `vztmpl` |
| Bridge en VLAN-tag van de controller | `vmbr0`, geen VLAN |
| DHCP of statisch IP (+ gateway, DNS) | DHCP |
| Poort van de web UI | 8080 |
| Storage voor de FortiGate en de OT-containers | zelfde als de controller |
| Bridge en VLAN van FortiGate port1 (WAN) | zelfde als de controller |
| Netwerk tijdens de template-build (DHCP of tijdelijk IP) | DHCP |
| Eerste VMID van het lab (reeks van 100 ID's) | controller-ID + 1, of de eerste vrije reeks daarna |
| IP waarop de controller de host via SSH bereikt | IP op de interface met de default route |
| Aparte gebruiker voor de host-toegang (ook in Standaard-modus) | ja, `otlab` |

Je krijgt een samenvatting en kunt nog aanpassen voordat er iets wordt aangemaakt.

Onbeheerd installeren kan ook, met dezelfde keuzes als variabelen:

```bash
OTLAB_UNATTENDED=1 CT_BRIDGE=vmbr1 CT_VLAN=40 CT_IP=10.40.0.240/24 CT_GW=10.40.0.1 CT_STORAGE=local-zfs \
  bash -c "$(curl -fsSL https://raw.githubusercontent.com/kay-arne/fortigate-ot-lab/main/install.sh)"
```

Zie de kop van [install.sh](install.sh) voor alle variabelen.

De installer:
1. maakt de controller-LXC op de gekozen storage, bridge en VLAN;
2. installeert de controller (FastAPI) als systemd service;
3. koppelt de controller aan de host (zie hieronder);
4. print de URL en het login-wachtwoord van de web UI.

### Hoe de controller met Proxmox praat

De controller logt via **SSH** in op de host en voert daar `qm`, `pct` en `pvesh` uit; bestanden
(FortiGate-image, config-ISO) gaan via SFTP. De OT-containers worden met `pct exec` bediend, dus
zonder netwerkpad naar de OT-segmenten. Nodig: de controller moet de host op TCP 22 bereiken.

Standaard (na bevestiging in de wizard) maakt de installer hiervoor een aparte gebruiker aan:

- systeemgebruiker `otlab` (home `/var/lib/otlab`, geen wachtwoord) met alleen de sleutel van de controller;
- de sleutel is aan het controller-IP gebonden (bij statisch IP) en staat geen forwarding toe;
- `/etc/sudoers.d/otlab` staat alleen `qm`, `pct`, `pvesh` en het plaatsen van de config-ISO toe;
- `/var/lib/vz/otlab` is de werkmap waar de gebruiker images en ISO's neerzet.

Toegang intrekken (lab-VM's en containers blijven staan):

```bash
otlab-revoke
```

Let op: `qm` en `pct` zijn in de praktijk root-equivalent (bijv. bind-mounts van host-paden). De
aparte gebruiker maakt de toegang **zichtbaar, controleerbaar en eenvoudig in te trekken**, maar is
geen harde sandbox. Kies je in de wizard voor root, dan komt de sleutel in
`/root/.ssh/authorized_keys` (de symlink naar `/etc/pve/priv/` blijft intact) en ruimt
`otlab-revoke` die ook weer op.

## Gebruik

1. **Setup**: upload `FGT_VM64_KVM-v7.6.x...fortios.qcow2`, kies de licentie (`.lic`-bestand óf
   FortiFlex-token), stel het WAN-IP van port1 en een admin-wachtwoord in.
2. **Lab → Deploy lab**. De controller:
   - maakt de Purdue-bridges (`vmbr900`–`vmbr999`);
   - maakt de FortiGate-VM (`<prefix>-fgt`) volgens de Fortinet Proxmox-handleiding, met een cloud-init config drive (interfaces, DNS, logging, licentie en het tijdelijke admin-wachtwoord `Deployment_Pass2026`, dat bij de provisioning via de REST API wordt vervangen door jouw wachtwoord);
   - bouwt één LXC-template met alle OT-simulators (Modbus, S7comm, OPC UA, HMI, historian) en kloont daaruit de devices en probes;
   - koppelt via SSH een REST API-user en rolt de OT-policies uit (adressen, conduits, application control, IPS, VIP).
3. **Losse componenten**: vink onder *Lab* de FortiGate, de policies, het netwerk, het template of
   individuele containers (of een heel segment) aan en kies *Uitrollen* (ontbrekende uitrollen),
   *Opnieuw uitrollen* (eerst verwijderen) of *Verwijderen*. Handig om alleen de FortiGate te testen of één
   mislukte container opnieuw te doen zonder het hele lab af te breken.
4. **Testcenter**: draai de validatietests of eigen ad-hoc tests. Per test zie je PASS/FAIL en de
   FortiGate-logregels (policy, applicatie, signature) die erbij horen.
5. **Toegang voor de kandidaat**: bovenaan het Testcenter staan de drie ingangen: de FortiGate-GUI,
   SSH als contractor (`remco`) via de VIP naar de jump host en door naar de EWS, en een kijkvenster dat de
   webinterface van HMI, historian, MES en de DMZ-servers toont (opgehaald via Proxmox, zonder gat in de firewall).
   Bestaand lab? Klik op *Simulators bijwerken* om het account en `otprobe` op de devices te zetten.
6. **Demo of training**: volg de [lab guide](docs/lab-guide.md), een verhalende rondgang
   ("een nacht bij Polderfris") langs alle tests, met per hoofdstuk de les en de koppeling naar IEC 62443.

## Wat zit erin

| Bestand | Inhoud |
|---|---|
| `install.sh` | oneliner-installer voor de Proxmox host |
| `lab/topology.yaml` | segmenten, IP-plan, devices, VMID's: pas hier het lab aan |
| `lab/scenarios.yaml` | validatietests (segmentatie, OT deep inspection, zichtbaarheid) |
| `controller/` | web UI, Proxmox-orchestratie, FortiGate-provisioning, testcenter |
| `otsim/` | OT-simulators + `otprobe` testtool (draait in elke LXC) |

### Conduits (FortiGate-policies)

| Van → naar | Wat | Profiel |
|---|---|---|
| WAN → DMZ jump (VIP :2222) | SSH remote access | IPS |
| DMZ jump → EWS / MES | SSH, HTTP | IPS |
| DMZ patch → internet | updates (NAT) | IPS |
| L3 historian → DMZ mirror | data-replicatie (push) | IPS |
| L3 historian → L2 OPC UA gw | OPC UA | OT-monitor |
| L3 → L1 Modbus PLC's | Modbus lezen | **OT-readonly** (write blokkeren) |
| L2 HMI → L1 | Modbus + S7 | OT-monitor |
| L2 OPC UA gw → L1 | Modbus lezen | **OT-readonly** |
| L2 EWS → L1 / L0 | engineering | OT-monitor |
| L1 PLC → L0 I/O | Modbus | OT-monitor |
| Test center → DMZ jump | SSH | IPS |

Al het andere valt op de implicit deny, die gelogd wordt.

## Om op te letten (nog niet op echte hardware getest)

Deze eerste versie is lokaal gecontroleerd (syntax, topologie, UI), maar **nog niet op een Proxmox host
met een echte FortiGate-VM gedraaid**. Controleer bij de eerste uitrol vooral:

- **FortiGate-VM**: aangemaakt volgens de Fortinet *Proxmox Administration Guide*
  ([deploying a FortiGate-VM](https://docs.fortinet.com/document/fortigate-private-cloud/8.0.0/proxmox-administration-guide/37920/deploying-a-fortigate-vm-into-proxmox),
  [cloud-init + FortiFlex](https://docs.fortinet.com/document/fortigate-private-cloud/8.0.0/proxmox-administration-guide/832344/deploying-a-fortigate-vm-into-proxmox-using-cloud-init-and-a-fortiflex-token)):
  OS Linux 6.x/2.6, `fortios.qcow2` via `qm disk import` gekoppeld als **scsi0**, logdisk **scsi1** (32 GB),
  NIC's zonder Proxmox-firewall, config drive als **ide2** en boot order `ide2 → scsi0 → scsi1`.
- **Config drive**: één MIME-multipart `openstack/latest/user_data` met `config.txt` (bootstrap) en
  `license.txt` (`LICENSE-TOKEN:<token>` voor FortiFlex), zoals in de handleiding. Een `.lic`-bestand gaat op
  dezelfde manier als bijlage mee; dat staat niet letterlijk in de handleiding, dus controleer bij problemen
  `diagnose debug cloudinit show` op de FortiGate. Lukt FortiFlex via cloud-init niet, dan activeert de
  controller het token alsnog met `execute vm-license`.
- **Zonder licentie** werken maar 3 interfaces (port1-3); het lab gebruikt er 7, dus een licentie is nodig.
- **OT-signatures**: application control-ID's worden opgezocht in de FortiGuard-database van de
  FortiGate (`/api/v2/cmdb/application/name`). Zonder actieve OT-licentie en een FortiGuard-update vindt
  de controller er geen; zet dan na de update *FortiGate policies opnieuw toepassen* aan.
- `config ips global / set exclude-signatures none` neemt OT-signatures op in IPS; controleer of je
  FortiOS-build dit zo verwacht.
- Het LXC-template haalt eenmalig packages op via `vmbr0` (DHCP). Geen DHCP? Zet `template_net` in
  `lab/topology.yaml`.
- Linked clones werken op LVM-thin/ZFS; op `dir`-storage valt de controller terug op full clones.
- De web UI gebruikt HTTP met een loginformulier (gebruiker `admin`, sessie 12 uur; wachtwoord in `/opt/otlab/data/ui_password`). Het verkeer is niet versleuteld: zet hem niet buiten je labnetwerk open.

## Bijwerken

De controller is een git-checkout in `/opt/otlab`; instellingen, uploads, de API-key en testresultaten
staan in `data/` (niet in git) en blijven bij een update behouden. Het draaiende lab wordt niet aangeraakt.

- **Web UI → Updates**: *Controleren op updates* toont de nieuwe commits, *Nu bijwerken* haalt ze op, werkt
  de Python-dependencies bij en herstart de controller. De pagina laadt daarna vanzelf opnieuw. Er verschijnt
  een blauwe stip bij *Updates* als er een nieuwe versie is.
- **Vanaf de Proxmox host**: draai de installer-oneliner opnieuw. Die herkent een bestaande controller en
  biedt *Bijwerken* aan (onbeheerd: `OTLAB_UPDATE=1`). Gebruik dit ook voor controllers van vóór deze
  update-functie: die hebben nog geen `update.sh`.
- **In de controller**: `/opt/otlab/update.sh`.

Wijzigt een update de OT-simulators, dan toont de *Lab*-tab een melding met de knop
*Simulators bijwerken*; die zet de nieuwe code in alle draaiende containers.

Heb je bestanden in `/opt/otlab` zelf aangepast (bijv. `lab/topology.yaml`), dan weigert de update en
toont welke bestanden het zijn, zodat er niets ongemerkt overschreven wordt.

## Afbreken

**Lab → Afbreken…** verwijdert VM's en containers, en optioneel het template en de bridges.
De controller-LXC verwijder je zelf met `pct destroy <id>`; draai daarna `otlab-revoke` om de host-toegang in te trekken.
