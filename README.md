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

Vervang `kay-arne` (ook in `install.sh` bij `REPO_URL`). Optionele variabelen, bijvoorbeeld:

```bash
CT_IP=192.168.1.240/24 CT_GW=192.168.1.1 CT_STORAGE=local-zfs bash -c "$(curl -fsSL https://raw.githubusercontent.com/kay-arne/fortigate-ot-lab/main/install.sh)"
```

De installer:
1. maakt de controller-LXC (VMID 9099) op `vmbr0`;
2. installeert de controller (FastAPI) als systemd service;
3. koppelt de controller aan de host met een SSH-sleutel die alleen vanaf het controller-IP geldig is;
4. print de URL en het login-wachtwoord van de web UI.

## Gebruik

1. **Setup**: upload `FGT_VM64_KVM-v7.6.x...fortios.qcow2`, kies de licentie (`.lic`-bestand óf
   FortiFlex-token), stel het WAN-IP van port1 en een admin-wachtwoord in.
2. **Lab → Deploy lab**. De controller:
   - maakt de Purdue-bridges (`vmbr900`–`vmbr999`);
   - maakt de FortiGate-VM met een cloud-init config drive (interfaces, DNS, logging, licentie);
   - bouwt één LXC-template met alle OT-simulators (Modbus, S7comm, OPC UA, HMI, historian) en kloont daaruit de devices en probes;
   - koppelt via SSH een REST API-user en rolt de OT-policies uit (adressen, conduits, application control, IPS, VIP).
3. **Testcenter**: draai de validatietests of eigen ad-hoc tests. Per test zie je PASS/FAIL en de
   FortiGate-logregels (policy, applicatie, signature) die erbij horen.

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

- **Config drive**: de indeling (`openstack/latest/user_data`, licentie in `openstack/content/0000`,
  `LICENSE-TOKEN:` voor FortiFlex) volgt de Fortinet KVM cloud-init-documentatie. Als de licentie niet
  via de config drive wordt opgepakt, activeert de controller een FortiFlex-token alsnog via
  `execute vm-license`.
- **OT-signatures**: application control-ID's worden opgezocht in de FortiGuard-database van de
  FortiGate (`/api/v2/cmdb/application/name`). Zonder actieve OT-licentie en een FortiGuard-update vindt
  de controller er geen; zet dan na de update *FortiGate policies opnieuw toepassen* aan.
- `config ips global / set exclude-signatures none` neemt OT-signatures op in IPS; controleer of je
  FortiOS-build dit zo verwacht.
- Het LXC-template haalt eenmalig packages op via `vmbr0` (DHCP). Geen DHCP? Zet `template_net` in
  `lab/topology.yaml`.
- Linked clones werken op LVM-thin/ZFS; op `dir`-storage valt de controller terug op full clones.
- De web UI gebruikt HTTP met basic auth; zet hem niet buiten je labnetwerk open.

## Afbreken

**Lab → Afbreken…** verwijdert VM's en containers, en optioneel het template en de bridges.
De controller-LXC verwijder je zelf met `pct destroy 9099`.
