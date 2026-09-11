#!/usr/bin/env python3
"""
gemini-manager - kontrol alat untuk gemini-web2api.

Mengelola:
  - status / start / stop / restart service
  - tambah / hapus / lihat akun Google (dengan rotasi round-robin)
  - test rotasi akun
  - install / uninstall systemd service

Contoh:
  gemini-manager status
  gemini-manager add-account akun-2 /path/cookie.txt --auth-user 1
  gemini-manager add-account akun-2 --cookie-json '[{...}]'
  gemini-manager remove-account akun-2
  gemini-manager start | stop | restart
  gemini-manager test 4
"""
import argparse
from argparse import Namespace
import getpass
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.realpath(__file__))
CONFIG_FILE = os.path.join(BASE, "config.json")
MAIN_SCRIPT = os.path.join(BASE, "gemini_web2api.py")
PID_FILE = os.path.join(BASE, "gemini-web2api.pid")
LOG_FILE = os.path.join(BASE, "nohup.out")
SERVICE_UNIT = os.path.join(BASE, "gemini-web2api.service")
VENV_PY = os.path.join(BASE, "venv", "bin", "python3")

PORT = 8081


# ─── config helpers ───────────────────────────────────────────────────────────

def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {}
    with open(CONFIG_FILE) as f:
        return json.load(f)


def save_config(cfg):
    cfg.setdefault("accounts", [])
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")


def cookie_expiry(cookie_file):
    """Return (expiry_ts, expires_in_days) for a cookie file, or (None, None)."""
    if not cookie_file or not os.path.exists(cookie_file):
        return None, None
    try:
        with open(cookie_file) as f:
            content = f.read().strip()
        if content.startswith("["):
            cookies = json.loads(content)
            dates = [c["expirationDate"] for c in cookies if c.get("expirationDate")]
            if not dates:
                return None, None
            exp = max(dates)
            return exp, (exp - time.time()) / 86400
    except Exception:
        pass
    return None, None


# ─── process helpers ──────────────────────────────────────────────────────────

def find_pid():
    if os.path.exists(PID_FILE):
        try:
            pid = int(open(PID_FILE).read().strip())
            if pid > 0 and os.path.isdir(f"/proc/{pid}"):
                return pid
        except (ValueError, OSError):
            pass
    try:
        out = subprocess.run(["pgrep", "-f", "gemini_web2api.py"],
                             capture_output=True, text=True).stdout.strip()
        if out:
            for line in out.splitlines():
                p = int(line)
                try:
                    cmd = open(f"/proc/{p}/cmdline").read()
                    if "gemini_web2api.py" in cmd:
                        return p
                except OSError:
                    continue
    except Exception:
        pass
    return None


def is_running():
    return find_pid() is not None


def cmd_start(args):
    if is_running():
        print(f"  Sudah jalan (PID {find_pid()}) di port {PORT}.")
        return
    py = VENV_PY if os.path.exists(VENV_PY) else sys.executable
    cmd = [py, MAIN_SCRIPT, "--config", CONFIG_FILE]
    devnull = open(os.devnull, "w")
    logf = open(LOG_FILE, "a") if not getattr(args, "no_log", False) else devnull
    try:
        proc = subprocess.Popen(cmd, cwd=BASE, stdin=devnull,
                                stdout=logf, stderr=logf, start_new_session=True)
        with open(PID_FILE, "w") as f:
            f.write(str(proc.pid))
        print(f"  Started PID {proc.pid} di http://0.0.0.0:{PORT}")
        print(f"  Log: {LOG_FILE}")
        time.sleep(1.5)
        if not os.path.isdir(f"/proc/{proc.pid}"):
            print("  WARNING: proses langsung berhenti, cek log di atas.")
    finally:
        if logf is not devnull:
            logf.close()
        devnull.close()


def cmd_stop(args):
    pid = find_pid()
    if not pid:
        print("  Tidak ada proses yang jalan.")
        return
    os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        if not os.path.isdir(f"/proc/{pid}"):
            break
        time.sleep(0.1)
    else:
        os.kill(pid, signal.SIGKILL)
    if os.path.exists(PID_FILE):
        os.remove(PID_FILE)
    print(f"  Stopped PID {pid}.")


