#!/usr/bin/env python3
"""
pve-rolling-updater.py - rolling apt dist-upgrade of the Proxmox cluster, one node at a time,
with a live TUI. Talks to the nodes by IP only, so DNS outages don't matter.

Before anything: cluster quorate, every node online, HA idle (and Ceph PGs clean, if Ceph).
Up front, in parallel: `apt-get update` + download upgrades on every node.
Then per node, stopping the whole rollout on the first failure:
  1. HA maintenance mode on, wait until every running guest has left the node
     (non-HA guests are migrated by the script to the node with the most free RAM)
  2. `apt-get dist-upgrade` (Proxmox's recommended way; never plain `upgrade`).
     Aborts if apt wants to remove proxmox-ve / pve-manager (usually a repo problem).
  3. Reboot ONLY if needed: running kernel != newest installed (or pinned) kernel,
     or /var/run/reboot-required. Ceph: `noout` is set for the reboot.
  4. Wait for the node to boot, quorum + PVE/HA services up, verify kernel + pve-manager version
  5. Maintenance mode off, non-HA guests migrated back, wait for HA to settle - next node

Settings come from .env next to this script (copy .env.example and fill it in).

Keys: up/down scroll, q = stop after the current node, Ctrl-C twice = abort (avoid mid-node).

Usage:
  ./pve-rolling-updater.py                  # roll through every node that has updates
  ./pve-rolling-updater.py --check-only     # refresh and report, change nothing
  ./pve-rolling-updater.py --only pve2
"""
from __future__ import annotations

import argparse
import curses
import datetime as dt
import json
import locale
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import traceback
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

# -- Configuration (.env next to this script, see .env.example) ----------------
ENV_FILE = Path(__file__).resolve().parent / ".env"


def read_env(path: Path) -> dict[str, str]:
    """KEY=value lines; blank lines, # comments, `export` and quotes are allowed."""
    out = {}
    if path.is_file():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[7:]
            key, sep, val = line.partition("=")
            if not sep or line.startswith("#"):
                continue
            val = val.strip()
            if len(val) > 1 and val[0] in "'\"" and val[-1] == val[0]:
                val = val[1:-1]
            else:
                val = val.split(" #", 1)[0].strip()
            out[key.strip()] = val
    return out


DOTENV = read_env(ENV_FILE)


def env(key: str, default: str = "") -> str:
    """Real environment variables win over .env."""
    return os.environ.get(key, DOTENV.get(key, default)).strip()


def env_list(key: str, default: str = "") -> list[str]:
    return [x.strip() for x in env(key, default).split(",") if x.strip()]


def env_int(key: str, default: int) -> int:
    v = env(key)
    try:
        return int(v) if v else default
    except ValueError:
        sys.exit(f"{key} must be a whole number, got {v!r} ({ENV_FILE})")


def env_bool(key: str, default: bool) -> bool:
    v = env(key).lower()
    return v in ("1", "true", "yes", "on") if v else default


NODES = env_list("PVE_NODES")                     # any reachable one is used
SSH_USER = env("SSH_USER")
SSH_KEY = Path(env("SSH_KEY")).expanduser()
SUDO = env("SSH_SUDO", "" if SSH_USER == "root" else "sudo -n")
SSH_EXTRA = ((["-p", env("SSH_PORT")] if env("SSH_PORT") else [])
             + (["-o", f"StrictHostKeyChecking={env('SSH_STRICT_HOST_KEY_CHECKING')}"]
                if env("SSH_STRICT_HOST_KEY_CHECKING") else []))
LOG_DIR = Path(env("UPDATER_LOG_DIR") or Path(__file__).resolve().parent / "logs").expanduser()
CONTROL_PATH = "~/.ssh/cm-pve-upd-%r@%h:%p"

