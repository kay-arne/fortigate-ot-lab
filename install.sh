#!/usr/bin/env bash
# FortiGate OT Lab - installer voor de Proxmox VE host.
#
# Oneliner:
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/kay-arne/fortigate-ot-lab/main/install.sh)"
#
# De installer vraagt interactief (whiptail) waar de controller moet draaien:
# storage, bridge, VLAN, DHCP/statisch IP, resources en de uplink van de
# FortiGate. Kies "Standaard" voor de defaults zonder verdere vragen.
#
# Onbeheerd installeren: zet OTLAB_UNATTENDED=1 en eventueel de variabelen
# hieronder als env vóór de oneliner.
#   CTID CT_HOSTNAME CT_CORES CT_MEMORY CT_DISK CT_STORAGE TPL_STORAGE
#   CT_BRIDGE CT_VLAN CT_IP CT_GW CT_DNS UI_PORT
#   LAB_STORAGE LAB_BRIDGE LAB_VLAN LAB_VMID_BASE TEMPLATE_NET HOST_IP
#   HOST_ACCESS (user|root, default user)  HOST_USER (default otlab)
#   REPO_URL REPO_BRANCH
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/kay-arne/fortigate-ot-lab.git}"
REPO_BRANCH="${REPO_BRANCH:-main}"
TITLE="FortiGate OT Lab"
# bridges die het lab zelf aanmaakt (lab/topology.yaml) - niet als uplink aanbieden
LAB_BRIDGES="vmbr900 vmbr910 vmbr920 vmbr930 vmbr935 vmbr999"

c_info() { printf '\033[1;36m[otlab]\033[0m %s\n' "$*"; }
c_ok()   { printf '\033[1;32m[otlab]\033[0m %s\n' "$*"; }
c_err()  { printf '\033[1;31m[otlab]\033[0m %s\n' "$*" >&2; }
die()    { c_err "$*"; exit 1; }
# whiptail draait in $(...)-subshells; een Cancel moet het hoofdscript stoppen
trap 'clear; c_err "Installatie afgebroken."; exit 1' USR1
cancel() { kill -USR1 $$; exit 1; }