def cmd_restart(args):
    cmd_stop(args)
    time.sleep(0.5)
    cmd_start(args)


def cmd_status(args):
    pid = find_pid()
    cfg = load_config()
    print("gemini-web2api status")
    print(f"  PID:        {pid if pid else '-'}  Port: {PORT}")
    print(f"  Model:      {cfg.get('default_model', '-')}")
    print(f"  API keys:   {len(cfg.get('api_keys', []))} configured")
    accounts = cfg.get("accounts", [])
    if accounts:
        print(f"  Accounts:   {len(accounts)}x rotasi round-robin")
    else:
        print(f"  Accounts:   none (pakai cookie_file global / anonymous)")
    # uptime
    if pid:
        try:
            with open(f"/proc/{pid}/stat") as f:
                fields = f.read().split()
                start_ticks = int(fields[22])
            with open("/proc/uptime") as f:
                boot_sec = float(f.read().split()[0])
            uptime = max(0, boot_sec - start_ticks / os.sysconf("SC_CLK_TCK"))
            if uptime >= 3600:
                print(f"  Uptime:     {int(uptime // 3600)} jam {int(uptime % 3600 // 60)} menit")
            else:
                print(f"  Uptime:     {int(uptime // 60)} menit")
        except Exception:
            pass
    print(f"  HTTP:       http://localhost:{PORT}/v1")
    # quick health check
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=2):
            print("  Health:     OK (port listening)")
    except OSError:
        print("  Health:     DOWN (port tidak open)")


def cmd_list(args):
    cfg = load_config()
    accounts = cfg.get("accounts", [])
    if not accounts:
        print("  Belum ada akun terdaftar.")
        return
    print(f"{'#':<3} {'name':<12} {'auth_user':<10} {'cookie':<42} {'expiry'}")
    for i, acc in enumerate(accounts, 1):
        cf = acc.get("cookie_file", "")
        au = acc.get("auth_user", "")
        _, days = cookie_expiry(cf)
        exp = f"{days:.0f} hari" if days is not None else "-"
        print(f"{i:<3} {acc.get('name','?'):<12} {str(au):<10} {cf:<42} {exp}")


def cmd_add(args):
    cfg = load_config()
    accounts = cfg.setdefault("accounts", [])

    # resolve cookie source
    if args.cookie_file:
        src = os.path.expanduser(args.cookie_file)
        if not os.path.exists(src):
            alt = os.path.join(BASE, src)
            if os.path.exists(alt):
                src = alt
            else:
                print(f"ERROR: file tidak ada: {src}")
                sys.exit(1)
        with open(src) as f:
            content = f.read().strip()
        if not content:
            print("ERROR: cookie file kosong.")
            sys.exit(1)
    elif args.cookie_json:
        content = args.cookie_json
    else:
        print("ERROR: berikan --cookie-file PATH atau --cookie-json '...'")
        sys.exit(1)

    # validate content is a usable cookie (string or JSON array/object)
    stripped = content.lstrip()
    if stripped.startswith(("[", "{")):
        try:
            json.loads(content)
        except json.JSONDecodeError:
            print("ERROR: cookie JSON tidak valid.")
            sys.exit(1)

    # store cookie in project dir
    safe = re.sub(r"[^A-Za-z0-9_.-]", "-", args.name)
    dest = os.path.join(BASE, f"cookie-{safe}.txt")
    with open(dest, "w") as f:
        f.write(content)
    if args.verbose:
        print(f"  Cookie disimpan ke: {dest}")

    # check duplicate name
    if any(a.get("name") == args.name for a in accounts):
        print(f"ERROR: akun '{args.name}' sudah ada. Pakai remove-account dulu.")
        sys.exit(1)

    accounts.append({
        "name": args.name,
        "cookie_file": dest,
        "auth_user": args.auth_user,
        "xsrf_token": args.xsrf_token,
    })
    save_config(cfg)

    if args.auth_user:
        au = f"/u/{args.auth_user}"
    else:
        au = "(default)"
    print(f"  Akun '{args.name}' ditambahkan (auth_user {au}).")
    cmd_list(args)
    _reload_hint(args)