MAINT_ALWAYS = env_bool("PVE_UPDATE_MAINT_ALWAYS", True)  # False = only enter maintenance mode when the node will reboot
PROTECTED = set(env_list("PVE_UPDATE_PROTECTED", "proxmox-ve,pve-manager"))  # abort the node if apt wants to remove these

REFRESH_TIMEOUT = env_int("PVE_UPDATE_REFRESH_TIMEOUT", 15 * 60)
UPGRADE_TIMEOUT = env_int("PVE_UPDATE_UPGRADE_TIMEOUT", 30 * 60)
EVAC_TIMEOUT = env_int("PVE_UPDATE_EVAC_TIMEOUT", 30 * 60)
MIGRATE_TIMEOUT = env_int("PVE_UPDATE_MIGRATE_TIMEOUT", 30 * 60)
BOOT_TIMEOUT = env_int("PVE_UPDATE_BOOT_TIMEOUT", 15 * 60)
SETTLE_TIMEOUT = env_int("PVE_UPDATE_SETTLE_TIMEOUT", 15 * 60)  # HA/Ceph settling
POLL = 5
LOG_LINES = 7


# -- Logging / ssh -------------------------------------------------------------
class Log:
    fh = None
    lock = threading.Lock()

    @classmethod
    def write(cls, msg: str) -> None:
        if cls.fh:
            with cls.lock:
                cls.fh.write(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}\n")
                cls.fh.flush()


class StepError(Exception):
    pass


