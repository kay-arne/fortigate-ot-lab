"""SSH-koppeling naar de Proxmox host (qm / pct / pvesh)."""
from __future__ import annotations

import shlex
import threading

import paramiko

from .config import load_host


class HostError(RuntimeError):
    pass


class Host:
    def __init__(self):
        self.cfg = load_host()
        self._client = None
        self._lock = threading.Lock()

    @property
    def node(self) -> str:
        return self.cfg["node"]

    def _connect(self) -> paramiko.SSHClient:
        if self._client and self._client.get_transport() and self._client.get_transport().is_active():
            return self._client
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            self.cfg["host"],
            username=self.cfg.get("ssh_user", "root"),
            key_filename=self.cfg["ssh_key"],
            timeout=10,
        )
        self._client = client
        return client

    def run(self, cmd, check=True, timeout=900, log=None) -> str:
        """Voer een commando uit op de host. cmd mag een lijst of string zijn."""
        if isinstance(cmd, (list, tuple)):
            cmd = " ".join(shlex.quote(str(c)) for c in cmd)
        if log:
            log(f"$ {cmd}")
        with self._lock:
            client = self._connect()
        _, stdout, stderr = client.exec_command(cmd, timeout=timeout)
        out = stdout.read().decode(errors="replace")
        err = stderr.read().decode(errors="replace")
        rc = stdout.channel.recv_exit_status()
        if check and rc != 0:
            raise HostError(f"'{cmd}' faalde (rc={rc}): {err.strip() or out.strip()}")
        return out

    def put(self, local_path, remote_path, callback=None):
        with self._lock:
            client = self._connect()
        sftp = client.open_sftp()
        try:
            sftp.put(str(local_path), remote_path, callback=callback)
        finally:
            sftp.close()

    def put_text(self, text: str, remote_path: str):
        with self._lock:
            client = self._connect()
        sftp = client.open_sftp()
        try:
            with sftp.open(remote_path, "w") as f:
                f.write(text)
        finally:
            sftp.close()

    # --- helpers ---------------------------------------------------------
    def exists(self, vmid: int) -> str | None:
        """'qemu', 'lxc' of None."""
        out = self.run(["pvesh", "get", "/cluster/resources", "--type", "vm", "--output-format", "json"])
        import json
        for r in json.loads(out):
            if r.get("vmid") == vmid:
                return r["type"]
        return None

    def status(self, vmid: int) -> str:
        kind = self.exists(vmid)
        if not kind:
            return "absent"
        tool = "qm" if kind == "qemu" else "pct"
        out = self.run([tool, "status", str(vmid)], check=False)
        return out.split(":", 1)[-1].strip() or "unknown"

    def pct_exec(self, vmid: int, command: str, timeout=300, check=False) -> tuple[int, str]:
        """Draai een shell-commando in een LXC; geeft (rc, output) terug."""
        full = f"pct exec {vmid} -- bash -lc {shlex.quote(command)} 2>&1; echo \"__rc=$?\""
        out = self.run(full, check=False, timeout=timeout)
        body, _, rc = out.rpartition("__rc=")
        code = int(rc.strip() or 1)
        if check and code != 0:
            raise HostError(f"pct exec {vmid} faalde: {body.strip()[-500:]}")
        return code, body


_host = None


def host() -> Host:
    global _host
    if _host is None:
        _host = Host()
    return _host