def cmd_remove(args):
    cfg = load_config()
    accounts = cfg.get("accounts", [])
    match = [a for a in accounts if a.get("name") == args.name]
    if not match:
        print(f"ERROR: akun '{args.name}' tidak ditemukan.")
        sys.exit(1)
    acc = match[0]
    accounts.remove(acc)
    save_config(cfg)
    print(f"  Akun '{args.name}' dihapus dari config.")
    if args.delete_cookie and acc.get("cookie_file"):
        cf = acc["cookie_file"]
        if os.path.exists(cf) and os.path.realpath(cf).startswith(BASE):
            os.remove(cf)
            print(f"  Cookie file dihapus: {cf}")
    cmd_list(args)
    _reload_hint(args)


def _reload_hint(args):
    if not is_running():
        print("  Jalankan: gemini-manager start")
    else:
        print("  Anti-reload aktif: perubahan akun langsung berlaku (tanpa restart).")


def cmd_test(args):
    if not is_running():
        print("ERROR: service tidak jalan. Jalankan dulu: gemini-manager start")
        sys.exit(1)
    import urllib.request
    cfg = load_config()
    api_key = args.api_key or (cfg.get("api_keys") or [None])[0] or "x"
    url = f"http://127.0.0.1:{PORT}/v1/chat/completions"
    n = max(1, args.count)
    ok = 0
    for i in range(n):
        body = json.dumps({
            "model": CONFIG_MODEL or "gemini-3.6-flash",
            "messages": [{"role": "user", "content": "Balas: ok"}],
        }).encode()
        req = urllib.request.Request(url, data=body, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        })
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())
            text = data["choices"][0]["message"]["content"]
            ok += 1
            print(f"  [{i+1}/{n}] OK -> {text[:40]!r}")
        except Exception as e:
            print(f"  [{i+1}/{n}] ERROR: {e}")
    print(f"  Sukses {ok}/{n}")


def service_unit_content():
    user = getpass.getuser()
    py = VENV_PY if os.path.exists(VENV_PY) else sys.executable
    return f"""[Unit]
Description=Gemini Web2API
After=network.target

[Service]
Type=simple
WorkingDirectory={BASE}
ExecStart={py} {MAIN_SCRIPT} --config {CONFIG_FILE}
Restart=always
RestartSec=3
User={user}
Group={user}
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
"""


def cmd_install_service(args):
    unit = service_unit_content()
    tmp = os.path.join(BASE, "gemini-web2api.service")
    with open(tmp, "w") as f:
        f.write(unit)
    subprocess.run(["sudo", "cp", tmp, "/etc/systemd/system/gemini-web2api.service"])
    subprocess.run(["sudo", "systemctl", "daemon-reload"])
    if args.enable:
        subprocess.run(["sudo", "systemctl", "enable", "gemini-web2api"])
    if args.start:
        subprocess.run(["sudo", "systemctl", "start", "gemini-web2api"])
    print("  Service systemd terpasang.")


def cmd_uninstall_service(args):
    subprocess.run(["sudo", "systemctl", "stop", "gemini-web2api"], capture_output=True)
    subprocess.run(["sudo", "systemctl", "disable", "gemini-web2api"], capture_output=True)
    subprocess.run(["sudo", "rm", "-f", "/etc/systemd/system/gemini-web2api.service"])
    subprocess.run(["sudo", "systemctl", "daemon-reload"])
    print("  Service systemd dihapus.")


# ─── Interactive menu & monitor ───────────────────────────────────────────────

def _input(prompt):
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise SystemExit(0)


