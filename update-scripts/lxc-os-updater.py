#!/usr/bin/env python3
"""
lxc-os-updater.py - apt/apk upgrade every running LXC in the Proxmox cluster, with a live TUI.

Goes through the Proxmox nodes by IP (`pct exec` on whichever node hosts the CT),
so it keeps working while DNS/reverse-proxy CTs restart and DNS is down.

  1. Discover - every running LXC, its OS and package manager (apt or apk)
  2. Refresh  - `apt-get update` + download upgrades, in parallel, while DNS is up
  3. Upgrade  - install from the local cache (no network needed); LXC_RUN_LAST ones last.
                CTs with Docker: waits until the same number of containers are back up
                (a docker-ce upgrade restarts dockerd and every container in it)
  4. Reboot   - only with --reboot, only CTs flagging /var/run/reboot-required, one at a time

Held packages (`apt-mark hold`, or `pkg=version` in Alpine's /etc/apk/world) are highlighted.

Settings come from .env next to this script (copy .env.example and fill it in).

Keys: up/down scroll, q = stop after the current step, Ctrl-C twice = abort.

Usage:
  ./lxc-os-updater.py                 # refresh + upgrade, report CTs that want a reboot
  ./lxc-os-updater.py --check-only    # refresh and list pending upgrades, change nothing
  ./lxc-os-updater.py --reboot        # also reboot CTs that need it
  ./lxc-os-updater.py --only jellyfin immich
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
from collections import Counter, deque
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


NODES = env_list("PVE_NODES")                    # any reachable one is used
SSH_USER = env("SSH_USER")
SSH_KEY = Path(env("SSH_KEY")).expanduser()
SUDO = env("SSH_SUDO", "" if SSH_USER == "root" else "sudo -n")
SSH_EXTRA = ((["-p", env("SSH_PORT")] if env("SSH_PORT") else [])
             + (["-o", f"StrictHostKeyChecking={env('SSH_STRICT_HOST_KEY_CHECKING')}"]
                if env("SSH_STRICT_HOST_KEY_CHECKING") else []))
LOG_DIR = Path(env("UPDATER_LOG_DIR") or Path(__file__).resolve().parent / "logs").expanduser()

EXCLUDE = set(env_list("LXC_OS_EXCLUDE"))        # CT names to never touch
RUN_LAST = tuple(x.lower() for x in env_list("LXC_RUN_LAST"))  # substring of CT name, upgraded/rebooted last in this order
PARALLEL = env_int("LXC_OS_PARALLEL", 4)         # concurrent refresh/download jobs
UPGRADE_PARALLEL = env_int("LXC_OS_UPGRADE_PARALLEL", 3)  # concurrent installs (the RUN_LAST ones go one by one)
DISCOVER_PARALLEL = env_int("LXC_OS_DISCOVER_PARALLEL", 2)

DISCOVER_TIMEOUT = env_int("LXC_OS_DISCOVER_TIMEOUT", 90)
REFRESH_TIMEOUT = env_int("LXC_OS_REFRESH_TIMEOUT", 15 * 60)
UPGRADE_TIMEOUT = env_int("LXC_OS_UPGRADE_TIMEOUT", 30 * 60)
SETTLE_TIMEOUT = env_int("LXC_OS_SETTLE_TIMEOUT", 5 * 60)  # docker containers back / CT back after reboot
POLL = 5
LOG_LINES = 6


# -- Logging / helpers ---------------------------------------------------------
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
            "-o", "ControlPath=~/.ssh/cm-os-upd-%r@%h:%p",
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
            Log.write(f"[{host}] ssh connection failed, retrying: {r.stderr.strip()}")
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


def pvesh(host: str, path: str, *args: str):
    r = ssh(host, sudo(f"pvesh get {path} {' '.join(args)} --output-format json"),
            log_output=False)
    return json.loads(r.stdout or "null")


def matches(name: str, patterns) -> bool:
    return any(p.lower() in name.lower() for p in patterns)


# -- Remote scripts ------------------------------------------------------------
DETECT = r'''
. /etc/os-release 2>/dev/null
if command -v apt-get >/dev/null 2>&1; then pm=apt
elif command -v apk >/dev/null 2>&1; then pm=apk
else pm=none; fi
command -v docker >/dev/null 2>&1 && d=1 || d=0
echo "$pm|${ID:-?} ${VERSION_ID:-}|$d"
'''

APT_ENV = "export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a; "
APT_OPTS = ("-o DPkg::Lock::Timeout=300 -o Dpkg::Options::=--force-confdef "
            "-o Dpkg::Options::=--force-confold")

# output: upgradable packages / @@@ / held packages / @@@ / REBOOT if flagged
REFRESH = {
    "apt": APT_ENV + r'''
apt-get -qq -o DPkg::Lock::Timeout=300 update >/dev/null || exit 1
apt-get -s -o Debug::NoLocking=1 dist-upgrade | awk '/^Inst /{print $2}'
echo @@@; apt-mark showhold
echo @@@; [ -f /var/run/reboot-required ] && echo REBOOT; true
''',
    "apk": r'''
apk update -q >/dev/null || exit 1
apk upgrade -s 2>/dev/null | sed -n 's/.*Upgrading \([^ ]*\).*/\1/p'
echo @@@; grep '=' /etc/apk/world 2>/dev/null
echo @@@; true
''',
}
DOWNLOAD = {"apt": APT_ENV + f"apt-get -qq -d -y {APT_OPTS} dist-upgrade"}   # apk: no pre-download
UPGRADE = {
    "apt": APT_ENV + f"apt-get -y -q {APT_OPTS} dist-upgrade && apt-get -y -q autoremove "
                     "&& apt-get -q clean; [ -f /var/run/reboot-required ] && echo REBOOT; true",
    "apk": "apk upgrade -q --no-progress && rm -rf /var/cache/apk/*; true",
}


# -- Model ---------------------------------------------------------------------
@dataclass
class Host:
    vmid: int
    name: str
    node: str
    ip: str

    def run(self, inner: str, timeout: int = 60, check: bool = True,
            log_output: bool = True) -> subprocess.CompletedProcess:
        return ssh(self.ip, sudo(f"pct exec {self.vmid} -- sh -c {shlex.quote(inner)}"),
                   timeout=timeout, check=check, log_output=log_output)


@dataclass
class Row:
    host: Host
    os: str = ""
    pm: str = ""
    docker: bool = False
    status: str = "pending"
    detail: str = ""
    pkgs: list[str] = field(default_factory=list)
    held: list[str] = field(default_factory=list)
    reboot: bool = False
    since: float = field(default_factory=time.time)


class Board:
    def __init__(self, plain: bool):
        self.rows: list[Row] = []
        self.log: deque[str] = deque(maxlen=200)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.plain = plain
        self.phase = "Starting"
        self.done = False
        self.error = ""
        self.t0 = time.time()

    def set(self, row: Row, status: str | None = None, detail: str | None = None) -> None:
        with self.lock:
            changed = bool(status) and status != row.status
            if changed:
                row.status, row.since = status, time.time()
            if detail is not None:
                row.detail = detail
        if changed:
            line = f"{row.host.name}: {row.status}" + (f" - {row.detail}" if row.detail else "")
            Log.write(f"STATE {line}")
            if self.plain:
                print(f"  {line}", flush=True)

    def note(self, msg: str) -> None:
        with self.lock:
            self.log.append(msg)
        Log.write(f"NOTE  {msg}")
        if self.plain:
            print(f"  ! {msg}", flush=True)


def rank(r: Row) -> int:
    return next((i + 1 for i, k in enumerate(RUN_LAST) if k in r.host.name.lower()), 0)


def pkg_summary(pkgs: list[str]) -> str:
    return f"{len(pkgs)} pkg(s): {', '.join(pkgs[:4])}{' …' if len(pkgs) > 4 else ''}"


# -- Phases --------------------------------------------------------------------
def discover(board: Board, only: list[str] | None) -> None:
    board.phase = "Discovering"
    entry = next((ip for ip in NODES if ssh(ip, "true", timeout=10, check=False).returncode == 0),
                 None)
    if not entry:
        raise StepError("couldn't reach any Proxmox node")
    board.note(f"Using Proxmox node {entry}")
    node_ip = {s["name"]: s.get("ip") for s in (pvesh(entry, "/cluster/status") or [])
               if s.get("type") == "node" and s.get("online")}
    rows = []
    for ct in sorted((r for r in (pvesh(entry, "/cluster/resources", "--type", "vm") or [])
                      if r.get("type") == "lxc" and r.get("status") == "running"),
                     key=lambda r: r["vmid"]):
        name = ct.get("name") or str(ct["vmid"])
        if name in EXCLUDE or (only and not matches(name, only)):
            continue
        ip = node_ip.get(ct.get("node"))
        if not ip:
            board.note(f"{name}: no IP for node {ct.get('node')} - skipped")
            continue
        rows.append(Row(Host(ct["vmid"], name, ct["node"], ip)))
    with board.lock:
        board.rows = rows

    def probe(row: Row) -> None:
        board.set(row, "checking")
        for _ in (1, 2):
            r = row.host.run(DETECT, timeout=DISCOVER_TIMEOUT, check=False, log_output=False)
            if r.returncode != 124:
                break
        if r.returncode != 0 or "|" not in r.stdout:
            board.set(row, "failed", (r.stderr.strip().splitlines() or ["detect failed"])[-1])
            return
        pm, row.os, d = r.stdout.strip().splitlines()[-1].split("|")
        row.os, row.docker = row.os.strip(), d == "1"
        if pm not in REFRESH:
            board.set(row, "unsupported", "no apt or apk")
            return
        row.pm = pm
        board.set(row, "pending", "")

    with ThreadPoolExecutor(DISCOVER_PARALLEL) as ex:
        list(ex.map(probe, rows))


def refresh_phase(board: Board, rows: list[Row], download: bool) -> None:
    board.phase = "Refreshing package lists"

    def refresh(row: Row) -> None:
        if board.stop.is_set():
            board.set(row, "skipped", "stopped by user")
            return
        step = "refresh"
        try:
            board.set(row, "refreshing", "")
            r = row.host.run(REFRESH[row.pm], timeout=REFRESH_TIMEOUT, log_output=False)
            pkgs, _, rest = r.stdout.partition("@@@")
            held, _, flag = rest.partition("@@@")
            row.pkgs = [p for p in pkgs.split() if p]
            row.held = [h for h in held.split() if h]
            row.reboot = "REBOOT" in flag
            if not row.pkgs:
                board.set(row, "reboot needed" if row.reboot else "up to date", "")
                return
            if download and row.pm in DOWNLOAD:
                step = "download"
                board.set(row, "downloading", pkg_summary(row.pkgs))
                row.host.run(DOWNLOAD[row.pm], timeout=REFRESH_TIMEOUT)
            board.set(row, "ready", pkg_summary(row.pkgs))
        except StepError as e:
            board.set(row, "failed", f"{step}: {e}")

    with ThreadPoolExecutor(PARALLEL) as ex:
        list(ex.map(refresh, rows))


def docker_up(row: Row) -> int:
    r = row.host.run("docker ps --format '{{.Status}}'", log_output=False)
    return sum(1 for s in r.stdout.splitlines()
               if s.startswith("Up") and "health: starting" not in s and "unhealthy" not in s)


def settle(board: Board, row: Row, want: int) -> None:
    """Wait until the CT answers and (if it runs Docker) `want` containers are up and healthy."""
    deadline, streak = time.time() + SETTLE_TIMEOUT, 0
    while True:
        try:
            if row.docker:
                n = docker_up(row)
                done, detail = n >= want, f"docker {n}/{want} up"
            else:
                row.host.run("true", timeout=15, log_output=False)
                done, detail = True, "responding"
        except StepError:
            done, detail = False, "waiting for CT"
        board.set(row, detail=detail)
        streak = streak + 1 if done else 0
        if streak >= 2:
            return
        if time.time() > deadline:
            raise StepError(f"not back after {fmt(SETTLE_TIMEOUT)} ({detail})")
        time.sleep(POLL)


def upgrade_one(board: Board, row: Row, reboot_later: bool) -> None:
    if board.stop.is_set():
        board.set(row, "ready", "stopped by user - not upgraded")
        return
    if rank(row):
        board.note(f"Upgrading {row.host.name} - DNS may drop briefly (nodes are hit by IP)")
    step = "upgrade"
    try:
        before = docker_up(row) if row.docker else 0
        board.set(row, "upgrading", pkg_summary(row.pkgs))
        r = row.host.run(UPGRADE[row.pm], timeout=UPGRADE_TIMEOUT)
        row.reboot = row.reboot or "REBOOT" in r.stdout
        step = "after upgrade"
        board.set(row, "waiting", "")
        settle(board, row, before)
        done = f"{len(row.pkgs)} pkg(s) upgraded" + (f", docker {before}/{before} up" if row.docker else "")
        if row.reboot and not reboot_later:
            board.set(row, "reboot needed", done + " - rerun with --reboot")
        else:
            board.set(row, "upgraded", done)
    except StepError as e:
        board.set(row, "failed", f"{step}: {e}")


def upgrade_phase(board: Board, rows: list[Row], reboot_later: bool) -> None:
    board.phase = "Upgrading"
    todo = [r for r in rows if r.status == "ready"]
    for r in todo:
        board.set(r, "queued")
    with ThreadPoolExecutor(UPGRADE_PARALLEL) as ex:
        list(ex.map(lambda r: upgrade_one(board, r, reboot_later), [r for r in todo if not rank(r)]))
    for r in sorted((r for r in todo if rank(r)), key=rank):
        upgrade_one(board, r, reboot_later)


def reboot_phase(board: Board, rows: list[Row]) -> None:
    board.phase = "Rebooting"
    for row in sorted((r for r in rows if r.reboot and r.status in ("upgraded", "reboot needed")),
                      key=rank):
        if board.stop.is_set():
            board.set(row, "reboot needed", "stopped by user - not rebooted")
            continue
        try:
            want = docker_up(row) if row.docker else 0
            board.set(row, "rebooting", "")
            ssh(row.host.ip, sudo(f"pct reboot {row.host.vmid}"), timeout=300)
            settle(board, row, want)
            board.set(row, "upgraded", "rebooted" + (f", docker {want}/{want} up" if row.docker else ""))
        except StepError as e:
            board.set(row, "failed", f"reboot: {e}")


def work(board: Board, args) -> None:
    try:
        discover(board, args.only)
        rows = [r for r in board.rows if r.status == "pending"]
        if rows:
            refresh_phase(board, rows, download=not args.check_only)
            if not args.check_only:
                upgrade_phase(board, rows, args.reboot)
                if args.reboot:
                    reboot_phase(board, rows)
        board.phase = "Stopped" if board.stop.is_set() else "Done"
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
ACTIVE = {"checking", "refreshing", "downloading", "upgrading", "waiting", "rebooting"}
PAIR = {"checking": 4, "refreshing": 4, "downloading": 4, "up to date": 1, "ready": 3,
        "queued": 3, "upgrading": 5, "waiting": 5, "rebooting": 5, "upgraded": 1,
        "reboot needed": 3, "failed": 2}
BOLD_STATES = {"upgraded", "failed", "ready", "reboot needed"}


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


def counts_line(rows: list[Row]) -> str:
    c = Counter(r.status for r in rows)
    held = sum(1 for r in rows if r.held)
    pkgs = sum(len(r.pkgs) for r in rows)
    parts = [f"{n} {s}" for s, n in c.items() if s in PAIR]
    return " · ".join(parts + ([f"{pkgs} packages"] if pkgs else []) + ([f"{held} with holds"] if held else []))


def draw(scr, board: Board, offset: int, frame: int) -> int:
    scr.erase()
    h, w = scr.getmaxyx()
    with board.lock:
        rows, logs = list(board.rows), list(board.log)
    put(scr, 0, 0, f" LXC OS updater │ {board.phase} │ {fmt(time.time() - board.t0)} ".ljust(w),
        curses.A_REVERSE | curses.A_BOLD)
    put(scr, 1, 1, counts_line(rows), curses.A_DIM)
    put(scr, 2, 0, f"{'CT':>5}  {'LXC':<18} {'OS':<12} {'STATUS':<16} {'HELD':<22} DETAIL",
        curses.A_UNDERLINE | curses.A_BOLD)

    log_h = min(LOG_LINES, max(0, h - 10))
    sep = h - log_h - 2
    body_h = max(0, sep - 3)
    offset = max(0, min(offset, len(rows) - body_h))
    for i, r in enumerate(rows[offset:offset + body_h]):
        y = 3 + i
        x = put(scr, y, 0, f"{r.host.vmid:>5}  ")
        x = put(scr, y, x, f"{r.host.name:<18.18} ",
                curses.color_pair(5) | curses.A_BOLD if r.held else 0)
        x = put(scr, y, x, f"{r.os:<12.12} ", curses.A_DIM)
        st = (SPIN[frame % len(SPIN)] if r.status in ACTIVE else " ") + " " + r.status
        x = put(scr, y, x, f"{st:<16.16} ", status_attr(r.status))
        x = put(scr, y, x, f"{', '.join(r.held):<22.22} ", curses.color_pair(5))
        extra = f" ({fmt(time.time() - r.since)})" if r.status in ACTIVE else ""
        put(scr, y, x, r.detail + extra, curses.A_DIM)
    if len(rows) > body_h > 0:
        put(scr, sep, w - 22, f" rows {offset + 1}-{min(len(rows), offset + body_h)}/{len(rows)} ",
            curses.A_DIM)

    put(scr, sep, 0, "─ log " + "─" * (w - 30), curses.A_DIM)
    for i, line in enumerate(logs[-log_h:] if log_h else []):
        put(scr, sep + 1 + i, 1, line, curses.A_DIM)
    if board.done:
        foot, attr = " done - press any key to exit", curses.A_REVERSE
    elif board.stop.is_set():
        foot, attr = " stopping after the current step… (Ctrl-C again to abort)", curses.color_pair(3)
    else:
        foot, attr = " ↑/↓ scroll   q stop after current step", curses.A_DIM
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
        elif ch == curses.KEY_PPAGE:
            offset -= 10
        elif ch == curses.KEY_NPAGE:
            offset += 10


# -- Summary / main ------------------------------------------------------------
def summary(board: Board, log_path: Path) -> None:
    tty = sys.stdout.isatty()
    c = (lambda code: f"\033[{code}m" if tty else "")
    ansi = {1: "32", 2: "31", 3: "33", 4: "36", 5: "35"}
    print(f"\n{c('1')}{board.phase} in {fmt(time.time() - board.t0)} - {counts_line(board.rows)}{c('0')}")
    for r in board.rows:
        col = c(ansi[PAIR[r.status]]) if r.status in PAIR else c("2")
        held = f" {c('35')}held: {', '.join(r.held)}{c('0')}" if r.held else ""
        print(f"{r.host.vmid:>5}  {r.host.name:<18.18} {r.os:<12.12} "
              f"{col}{r.status:<14}{c('0')} {c('2')}{r.detail}{c('0')}{held}")
    for line in board.log:
        print(f"  {c('2')}! {line}{c('0')}")
    print(f"{c('2')}Full log: {log_path}{c('0')}")


def main() -> int:
    ap = argparse.ArgumentParser(description="apt/apk upgrade every running LXC.")
    ap.add_argument("--only", nargs="+", metavar="NAME", help="only CTs whose name contains one of these")
    ap.add_argument("--check-only", action="store_true", help="refresh and report, don't upgrade")
    ap.add_argument("--reboot", action="store_true", help="reboot CTs that flag reboot-required")
    args = ap.parse_args()

    missing = [k for k in ("PVE_NODES", "SSH_USER", "SSH_KEY") if not env(k)]
    if missing:
        print(f"Missing {', '.join(missing)} in {ENV_FILE} - copy .env.example to .env and fill it in")
        return 1
    if not SSH_KEY.is_file():
        print(f"SSH key not found: {SSH_KEY}")
        return 1
    log_path = LOG_DIR / f"os-update-{dt.datetime.now():%Y%m%d-%H%M%S}.log"
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