header() {
  clear
  cat <<'EOF'
   ___ _____   _         _      _
  / _ \_   _| | |   __ _| |__  | FortiGate OT security lab
 | (_) || |   | |__/ _` | '_ \ | Purdue-model op Proxmox VE
  \___/ |_|   |____\__,_|_.__/ |
EOF
  echo
}

# --- whiptail helpers ----------------------------------------------------------
# Alle helpers geven de keuze terug op stdout en breken af bij Cancel/Esc.
w_input() {   # titel tekst default
  whiptail --backtitle "$TITLE" --title "$1" --inputbox "$2" 10 70 "$3" 3>&1 1>&2 2>&3 || cancel
}
w_menu() {    # titel tekst default tag1 item1 tag2 item2 ...
  local title=$1 text=$2 def=$3; shift 3
  local n=$(( $# / 2 )); (( n > 10 )) && n=10
  whiptail --backtitle "$TITLE" --title "$title" --default-item "$def" \
    --menu "$text" $(( n + 10 )) 78 "$n" "$@" 3>&1 1>&2 2>&3 || cancel
}
w_yesno() {   # titel tekst  -> rc 0 = ja
  whiptail --backtitle "$TITLE" --title "$1" --yesno "$2" 12 70
}
w_msg() {
  whiptail --backtitle "$TITLE" --title "$1" --msgbox "$2" 12 70
}

# --- validatie -----------------------------------------------------------------
is_cidr() { [[ $1 =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/([0-9]|[12][0-9]|3[0-2])$ ]]; }
is_ip()   { [[ $1 =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; }
is_vlan() { [[ -z $1 ]] || { [[ $1 =~ ^[0-9]+$ ]] && (( $1 >= 1 && $1 <= 4094 )); }; }
is_num()  { [[ $1 =~ ^[0-9]+$ ]]; }
vmid_free() { ! pct status "$1" >/dev/null 2>&1 && ! qm status "$1" >/dev/null 2>&1; }
range_free() {  # base: base..base+99 moeten vrij zijn
  local used
  used="$(pvesh get /cluster/resources --type vm --output-format json 2>/dev/null \
    | grep -o '"vmid":[0-9]*' | cut -d: -f2 | awk -v b="$1" '$1>=b && $1<b+100' | head -n1)"
  [[ -z $used ]]
}

# vraag tot de validator slaagt
ask_valid() {  # validator titel tekst default
  local fn=$1 title=$2 text=$3 val=$4
  while true; do
    val="$(w_input "$title" "$text" "$val")"
    $fn "$val" && { echo "$val"; return; }
    w_msg "$title" "Ongeldige waarde: '$val'"
  done
}

# --- keuzelijsten uit de host --------------------------------------------------
storage_items() {  # content-type -> "naam" "type, vrij GB" ...
  pvesm status -content "$1" 2>/dev/null | awk 'NR>1 && $3=="active" {
    printf "%s\n%s, %.0f GB vrij\n", $1, $2, $6/1024/1024 }'
}
bridge_items() {
  local br ip vlan
  for br in $(ip -o link show type bridge | awk -F': ' '{print $2}' | cut -d@ -f1 | sort); do
    [[ " $LAB_BRIDGES " == *" $br "* ]] && continue
    ip="$(ip -4 -o addr show "$br" | awk '{print $4}' | head -n1)"
    vlan="$(cat "/sys/class/net/$br/bridge/vlan_filtering" 2>/dev/null || echo 0)"
    printf '%s\n%s%s\n' "$br" "${ip:-geen IP}" "$([[ $vlan == 1 ]] && echo ', VLAN-aware')"
  done
}
to_args() { tr '\n' '\0'; }

# --- preflight -------------------------------------------------------------------
[[ $EUID -eq 0 ]] || die "Draai als root op de Proxmox host."
command -v pveversion >/dev/null || die "Dit is geen Proxmox VE host."
command -v pct >/dev/null || die "pct niet gevonden."

NODE="$(hostname)"
DEF_ROUTE_DEV="$(ip route show default | awk '{print $5; exit}')"
DEF_GW="$(ip route show default | awk '{print $3; exit}')"

# --- defaults --------------------------------------------------------------------
CTID="${CTID:-$(pvesh get /cluster/nextid)}"
CT_HOSTNAME="${CT_HOSTNAME:-otlab-controller}"
CT_CORES="${CT_CORES:-2}"
CT_MEMORY="${CT_MEMORY:-2048}"
CT_DISK="${CT_DISK:-16}"
CT_STORAGE="${CT_STORAGE:-$(storage_items rootdir | head -n1)}"
TPL_STORAGE="${TPL_STORAGE:-$(storage_items vztmpl | head -n1)}"
CT_BRIDGE="${CT_BRIDGE:-vmbr0}"
CT_VLAN="${CT_VLAN:-}"
CT_IP="${CT_IP:-dhcp}"
CT_GW="${CT_GW:-}"
CT_DNS="${CT_DNS:-}"
UI_PORT="${UI_PORT:-8080}"
LAB_STORAGE="${LAB_STORAGE:-}"
LAB_BRIDGE="${LAB_BRIDGE:-}"
LAB_VLAN="${LAB_VLAN-__same__}"
LAB_VMID_BASE="${LAB_VMID_BASE:-9100}"
TEMPLATE_NET="${TEMPLATE_NET:-}"
HOST_ACCESS="${HOST_ACCESS:-user}"
HOST_USER="${HOST_USER:-otlab}"
WORKDIR="/var/lib/vz/otlab"
HOST_IP="${HOST_IP:-$(ip -4 -o addr show "${DEF_ROUTE_DEV:-vmbr0}" | awk '{print $4}' | cut -d/ -f1 | head -n1)}"

# --- wizard ------------------------------------------------------------------------
advanced_settings() {
  local args

  CTID="$(ask_valid is_num "Container ID" "VMID van de controller-container:" "$CTID")"
  while ! vmid_free "$CTID"; do
    w_msg "Container ID" "ID $CTID is al in gebruik."
    CTID="$(ask_valid is_num "Container ID" "VMID van de controller-container:" "$(pvesh get /cluster/nextid)")"
  done
  CT_HOSTNAME="$(w_input "Hostname" "Hostname van de controller:" "$CT_HOSTNAME")"
  CT_CORES="$(ask_valid is_num "CPU" "Aantal cores:" "$CT_CORES")"
  CT_MEMORY="$(ask_valid is_num "Geheugen" "RAM in MiB:" "$CT_MEMORY")"
  CT_DISK="$(ask_valid is_num "Disk" "Disk in GB:" "$CT_DISK")"

  mapfile -d '' args < <(storage_items rootdir | to_args)
  (( ${#args[@]} )) || die "Geen storage met content 'rootdir' gevonden."
  CT_STORAGE="$(w_menu "Storage" "Storage voor de controller-container:" "$CT_STORAGE" "${args[@]}")"

  mapfile -d '' args < <(storage_items vztmpl | to_args)
  (( ${#args[@]} )) || die "Geen storage met content 'vztmpl' gevonden."
  TPL_STORAGE="$(w_menu "Template storage" "Storage voor het Debian LXC-template:" "$TPL_STORAGE" "${args[@]}")"

  mapfile -d '' args < <(bridge_items | to_args)
  (( ${#args[@]} )) || die "Geen Linux bridges gevonden."
  CT_BRIDGE="$(w_menu "Netwerk" "Bridge voor de controller (bereikbaar vanaf je beheer-pc):" "$CT_BRIDGE" "${args[@]}")"
  CT_VLAN="$(ask_valid is_vlan "VLAN" "VLAN-tag voor de controller (leeg = geen VLAN):" "$CT_VLAN")"

  if [[ "$(w_menu "IP-adres" "Adressering van de controller:" "$([[ $CT_IP == dhcp ]] && echo dhcp || echo static)" \
        dhcp "DHCP" static "Statisch IP")" == static ]]; then
    CT_IP="$(ask_valid is_cidr "Statisch IP" "IP-adres met prefix, bijv. 192.168.1.240/24:" "$([[ $CT_IP == dhcp ]] || echo "$CT_IP")")"
    CT_GW="$(ask_valid is_ip "Gateway" "Default gateway:" "${CT_GW:-$DEF_GW}")"
    CT_DNS="$(w_input "DNS" "DNS-server (leeg = instellingen van de host):" "$CT_DNS")"
  else
    CT_IP=dhcp; CT_GW=""
  fi
  UI_PORT="$(ask_valid is_num "Web UI" "TCP-poort van de web UI:" "$UI_PORT")"

  # --- lab ---
  mapfile -d '' args < <(storage_items images | to_args)
  (( ${#args[@]} )) || die "Geen storage met content 'images' gevonden."
  LAB_STORAGE="$(w_menu "Lab storage" "Storage voor de FortiGate-VM en de OT-containers:" "${LAB_STORAGE:-$CT_STORAGE}" "${args[@]}")"

  if w_yesno "FortiGate uplink" "Komt port1 (WAN) van de FortiGate op dezelfde bridge/VLAN als de controller?\n\nBridge: $CT_BRIDGE\nVLAN:   ${CT_VLAN:-geen}"; then
    LAB_BRIDGE="$CT_BRIDGE"; LAB_VLAN="$CT_VLAN"
  else
    mapfile -d '' args < <(bridge_items | to_args)
    LAB_BRIDGE="$(w_menu "FortiGate uplink" "Bridge voor FortiGate port1 (de enige weg naar het OT-lab):" "$CT_BRIDGE" "${args[@]}")"
    [[ $LAB_VLAN == __same__ ]] && LAB_VLAN="$CT_VLAN"
    LAB_VLAN="$(ask_valid is_vlan "FortiGate uplink" "VLAN-tag voor FortiGate port1 (leeg = geen VLAN):" "$LAB_VLAN")"
  fi

  w_msg "Template-build" "Het OT-template haalt eenmalig packages van internet via $LAB_BRIDGE${LAB_VLAN:+ (VLAN $LAB_VLAN)}. Daarna wordt het netwerk van het template weer losgekoppeld."
  if [[ "$(w_menu "Template-build" "Adressering tijdens de template-build:" dhcp dhcp "DHCP" static "Tijdelijk statisch IP")" == static ]]; then
    local tip tgw
    tip="$(ask_valid is_cidr "Template-build" "Tijdelijk IP met prefix (ander IP dan de controller):" "")"
    tgw="$(ask_valid is_ip "Template-build" "Gateway:" "${CT_GW:-$DEF_GW}")"
    TEMPLATE_NET="ip=$tip,gw=$tgw"
  else
    TEMPLATE_NET="ip=dhcp"
  fi

  LAB_VMID_BASE="$(ask_valid is_num "VMID-reeks" "Eerste VMID van het lab (gebruikt <basis> t/m <basis>+99):" "$LAB_VMID_BASE")"
  HOST_IP="$(ask_valid is_ip "Proxmox host" "IP waarop de controller deze host via SSH bereikt:" "$HOST_IP")"
}

access_settings() {  # $1 = advanced (1/0)
  if w_yesno "Toegang tot deze host" "De controller beheert deze Proxmox host via SSH (qm, pct, pvesh).

Aanbevolen: een aparte gebruiker met een eigen SSH-sleutel en sudo-rechten voor alleen die commando's. De toegang trek je later in met: otlab-revoke

Ja  = aparte gebruiker aanmaken (sudo wordt zo nodig geinstalleerd)
Nee = sleutel aan root koppelen"; then
    HOST_ACCESS=user
    if [[ $1 == 1 ]]; then
      while true; do
        HOST_USER="$(w_input "Toegang tot deze host" "Naam van de gebruiker op de host:" "$HOST_USER")"
        [[ $HOST_USER =~ ^[a-z_][a-z0-9_-]{0,30}$ && $HOST_USER != root ]] && break
        w_msg "Toegang tot deze host" "Ongeldige gebruikersnaam: '$HOST_USER'"
      done
    fi
  else
    HOST_ACCESS=root
  fi
}

finalize_defaults() {
  LAB_STORAGE="${LAB_STORAGE:-$CT_STORAGE}"
  LAB_BRIDGE="${LAB_BRIDGE:-$CT_BRIDGE}"
  [[ $LAB_VLAN == __same__ ]] && LAB_VLAN="$CT_VLAN"
  TEMPLATE_NET="${TEMPLATE_NET:-ip=dhcp}"
}

summary() {
  cat <<EOF
Controller-container
  ID / hostname : $CTID / $CT_HOSTNAME
  Resources     : $CT_CORES cores, $CT_MEMORY MiB RAM, $CT_DISK GB op $CT_STORAGE
  Netwerk       : $CT_BRIDGE${CT_VLAN:+ VLAN $CT_VLAN}, $CT_IP${CT_GW:+ gw $CT_GW}${CT_DNS:+ dns $CT_DNS}
  Web UI        : poort $UI_PORT

Lab
  Storage       : $LAB_STORAGE
  FortiGate WAN : $LAB_BRIDGE${LAB_VLAN:+ VLAN $LAB_VLAN}
  Template-net  : $TEMPLATE_NET
  VMID's        : $LAB_VMID_BASE - $(( LAB_VMID_BASE + 99 ))
  Proxmox host  : $NODE ($HOST_IP)
  Host-toegang  : $([[ $HOST_ACCESS == user ]] && echo "gebruiker $HOST_USER (sudo: qm, pct, pvesh)" || echo "root")$([[ $CT_IP == dhcp ]] && echo ", sleutel niet aan IP gebonden (DHCP)")
EOF
}

if [[ ${OTLAB_UNATTENDED:-0} == 1 || ! -t 0 ]]; then
  finalize_defaults
  c_info "Onbeheerde installatie met:"; summary
else
  command -v whiptail >/dev/null || die "whiptail ontbreekt (apt install whiptail)."
  header
  while true; do
    mode="$(w_menu "Installatie" "Hoe wil je installeren?" default \
      default "Standaard instellingen" advanced "Geavanceerd (storage, netwerk, VLAN, IP, ...)")"
    [[ $mode == advanced ]] && advanced_settings
    access_settings "$([[ $mode == advanced ]] && echo 1 || echo 0)"
    finalize_defaults
    whiptail --backtitle "$TITLE" --title "Samenvatting" --yes-button "Installeren" --no-button "Aanpassen" \
      --yesno "$(summary)" 24 78 && break
    mode=advanced
  done
  clear; header
fi

# --- controles op de gekozen waarden --------------------------------------------------
vmid_free "$CTID" || die "CT $CTID bestaat al."
range_free "$LAB_VMID_BASE" || die "VMID-reeks $LAB_VMID_BASE-$(( LAB_VMID_BASE + 99 )) is niet vrij."
(( CTID < LAB_VMID_BASE || CTID > LAB_VMID_BASE + 99 )) || die "Controller-ID $CTID valt binnen de lab-reeks."
for br in "$CT_BRIDGE" "$LAB_BRIDGE"; do
  ip link show "$br" >/dev/null 2>&1 || die "Bridge $br bestaat niet."
done
[[ -n $CT_STORAGE && -n $TPL_STORAGE && -n $LAB_STORAGE ]] || die "Kon geen geschikte storage bepalen."
is_vlan "$CT_VLAN" && is_vlan "$LAB_VLAN" || die "Ongeldige VLAN-tag."
[[ -n $HOST_IP ]] || die "Kon het IP van de host niet bepalen (zet HOST_IP=...)."
[[ $HOST_ACCESS == user || $HOST_ACCESS == root ]] || die "HOST_ACCESS moet 'user' of 'root' zijn."
[[ $HOST_ACCESS == root || ( $HOST_USER =~ ^[a-z_][a-z0-9_-]{0,30}$ && $HOST_USER != root ) ]] || die "Ongeldige HOST_USER."

# --- Debian template -------------------------------------------------------------------
c_info "Debian 12 LXC-template ophalen naar $TPL_STORAGE..."
pveam update >/dev/null
TEMPLATE="$(pveam available --section system | awk '/debian-12-standard/ {print $2}' | sort -V | tail -n1)"
[[ -n "$TEMPLATE" ]] || die "Geen debian-12-standard template gevonden."
if ! pveam list "$TPL_STORAGE" | grep -q "$TEMPLATE"; then
  pveam download "$TPL_STORAGE" "$TEMPLATE"
fi
TPL_REF="$TPL_STORAGE:vztmpl/$TEMPLATE"

# --- controller LXC --------------------------------------------------------------------
NET0="name=eth0,bridge=$CT_BRIDGE,ip=$CT_IP"
[[ -n $CT_VLAN ]] && NET0="$NET0,tag=$CT_VLAN"
[[ $CT_IP != dhcp && -n $CT_GW ]] && NET0="$NET0,gw=$CT_GW"
EXTRA=()
[[ -n $CT_DNS ]] && EXTRA+=(--nameserver "$CT_DNS")

c_info "Controller-LXC $CTID aanmaken..."
pct create "$CTID" "$TPL_REF" \
  --hostname "$CT_HOSTNAME" \
  --cores "$CT_CORES" --memory "$CT_MEMORY" --swap 512 \
  --rootfs "$CT_STORAGE:$CT_DISK" \
  --net0 "$NET0" "${EXTRA[@]}" \
  --unprivileged 1 --features nesting=1 \
  --onboot 1 \
  --description "FortiGate OT Lab controller - web UI op poort $UI_PORT" \
  --tags "otlab"
pct start "$CTID"

c_info "Wachten op netwerk in de controller..."
CT_ADDR=""
for _ in $(seq 1 60); do
  CT_ADDR="$(pct exec "$CTID" -- sh -c "ip -4 -o addr show eth0 | awk '{print \$4}' | cut -d/ -f1" 2>/dev/null || true)"
  [[ -n "$CT_ADDR" ]] && pct exec "$CTID" -- sh -c "getent hosts deb.debian.org >/dev/null" && break
  sleep 2
done
[[ -n "$CT_ADDR" ]] || die "Controller kreeg geen IP-adres (DHCP/VLAN correct?)."
pct exec "$CTID" -- sh -c "getent hosts deb.debian.org >/dev/null" || die "Controller ($CT_ADDR) heeft geen internet/DNS."

c_info "Pakketten installeren in de controller..."
pct exec "$CTID" -- bash -c "export DEBIAN_FRONTEND=noninteractive; apt-get update -q && apt-get install -yq git python3-venv python3-pip openssh-client genisoimage ca-certificates curl >/dev/null"

c_info "Repo clonen ($REPO_URL @ $REPO_BRANCH)..."
pct exec "$CTID" -- git clone --depth 1 -b "$REPO_BRANCH" "$REPO_URL" /opt/otlab

pct exec "$CTID" -- bash -c "python3 -m venv /opt/otlab/.venv && /opt/otlab/.venv/bin/pip install -q -r /opt/otlab/controller/requirements.txt"

# --- koppeling controller -> Proxmox host -----------------------------------------------
# De controller beheert de host via SSH (qm/pct/pvesh). Standaard met een
# aparte gebruiker en een sudo-regel voor alleen die commando's; intrekken
# met /usr/local/sbin/otlab-revoke.
c_info "SSH-sleutel aanmaken in de controller..."
pct exec "$CTID" -- bash -c "mkdir -p /opt/otlab/data && ssh-keygen -q -t ed25519 -N '' -C otlab-controller -f /opt/otlab/data/id_ed25519"
PUBKEY="$(pct exec "$CTID" -- cat /opt/otlab/data/id_ed25519.pub)"
KEY_OPTS="no-agent-forwarding,no-port-forwarding,no-X11-forwarding"
# Bij DHCP kan het IP veranderen; dan binden we de sleutel niet aan het adres.
[[ $CT_IP != dhcp ]] && KEY_OPTS="from=\"$CT_ADDR\",$KEY_OPTS"

install -d -m 755 /var/lib/vz/template/iso

setup_access_user() {
  c_info "Gebruiker '$HOST_USER' inrichten op de host..."
  if ! command -v sudo >/dev/null; then
    c_info "sudo installeren..."
    DEBIAN_FRONTEND=noninteractive apt-get install -yq sudo >/dev/null
  fi
  id "$HOST_USER" >/dev/null 2>&1 || useradd --system --create-home --home-dir "/var/lib/$HOST_USER" \
    --shell /bin/bash --comment "FortiGate OT Lab controller" "$HOST_USER"
  local home
  home="$(getent passwd "$HOST_USER" | cut -d: -f6)"
  install -d -m 700 -o "$HOST_USER" -g "$HOST_USER" "$home/.ssh"
  echo "$KEY_OPTS $PUBKEY" > "$home/.ssh/authorized_keys"
  chown "$HOST_USER:$HOST_USER" "$home/.ssh/authorized_keys"
  chmod 600 "$home/.ssh/authorized_keys"
  install -d -m 750 -o "$HOST_USER" -g "$HOST_USER" "$WORKDIR"

  local tmp=/etc/sudoers.d/.otlab.tmp
  cat > "$tmp" <<EOF
# FortiGate OT Lab controller (CT $CTID). Intrekken met: otlab-revoke
Defaults:$HOST_USER !requiretty
$HOST_USER ALL=(root) NOPASSWD: $(command -v qm), $(command -v pct), $(command -v pvesh), \\
  $(command -v install) -m 0644 $WORKDIR/otlab-fgt-config.iso /var/lib/vz/template/iso/otlab-fgt-config.iso
EOF
  chmod 440 "$tmp"
  visudo -cf "$tmp" >/dev/null || { rm -f "$tmp"; die "Ongeldige sudo-regel gegenereerd."; }
  mv "$tmp" /etc/sudoers.d/otlab
}

setup_access_root() {
  c_info "Sleutel koppelen aan root..."
  # Op Proxmox is /root/.ssh/authorized_keys een symlink naar
  # /etc/pve/priv/authorized_keys (gedeeld in een cluster): het doelbestand
  # in place bijwerken zodat de symlink intact blijft.
  mkdir -p /root/.ssh && chmod 700 /root/.ssh
  local ak tmp
  ak="$(readlink -f /root/.ssh/authorized_keys)"
  tmp="$(mktemp)"
  grep -v 'otlab-controller$' "$ak" > "$tmp" 2>/dev/null || true
  echo "$KEY_OPTS $PUBKEY" >> "$tmp"
  cat "$tmp" > "$ak"
  rm -f "$tmp"
  install -d -m 750 "$WORKDIR"
}

install_revoke() {
  cat > /usr/local/sbin/otlab-revoke <<'EOF'
#!/usr/bin/env bash
# Trekt de toegang van de FortiGate OT Lab controller tot deze host in.
# Lab-VM's, containers en de controller zelf blijven bestaan.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Draai als root." >&2; exit 1; }
HOST_USER="__HOST_USER__"
if [[ -n $HOST_USER ]] && id "$HOST_USER" >/dev/null 2>&1; then
  pkill -u "$HOST_USER" 2>/dev/null || true
  userdel -r "$HOST_USER" 2>/dev/null || userdel "$HOST_USER"
  echo "Gebruiker $HOST_USER verwijderd."
fi
rm -f /etc/sudoers.d/otlab
ak="$(readlink -f /root/.ssh/authorized_keys)"
if [[ -f $ak ]] && grep -q 'otlab-controller$' "$ak"; then
  tmp="$(mktemp)"
  grep -v 'otlab-controller$' "$ak" > "$tmp" || true
  cat "$tmp" > "$ak"
  rm -f "$tmp"
  echo "Sleutel uit root authorized_keys verwijderd."
fi
echo "Toegang van de OT Lab controller ingetrokken."
EOF
  sed -i "s/__HOST_USER__/$([[ $HOST_ACCESS == user ]] && echo "$HOST_USER")/" /usr/local/sbin/otlab-revoke
  chmod 750 /usr/local/sbin/otlab-revoke
}

if [[ $HOST_ACCESS == user ]]; then
  setup_access_user
  SSH_USER="$HOST_USER"
else
  setup_access_root
  SSH_USER=root
fi
install_revoke

pct exec "$CTID" -- bash -c "cat > /opt/otlab/data/host.json" <<EOF
{
  "host": "$HOST_IP",
  "node": "$NODE",
  "ssh_user": "$SSH_USER",
  "ssh_key": "/opt/otlab/data/id_ed25519",
  "workdir": "$WORKDIR",
  "storage": "$LAB_STORAGE",
  "template": "$TPL_REF",
  "controller_ctid": $CTID,
  "uplink_bridge": "$LAB_BRIDGE",
  "uplink_vlan": "$LAB_VLAN",
  "template_net": "$TEMPLATE_NET",
  "vmid_base": $LAB_VMID_BASE
}
EOF

UI_PASSWORD="$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 16 || true)"
pct exec "$CTID" -- bash -c "umask 077; echo '$UI_PASSWORD' > /opt/otlab/data/ui_password"

c_info "Verbinding controller -> host testen..."
SUDO=""; [[ $SSH_USER != root ]] && SUDO="sudo -n "
pct exec "$CTID" -- bash -c "ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 -i /opt/otlab/data/id_ed25519 $SSH_USER@$HOST_IP '${SUDO}pvesh get /version >/dev/null'" \
  || die "Controller ($CT_ADDR) kan niet als $SSH_USER via SSH (en sudo) naar de host ($HOST_IP). Routing/firewall tussen VLAN's?"

# --- systemd service ----------------------------------------------------------------------
pct exec "$CTID" -- bash -c "cat > /etc/systemd/system/otlab.service" <<EOF
[Unit]
Description=FortiGate OT Lab controller
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/opt/otlab/controller
ExecStart=/opt/otlab/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port $UI_PORT
Restart=on-failure
Environment=OTLAB_ROOT=/opt/otlab

[Install]
WantedBy=multi-user.target
EOF
pct exec "$CTID" -- systemctl daemon-reload
pct exec "$CTID" -- systemctl enable --now otlab.service

c_ok "Klaar."
echo
echo "  Web UI:  http://$CT_ADDR:$UI_PORT"
echo "  Login:   admin / $UI_PASSWORD   (staat ook in CT $CTID: /opt/otlab/data/ui_password)"
echo "  Host:    controller beheert $NODE als '$SSH_USER'; intrekken met: otlab-revoke"
echo "  Volgende stap: upload het FortiGate-VM (KVM) qcow2-image + licentie in de UI en klik 'Deploy lab'."
echo