def read_cookie_paste(prompt):
    """Read multi-line pasted cookie until an empty line."""
    print(f"  {prompt}")
    print("  (paste lalu tekan Enter 2x / baris kosong untuk selesai)")
    print("  =====================================================================")
    lines = []
    while True:
        try:
            line = input()
        except (EOFError, KeyboardInterrupt):
            break
        if not line.strip():
            break
        lines.append(line)
    print("  =====================================================================")
    content = "\n".join(lines).strip()
    if not content:
        print("  ERROR: kosong, tidak jadi apa-apa.")
        return None
    stripped = content.lstrip()
    if stripped.startswith(("[", "{")):
        try:
            json.loads(content)
        except json.JSONDecodeError:
            print("  ERROR: JSON tidak valid, gagal menyimpan.")
            return None
    return content


def _add_account_interactive(args):
    name = _input("  Nama akun (mis. akun-1): ")
    if not name:
        return
    flex = input("  Auth user index Enter=default, 1=akun2, 2=akun3: ").strip()
    auth_user = int(flex) if flex else None
    source = _input("  Cookie dari (1) paste manual / (2) file: ")
    if source == "2":
        path = _input("  Path file cookie: ")
        if not path:
            return
        path = os.path.expanduser(path)
        if not os.path.isabs(path):
            alt = os.path.join(BASE, path)
            if os.path.exists(alt):
                path = alt
        if not os.path.exists(path):
            print(f"  ERROR: file tidak ada: {path}")
            return
        with open(path) as f:
            content = f.read().strip()
        if not content:
            print("  ERROR: file kosong.")
            return
    else:
        content = read_cookie_paste("TEMPEL cookie akun di sini")
        if content is None:
            return
    args.cookie_json = content
    args.cookie_file = None
    args.name = name
    args.auth_user = auth_user
    args.xsrf_token = None
    cmd_add(args)


def _remove_account_interactive(args):
    cfg = load_config()
    accounts = cfg.get("accounts", [])
    if not accounts:
        print("  Belum ada akun.")
        return
    cmd_list(args)
    name = _input("  Nama akun yang dihapus (atau kosong untuk batal): ")
    if not name:
        return
    do_del = _input("  Hapus juga file cookie-nya? (y/N): ").lower()
    args.name = name
    args.delete_cookie = do_del == "y"
    cmd_remove(args)


def cmd_monitor(args):
    """Live monitor: request log + akun yg dipakai tiap request."""
    try:
        print(f"  Monitor log: {LOG_FILE}  (Ctrl+C untuk keluar)")
        print("  ───────────────────────────────────────────────")
        counts = {}
        pos = os.path.getsize(LOG_FILE) if os.path.exists(LOG_FILE) else 0
        while True:
            time.sleep(args.interval)
            if not os.path.exists(LOG_FILE):
                continue
            size = os.path.getsize(LOG_FILE)
            if size > pos:
                with open(LOG_FILE) as f:
                    f.seek(pos)
                    for line in f:
                        m = re.search(r"Using account: (\S+)", line)
                        if m:
                            counts[m.group(1)] = counts.get(m.group(1), 0) + 1
                        s = line.rstrip("\n")
                        if s:
                            print(f"  {s}", flush=True)
                    pos = f.tell()
                if counts:
                    usage = "  ".join(f"{k}={v}" for k, v in sorted(counts.items()))
                    print(f"\n  [sedang aktif -> {usage}]\n", flush=True)
            if size < pos:  # log di-truncate/rotate
                pos = 0
    except KeyboardInterrupt:
        print("\n  Monitor berhenti.")


def cmd_menu(args):
    menu_items = [
        ("Status service & akun", cmd_status),
        ("Lihat daftar akun", cmd_list),
        ("Tambah akun (paste cookie)", _add_account_interactive),
        ("Hapus akun", _remove_account_interactive),
        ("Start service", cmd_start),
        ("Stop service", cmd_stop),
        ("Restart service", cmd_restart),
        ("Monitor request real-time", cmd_monitor),
        ("Test rotasi akun", cmd_test),
        ("Install/uninstall systemd service", cmd_service_menu),
        ("Keluar", None),
    ]
    while True:
        print()
        print("  ╔══════════════════════════════════════════════════╗")
        print("  ║   ✦   geminiwebapi  -  kontrol pusat Gemini  ✦   ║")
        print("  ╚══════════════════════════════════════════════════╝")
        pid = find_pid()
        status = f"PID {pid}" if pid else "mati"
        acc = _account_mgr_count()
        print(f"  [{'●' if pid else '○'} service {status}] [akun: {acc}]")
        print()
        for i, (label, _) in enumerate(menu_items, 1):
            print(f"  {i}. {label}")
        choice = _input(f"\n  Pilih (1-{len(menu_items)}): ")
        try:
            idx = int(choice)
        except ValueError:
            continue
        if idx < 1 or idx > len(menu_items):
            continue
        label, fn = menu_items[idx - 1]
        if fn is None:
            print("  Dadah!")
            return
        fn(Namespace(
            no_log=False, count=3, api_key=None, delete_cookie=False,
            interval=0.5, verbose=False, xsrf_token=None, cookie_file=None,
            cookie_json=None, name=None, auth_user=None, enable=False, start=False,
        ))