def fmt(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"


def sudo(cmd: str) -> str:
    return f"{SUDO} {cmd}".strip()


def ssh(host: str, cmd: str, timeout: int = 60, check: bool = True,
        log_output: bool = True) -> subprocess.CompletedProcess:
    argv = ["ssh", "-i", str(SSH_KEY), *SSH_EXTRA,
            "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=3",
            "-o", "ControlMaster=auto", "-o", "ControlPersist=120",
            "-o", f"ControlPath={CONTROL_PATH}",
            f"{SSH_USER}@{host}", cmd]
    Log.write(f"[{host}] $ {cmd}")
    for attempt in (1, 2):
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                               stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            Log.write(f"[{host}] timed out after {timeout}s")
            if check:
                raise StepError(f"timed out after {fmt(timeout)}")
            return subprocess.CompletedProcess(argv, 124, "", "timeout")
        if attempt == 1 and r.returncode == 255 and re.search(
                r"Connection (reset|refused|closed)|kex_exchange|banner exchange|timed out",
                r.stderr):
            time.sleep(3)
            continue
        break
    if log_output:
        for line in (r.stdout + r.stderr).strip().splitlines():
            Log.write(f"[{host}]   {line}")
    Log.write(f"[{host}] exit {r.returncode}")
    if check and r.returncode != 0:
        lines = (r.stderr.strip() or r.stdout.strip()).splitlines()
        raise StepError(f"exit {r.returncode}" + (f": {lines[-1]}" if lines else ""))
    return r


def close_master(host: str) -> None:
    subprocess.run(["ssh", *SSH_EXTRA, "-o", f"ControlPath={CONTROL_PATH}", "-O", "exit",
                    f"{SSH_USER}@{host}"], capture_output=True)


def pvesh(host: str, path: str, *args: str):
    r = ssh(host, sudo(f"pvesh get {path} {' '.join(args)} --output-format json"),
            log_output=False)
    return json.loads(r.stdout or "null")


# -- Remote scripts ------------------------------------------------------------
APT = ("export DEBIAN_FRONTEND=noninteractive; ")
APT_OPTS = ("-o DPkg::Lock::Timeout=300 -o Dpkg::Options::=--force-confdef "
            "-o Dpkg::Options::=--force-confold")
INFO = r'''
pveversion | sed -n 's|^pve-manager/\([^/]*\)/.*|\1|p'
uname -r
pin=$(proxmox-boot-tool kernel list 2>/dev/null | sed -n '/^Pinned kernel:/{n;p;}' | head -n1)
if [ -n "$pin" ]; then echo "$pin"; else ls /boot/vmlinuz-* | sed 's|.*/vmlinuz-||' | sort -V | tail -n1; fi
[ -f /var/run/reboot-required ] && echo REBOOT || echo NO
'''
REFRESH = APT + r'''
apt-get -qq -o DPkg::Lock::Timeout=300 update >/dev/null || exit 1
apt-get -s -o Debug::NoLocking=1 dist-upgrade | awk '/^(Inst|Remv) /{print $1, $2}'
'''
DOWNLOAD = APT + f"apt-get -qq -d -y {APT_OPTS} dist-upgrade"
UPGRADE = APT + f"apt-get -y -q {APT_OPTS} dist-upgrade"
SERVICES_UP = ("pvecm status 2>/dev/null | grep -q 'Quorate:.*Yes' && systemctl is-active -q "
               "corosync pve-cluster pvedaemon pveproxy pve-ha-lrm pve-ha-crm")
HA_BUSY = ("migrate", "relocate", "starting", "recovery", "fence", "request_stop", "freeze")


# -- Model ---------------------------------------------------------------------
@dataclass
class Node:
    name: str
    ip: str
    pve: str = ""
    kernel: str = ""
    expected: str = ""
    flag: bool = False
    status: str = "pending"
    detail: str = ""
    inst: list[str] = field(default_factory=list)
    remv: list[str] = field(default_factory=list)
    since: float = field(default_factory=time.time)

    def run(self, cmd: str, timeout: int = 60, check: bool = True,
            log_output: bool = True) -> subprocess.CompletedProcess:
        return ssh(self.ip, sudo(f"sh -c {shlex.quote(cmd)}"), timeout=timeout,
                   check=check, log_output=log_output)

    def info(self) -> None:
        lines = self.run(INFO, log_output=False).stdout.strip().splitlines()
        if len(lines) < 4:
            raise StepError(f"unexpected version output: {lines}")
        self.pve, self.kernel, self.expected, flag = lines[-4:]
        self.flag = flag == "REBOOT"

    @property
    def needs_reboot(self) -> bool:
        return self.flag or (bool(self.expected) and self.kernel != self.expected)

    @property
    def kernel_update(self) -> bool:
        return any(p.startswith(("proxmox-kernel-", "pve-kernel-")) for p in self.inst)


class Board:
    def __init__(self, plain: bool):
        self.rows: list[Node] = []
        self.log: deque[str] = deque(maxlen=200)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.plain = plain
        self.phase = "Starting"
        self.ceph = False
        self.done = False
        self.error = ""
        self.t0 = time.time()

    def set(self, row: Node, status: str | None = None, detail: str | None = None) -> None:
        with self.lock:
            changed = bool(status) and status != row.status
            if changed:
                row.status, row.since = status, time.time()
            if detail is not None:
                row.detail = detail
        if changed:
            line = f"{row.name}: {row.status}" + (f" - {row.detail}" if row.detail else "")
            Log.write(f"STATE {line}")
            if self.plain:
                print(f"  {line}", flush=True)

    def note(self, msg: str) -> None:
        with self.lock:
            self.log.append(msg)
        Log.write(f"NOTE  {msg}")
        if self.plain:
            print(f"  ! {msg}", flush=True)

    def peer(self, row: Node) -> Node:
        return next(r for r in self.rows if r is not row)

    def ip_of(self, name: str) -> str:
        return next(r.ip for r in self.rows if r.name == name)


def wait_for(board: Board, row: Node, check, timeout: int, what: str, stable: int = 2) -> str:
    deadline, streak = time.time() + timeout, 0
    while True:
        try:
            done, detail = check()
        except (StepError, json.JSONDecodeError, ValueError) as e:
            done, detail = False, f"waiting ({str(e)[:60]})"
        board.set(row, detail=detail)
        streak = streak + 1 if done else 0
        if streak >= stable:
            return detail
        if time.time() > deadline:
            raise StepError(f"timed out after {fmt(timeout)} {what} ({detail})")
        time.sleep(POLL)


# -- Cluster checks ------------------------------------------------------------
def cluster_ok(board: Board, via: Node) -> tuple[bool, str]:
    st = pvesh(via.ip, "/cluster/status") or []
    cl = next((s for s in st if s.get("type") == "cluster"), {})
    off = [s["name"] for s in st if s.get("type") == "node" and not s.get("online")]
    if not cl.get("quorate"):
        return False, "cluster not quorate"
    if off:
        return False, f"node(s) offline: {', '.join(off)}"
    ha = pvesh(via.ip, "/cluster/ha/status/current") or []
    busy = [str(s.get("sid") or s.get("id")) for s in ha if s.get("type") == "service"
            and any(b in f"{s.get('state', '')} {s.get('status', '')}" for b in HA_BUSY)]
    maint = [s.get("node") for s in ha if s.get("type") == "lrm" and "maintenance" in json.dumps(s)]
    if busy:
        return False, f"HA busy: {', '.join(busy[:3])}"
    if maint:
        return False, f"in maintenance: {', '.join(maint)}"
    if board.ceph:
        r = via.run("ceph pg stat", timeout=30, log_output=False)
        m = re.match(r"\s*(\d+) pgs: (\d+) active\+clean[;,]", r.stdout)
        if not (m and m.group(1) == m.group(2)):
            return False, "ceph: " + r.stdout.strip()[:60]
    return True, "cluster healthy"


def running_guests(board: Board, row: Node) -> tuple[list[dict], list[dict]]:
    """(HA-managed, not HA-managed) guests still running on row."""
    via = board.peer(row)
    res = pvesh(via.ip, "/cluster/resources", "--type", "vm") or []
    ha = {r["sid"]: r.get("state") for r in (pvesh(via.ip, "/cluster/ha/resources") or [])}
    mine = [g for g in res if g.get("node") == row.name and g.get("status") == "running"
            and not g.get("template")]
    sid = lambda g: ("ct:" if g["type"] == "lxc" else "vm:") + str(g["vmid"])
    managed = [g for g in mine if ha.get(sid(g), "ignored") not in ("ignored", "disabled")]
    return managed, [g for g in mine if g not in managed]


def migrate(src_ip: str, g: dict, target: str) -> None:
    if g["type"] == "lxc":
        cmd = f"pct migrate {g['vmid']} {target} --restart"
    else:
        cmd = f"qm migrate {g['vmid']} {target} --online --with-local-disks"
    ssh(src_ip, sudo(cmd), timeout=MIGRATE_TIMEOUT)


# -- Per-node steps ------------------------------------------------------------
def enter_maintenance(board: Board, row: Node) -> list[tuple[dict, str]]:
    board.set(row, "evacuating", "enabling HA maintenance mode")
    ssh(row.ip, sudo(f"ha-manager crm-command node-maintenance enable {row.name}"))

    def ha_gone():
        managed, _ = running_guests(board, row)
        names = ", ".join(g.get("name", str(g["vmid"])) for g in managed[:4])
        return not managed, f"{len(managed)} HA guest(s) left: {names}" if managed else "HA guests moved"

    wait_for(board, row, ha_gone, EVAC_TIMEOUT, "waiting for HA to move guests off")
    moved = []
    _, others = running_guests(board, row)
    for g in others:
        via = board.peer(row)
        nodes = [n for n in (pvesh(via.ip, "/cluster/resources", "--type", "node") or [])
                 if n.get("node") != row.name and n.get("status") == "online"]
        target = max(nodes, key=lambda n: n.get("maxmem", 0) - n.get("mem", 0))["node"]
        board.set(row, detail=f"migrating non-HA {g['type']} {g['vmid']} "
                              f"{g.get('name', '')} → {target}")
        migrate(row.ip, g, target)
        moved.append((g, target))
    managed, others = running_guests(board, row)
    if managed or others:
        raise StepError(f"{len(managed) + len(others)} guest(s) still running on node")
    if moved:
        board.note(f"{row.name}: migrated {len(moved)} non-HA guest(s) away")
    return moved


def exit_maintenance(board: Board, row: Node, moved: list[tuple[dict, str]]) -> None:
    board.set(row, "restoring", "disabling HA maintenance mode")
    ssh(row.ip, sudo(f"ha-manager crm-command node-maintenance disable {row.name}"))
    for g, target in moved:
        board.set(row, detail=f"migrating {g['type']} {g['vmid']} {g.get('name', '')} back")
        try:
            migrate(board.ip_of(target), g, row.name)
        except StepError as e:
            board.note(f"{g.get('name', g['vmid'])}: couldn't move back to {row.name} ({e})")
    wait_for(board, row, lambda: cluster_ok(board, row), SETTLE_TIMEOUT,
             "waiting for HA/cluster to settle")


def reboot(board: Board, row: Node) -> None:
    if board.ceph:
        row.run("ceph osd set noout")
    old_boot = row.run("cat /proc/sys/kernel/random/boot_id", log_output=False).stdout.strip()
    board.set(row, "rebooting", f"→ kernel {row.expected}")
    row.run("systemctl reboot", timeout=30, check=False)
    close_master(row.ip)

    def back():
        bid = ssh(row.ip, "cat /proc/sys/kernel/random/boot_id", timeout=10,
                  log_output=False).stdout.strip()
        return bid != old_boot, "rebooted, waiting for services" if bid != old_boot else "shutting down"

    wait_for(board, row, back, BOOT_TIMEOUT, "waiting for node to reboot", stable=1)
    board.set(row, "booting", "waiting for quorum + PVE services")
    wait_for(board, row, lambda: (row.run(SERVICES_UP, timeout=30, check=False,
                                          log_output=False).returncode == 0, "services up"),
             BOOT_TIMEOUT, "waiting for quorum + PVE services")
    if board.ceph:
        board.set(row, "verifying", "ceph: unsetting noout, waiting for PGs active+clean")
        row.run("ceph osd unset noout")


def process(board: Board, row: Node) -> None:
    step = "pre-check"
    in_maint, moved = False, []
    try:
        board.set(row, "verifying", "pre-check")
        ok, why = cluster_ok(board, board.peer(row))
        if not ok:
            raise StepError(why)
        before_pve, before_kernel = row.pve, row.kernel
        if MAINT_ALWAYS or row.needs_reboot or row.kernel_update:
            step = "maintenance"
            moved, in_maint = enter_maintenance(board, row), True
        if row.inst:
            step = "upgrade"
            board.set(row, "upgrading", f"{len(row.inst)} package(s)")
            row.run(UPGRADE, timeout=UPGRADE_TIMEOUT)
        row.info()
        rebooted = False
        if row.needs_reboot:
            if not in_maint:
                step = "maintenance"
                moved, in_maint = enter_maintenance(board, row), True
            step = "reboot"
            reboot(board, row)
            rebooted = True
            step = "verify"
            board.set(row, "verifying", "checking kernel + versions")
            expected = row.expected
            row.info()
            if row.kernel != expected:
                raise StepError(f"booted {row.kernel}, expected {expected}")
        if in_maint:
            step = "leave maintenance"
            exit_maintenance(board, row, moved)
            in_maint = False
        pve = f"pve {before_pve}→{row.pve}" if before_pve != row.pve else f"pve {row.pve}"
        kern = (f"kernel {before_kernel}→{row.kernel}" if before_kernel != row.kernel
                else f"kernel {row.kernel}")
        board.set(row, "updated", f"{pve} · {kern} · "
                  + ("rebooted" if rebooted else "no reboot needed"))
    except StepError as e:
        board.set(row, "failed", f"{step}: {e}")
        board.note(f"Rollout stopped at {row.name} ({step}).")
        if in_maint:
            board.note(f"{row.name} left in maintenance mode. When fixed: "
                       f"ha-manager crm-command node-maintenance disable {row.name}")
        if moved:
            board.note("Non-HA guests moved away (migrate back by hand): "
                       + ", ".join(f"{g.get('name', g['vmid'])}→{t}" for g, t in moved))
        if board.ceph and step in ("reboot", "verify"):
            board.note("Ceph noout may still be set: ceph osd unset noout")
        raise


# -- Phases --------------------------------------------------------------------
def discover(board: Board) -> None:
    board.phase = "Discovering"
    entry = next((ip for ip in NODES if ssh(ip, "true", timeout=10, check=False).returncode == 0),
                 None)
    if not entry:
        raise StepError("couldn't reach any Proxmox node")
    st = pvesh(entry, "/cluster/status") or []
    nodes = sorted((s for s in st if s.get("type") == "node"), key=lambda s: s["name"])
    off = [s["name"] for s in nodes if not s.get("online")]
    if off:
        raise StepError(f"node(s) offline: {', '.join(off)} - not rolling a degraded cluster")
    if len(nodes) < 2:
        raise StepError("need at least 2 nodes to roll updates")
    with board.lock:
        board.rows = [Node(s["name"], s["ip"]) for s in nodes]
    r = ssh(entry, sudo("sh -c '[ -f /etc/pve/ceph.conf ] && command -v ceph >/dev/null && echo yes'"),
            check=False, log_output=False)
    board.ceph = "yes" in r.stdout
    board.note(f"Using {entry}; {len(nodes)} nodes" + ("; Ceph detected" if board.ceph else ""))


def refresh_phase(board: Board, rows: list[Node], download: bool) -> None:
    board.phase = "Refreshing package lists"

    def refresh(row: Node) -> None:
        step = "read versions"
        try:
            board.set(row, "checking", "")
            row.info()
            step = "apt-get update"
            board.set(row, "refreshing", "")
            out = row.run(REFRESH, timeout=REFRESH_TIMEOUT, log_output=False).stdout
            row.inst = [ln.split()[1] for ln in out.splitlines() if ln.startswith("Inst ")]
            row.remv = [ln.split()[1] for ln in out.splitlines() if ln.startswith("Remv ")]
            bad = PROTECTED & set(row.remv)
            if bad:
                raise StepError(f"dist-upgrade would REMOVE {', '.join(sorted(bad))} - check repos")
            if not row.inst:
                if row.needs_reboot:
                    board.set(row, "reboot pending", f"running {row.kernel}, installed {row.expected}")
                else:
                    board.set(row, "up to date", "")
                return
            if download:
                step = "download"
                board.set(row, "downloading", f"{len(row.inst)} package(s)")
                row.run(DOWNLOAD, timeout=REFRESH_TIMEOUT)
            extra = (" · new kernel → reboot" if row.kernel_update or row.needs_reboot else "")
            extra += f" · removes {', '.join(row.remv[:3])}" if row.remv else ""
            board.set(row, "ready", f"{len(row.inst)} pkg(s): {', '.join(row.inst[:4])}"
                      f"{' …' if len(row.inst) > 4 else ''}{extra}")
        except StepError as e:
            board.set(row, "failed", f"{step}: {e}")

    with ThreadPoolExecutor(len(rows)) as ex:
        list(ex.map(refresh, rows))


def work(board: Board, args) -> None:
    try:
        discover(board)
        board.phase = "Pre-check"
        ok, why = cluster_ok(board, board.rows[0])
        if not ok:
            raise StepError(f"pre-check: {why}")
        refresh_phase(board, board.rows, download=not args.check_only)
        if any(r.status == "failed" for r in board.rows):
            raise StepError("refresh failed on a node - fix that before rolling")
        todo = [r for r in board.rows if r.status in ("ready", "reboot pending")
                and (not args.only or r.name in args.only)]
        for r in board.rows:
            if args.only and r.name not in args.only and r.status in ("ready", "reboot pending"):
                board.set(r, "skipped", "not in --only")
        if not args.check_only:
            for r in todo:
                board.set(r, "queued")
            for i, r in enumerate(todo, 1):
                if board.stop.is_set():
                    for rest in todo[i - 1:]:
                        board.set(rest, "skipped", "stopped by user")
                    break
                board.phase = f"Rolling update ({i}/{len(todo)}): {r.name}"
                try:
                    process(board, r)
                except StepError:
                    for rest in todo[i:]:
                        board.set(rest, "skipped", "rollout stopped")
                    board.error = f"{r.name} failed"
                    break
        board.phase = ("Failed" if board.error else "Stopped" if board.stop.is_set() else "Done")
    except StepError as e:
        board.error, board.phase = str(e), "Failed"
        board.note(f"FAILED: {e}")
    except Exception as e:
        board.error, board.phase = repr(e), "Crashed"
        Log.write(traceback.format_exc())
        board.note(f"CRASHED: {e!r} (see log)")
    finally:
        board.done = True


# -- TUI -----------------------------------------------------------------------
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
ACTIVE = {"checking", "refreshing", "downloading", "evacuating", "upgrading", "rebooting",
          "booting", "verifying", "restoring"}
PAIR = {"checking": 4, "refreshing": 4, "downloading": 4, "verifying": 4, "up to date": 1,
        "ready": 3, "reboot pending": 3, "queued": 3, "evacuating": 5, "upgrading": 5,
        "rebooting": 5, "booting": 5, "restoring": 5, "updated": 1, "failed": 2}
BOLD_STATES = {"updated", "failed", "ready", "reboot pending"}


def status_attr(s: str) -> int:
    if s not in PAIR:
        return curses.A_DIM
    return curses.color_pair(PAIR[s]) | (curses.A_BOLD if s in BOLD_STATES else 0)


def put(scr, y: int, x: int, text: str, attr: int = 0) -> int:
    h, w = scr.getmaxyx()
    if 0 <= y < h and x < w - 1:
        try:
            scr.addstr(y, x, text[:w - 1 - x], attr)
        except curses.error:
            pass
    return x + len(text)


def draw(scr, board: Board, offset: int, frame: int) -> int:
    scr.erase()
    h, w = scr.getmaxyx()
    with board.lock:
        rows, logs = list(board.rows), list(board.log)
    put(scr, 0, 0, f" PVE rolling updater │ {board.phase} │ {fmt(time.time() - board.t0)} ".ljust(w),
        curses.A_REVERSE | curses.A_BOLD)
    put(scr, 2, 0, f"{'NODE':<10} {'IP':<15} {'PVE':<9} {'KERNEL':<17} {'STATUS':<16} DETAIL",
        curses.A_UNDERLINE | curses.A_BOLD)
    log_h = min(LOG_LINES, max(0, h - 10))
    sep = h - log_h - 2
    body_h = max(0, sep - 3)
    offset = max(0, min(offset, len(rows) - body_h))
    for i, r in enumerate(rows[offset:offset + body_h]):
        y = 3 + i
        x = put(scr, y, 0, f"{r.name:<10.10} ", curses.A_BOLD)
        x = put(scr, y, x, f"{r.ip:<15} ", curses.A_DIM)
        x = put(scr, y, x, f"{r.pve:<9.9} ")
        x = put(scr, y, x, f"{r.kernel:<17.17} ",
                curses.color_pair(3) if r.needs_reboot and r.status != "updated" else 0)
        st = (SPIN[frame % len(SPIN)] if r.status in ACTIVE else " ") + " " + r.status
        x = put(scr, y, x, f"{st:<16.16} ", status_attr(r.status))
        extra = f" ({fmt(time.time() - r.since)})" if r.status in ACTIVE else ""
        put(scr, y, x, r.detail + extra, curses.A_DIM)
    put(scr, sep, 0, "─ log " + "─" * (w - 8), curses.A_DIM)
    for i, line in enumerate(logs[-log_h:] if log_h else []):
        put(scr, sep + 1 + i, 1, line, curses.A_DIM)
    if board.done:
        foot, attr = " done - press any key to exit", curses.A_REVERSE
    elif board.stop.is_set():
        foot, attr = " stopping after the current node… (Ctrl-C again to abort)", curses.color_pair(3)
    else:
        foot, attr = " q stop after current node", curses.A_DIM
    put(scr, h - 1, 0, foot.ljust(w), attr)
    scr.refresh()
    return offset


def tui(scr, board: Board) -> None:
    curses.curs_set(0)
    curses.use_default_colors()
    for i, c in enumerate((curses.COLOR_GREEN, curses.COLOR_RED, curses.COLOR_YELLOW,
                           curses.COLOR_CYAN, curses.COLOR_MAGENTA), 1):
        curses.init_pair(i, c, -1)
    scr.timeout(150)
    offset = frame = 0
    while True:
        frame += 1
        offset = draw(scr, board, offset, frame)
        try:
            ch = scr.getch()
        except KeyboardInterrupt:
            if board.stop.is_set():
                raise
            board.stop.set()
            continue
        if board.done and ch != -1 and ch != curses.KEY_RESIZE:
            return
        if ch in (ord("q"), ord("Q")):
            board.stop.set()
        elif ch == curses.KEY_UP:
            offset -= 1
        elif ch == curses.KEY_DOWN:
            offset += 1


# -- Summary / main ------------------------------------------------------------
def summary(board: Board, log_path: Path) -> None:
    tty = sys.stdout.isatty()
    c = (lambda code: f"\033[{code}m" if tty else "")
    ansi = {1: "32", 2: "31", 3: "33", 4: "36", 5: "35"}
    print(f"\n{c('1')}{board.phase} in {fmt(time.time() - board.t0)}{c('0')}")
    for r in board.rows:
        col = c(ansi[PAIR[r.status]]) if r.status in PAIR else c("2")
        print(f"  {r.name:<10} {r.pve:<9} {r.kernel:<17} {col}{r.status:<14}{c('0')} "
              f"{c('2')}{r.detail}{c('0')}")
    for line in board.log:
        print(f"  {c('2')}! {line}{c('0')}")
    print(f"{c('2')}Full log: {log_path}{c('0')}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Rolling dist-upgrade of a Proxmox cluster.")
    ap.add_argument("--only", nargs="+", metavar="NODE", help="only these node names")
    ap.add_argument("--check-only", action="store_true", help="refresh and report, change nothing")
    args = ap.parse_args()

    missing = [k for k in ("PVE_NODES", "SSH_USER", "SSH_KEY") if not env(k)]
    if missing:
        print(f"Missing {', '.join(missing)} in {ENV_FILE} - copy .env.example to .env and fill it in")
        return 1
    if not SSH_KEY.is_file():
        print(f"SSH key not found: {SSH_KEY}")
        return 1
    log_path = LOG_DIR / f"pve-update-{dt.datetime.now():%Y%m%d-%H%M%S}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    Log.fh = log_path.open("w")

    board = Board(plain=not (sys.stdout.isatty() and sys.stdin.isatty()))
    try:
        if board.plain:
            work(board, args)
        else:
            locale.setlocale(locale.LC_ALL, "")
            threading.Thread(target=work, args=(board, args), daemon=True).start()
            curses.wrapper(tui, board)
    except KeyboardInterrupt:
        board.phase = "Aborted"
        summary(board, log_path)
        return 130
    summary(board, log_path)
    return 1 if board.error or any(r.status == "failed" for r in board.rows) else 0


if __name__ == "__main__":
    sys.exit(main())