def cmd_service_menu(args):
    print()
    print("  1. Install systemd service (--enable --start)")
    print("  2. Uninstall systemd service")
    c = _input("  Pilih (1/2, kosong batal): ")
    if c == "1":
        cmd_install_service(Namespace(enable=True, start=True))
    elif c == "2":
        cmd_uninstall_service(Namespace())
    else:
        print("  Batal.")


def _account_mgr_count():
    cfg = load_config()
    return len(cfg.get("accounts", []))


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    global CONFIG_MODEL
    parser = argparse.ArgumentParser(prog="gemini-manager", description=__doc__)
    parser.add_argument("--verbose", action="store_true", help="tampilkan detail")
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("status", help="status service & akun")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("list-accounts", help="daftar akun")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("add-account", help="tambah akun Google")
    p.add_argument("name", help="nama akun, mis. akun-2")
    p.add_argument("--cookie-file", help="path ke file cookie")
    p.add_argument("--cookie-json", help="raw cookie (string atau JSON array/object)")
    p.add_argument("--auth-user", type=int, default=None, help="index akun Google (0/null=default, 1=akun kedua)")
    p.add_argument("--xsrf-token", default=None, help="token XSRF (opsional)")
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("remove-account", help="hapus akun Google")
    p.add_argument("name", help="nama akun")
    p.add_argument("--delete-cookie", action="store_true", help="hapus juga file cookie-nya")
    p.set_defaults(fn=cmd_remove)

    p = sub.add_parser("start", help="jalankan service")
    p.add_argument("--no-log", action="store_true", help="jangan tulis log ke file")
    p.set_defaults(fn=cmd_start)

    p = sub.add_parser("stop", help="stop service")
    p.set_defaults(fn=cmd_stop)

    p = sub.add_parser("restart", help="restart service")
    p.set_defaults(fn=cmd_restart)

    p = sub.add_parser("test", help="tes rotasi akun ke endpoint lokal")
    p.add_argument("--count", type=int, default=3, help="jumlah request (default 3)")
    p.add_argument("--api-key", default=None, help="API key (default ambil dari config)")
    p.set_defaults(fn=cmd_test)

    p = sub.add_parser("install-service", help="pasang unit systemd (butuh sudo)")
    p.add_argument("--enable", action="store_true", help="enable pada boot")
    p.add_argument("--start", action="store_true", help="langsung start")
    p.set_defaults(fn=cmd_install_service)

    p = sub.add_parser("uninstall-service", help="hapus unit systemd")
    p.set_defaults(fn=cmd_uninstall_service)

    p = sub.add_parser("menu", help="menu interaktif")
    p.set_defaults(fn=cmd_menu)

    p = sub.add_parser("monitor", help="monitor request real-time")
    p.add_argument("--interval", type=float, default=0.5, help="interval cek log (detik)")
    p.set_defaults(fn=cmd_monitor)

    args = parser.parse_args()
    global CONFIG_MODEL, PORT
    cfg = load_config()
    CONFIG_MODEL = cfg.get("default_model", "gemini-3.6-flash")
    if cfg.get("port"):
        PORT = cfg["port"]
    if args.cmd is None:
        args.fn = cmd_menu
    args.fn(args)


if __name__ == "__main__":
    main()