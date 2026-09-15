#!/usr/bin/env python3
"""
gemini-manager - kontrol alat untuk gemini-web2api.

Mengelola:
  - status / start / stop / restart service
  - tambah / hapus / lihat akun Google (dengan rotasi round-robin)
  - test rotasi akun
  - install / uninstall systemd service
  - tambah / hapus / login / chat akun Antigravity (round-robin antar akun)

Contoh:
  gemini-manager status
  gemini-manager add-account akun-2 /path/cookie.txt --auth-user 1
  gemini-manager add-account akun-2 --cookie-json '[{...}]'
  gemini-manager remove-account akun-2
  gemini-manager start | stop | restart
  gemini-manager test 4
  gemini-manager antigravity add agy-b
  gemini-manager antigravity login agy-b
  gemini-manager antigravity chat "halo"
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

# ─── antigravity (round-robin akun) ───────────────────────────────────────────

ANTI_DIR = os.path.join(os.path.expanduser("~"), ".antigravity-accounts")
ANTI_CONFIG = os.path.join(ANTI_DIR, "config.json")
ANTI_BIN_CANDIDATES = ("/home/z/.local/bin/agy", os.path.expanduser("~/.local/bin/agy"))


def agy_bin():
    import shutil
    for cand in ANTI_BIN_CANDIDATES:
        if os.path.exists(cand):
            return cand
    return shutil.which("agy") or "agy"


def anti_load():
    cfg = {"accounts": [], "index": 0}
    if os.path.exists(ANTI_CONFIG):
        try:
            with open(ANTI_CONFIG) as f:
                cfg.update(json.load(f))
        except (json.JSONDecodeError, OSError):
            pass
    cfg.setdefault("accounts", [])
    cfg.setdefault("index", 0)
    return cfg


def anti_save(cfg):
    os.makedirs(ANTI_DIR, exist_ok=True)
    tmp = ANTI_CONFIG + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, ANTI_CONFIG)


def anti_home(acc):
    return acc.get("home", os.path.expanduser("~"))


def anti_token(acc):
    return os.path.join(anti_home(acc), ".gemini", "antigravity-cli",
                        "antigravity-oauth-token")


def anti_pick(cfg):
    accs = cfg["accounts"]
    if not accs:
        return None
    idx = cfg.get("index", 0) or 0
    acc = accs[idx % len(accs)]
    cfg["index"] = (idx + 1) % len(accs)
    anti_save(cfg)
    return acc


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


def cmd_list_keys(args):
    cfg = load_config()
    keys = cfg.get("api_keys", [])
    if not keys:
        print("  Belum ada API key terdaftar.")
        return
    print(f"{'#':<3} {'api_key':<70} {'status'}")
    for i, key in enumerate(keys, 1):
        print(f"{i:<3} {key:<70} aktif")
    if args.verbose:
        print()
        print("  Path config: " + CONFIG_FILE)


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
                acc = resp.headers.get("X-Gemini-Account", "?")
            text = data["choices"][0]["message"]["content"]
            ok += 1
            print(f"  [{i+1}/{n}] {acc} -> {text[:40]!r}")
        except Exception as e:
            print(f"  [{i+1}/{n}] ERROR: {e}")
    print(f"  Sukses {ok}/{n}")


def _anti_find(cfg, name):
    for a in cfg["accounts"]:
        if a.get("name") == name:
            return a
    return None


def cmd_anti_list(args):
    cfg = anti_load()
    accs = cfg["accounts"]
    print(f"Antigravity round-robin ({len(accs)} akun, urutan berikutnya: "
          f"index {cfg.get('index', 0)} di {anti_bin_desc()})")
    if not accs:
        print("  Belum ada akun. Tambah: gemini-manager antigravity add NAMA")
        return
    for i, acc in enumerate(accs):
        name = acc.get("name", "?")
        token = os.path.exists(anti_token(acc))
        home = anti_home(acc)
        next_ = "=>" if i == cfg.get("index", 0) % len(accs) else "  "
        print(f"  {next_} {name:<16} token {'ADA' if token else 'BELUM'}  {home}")
    if args.verbose:
        print("  Config: " + ANTI_CONFIG)


def cmd_anti_add(args):
    cfg = anti_load()
    if _anti_find(cfg, args.name):
        print(f"ERROR: akun '{args.name}' sudah ada.")
        sys.exit(1)
    if args.home:
        home = os.path.realpath(os.path.expanduser(args.home))
        if not os.path.isdir(home):
            print(f"ERROR: folder HOME tidak ada: {home}")
            sys.exit(1)
    else:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "-", args.name)
        home = os.path.join(ANTI_DIR, safe)
        os.makedirs(os.path.join(home, ".gemini", "antigravity-cli"), exist_ok=True)
    cfg["accounts"].append({"name": args.name, "home": home})
    anti_save(cfg)
    print(f"  Akun '{args.name}' didaftarkan (HOME: {home}).")
    print("  Login sekali: gemini-manager antigravity login " + args.name)


def cmd_anti_remove(args):
    cfg = anti_load()
    acc = _anti_find(cfg, args.name)
    if not acc:
        print(f"ERROR: akun '{args.name}' tidak ditemukan.")
        sys.exit(1)
    cfg["accounts"].remove(acc)
    cfg.setdefault("index", 0)
    if cfg["accounts"]:
        cfg["index"] %= len(cfg["accounts"])
    else:
        cfg["index"] = 0
    anti_save(cfg)
    print(f"  Akun '{args.name}' dihapus dari rotasi.")
    if args.delete_home and not anti_home(acc).startswith(os.path.expanduser("~")):
        import shutil
        shutil.rmtree(anti_home(acc), ignore_errors=True)
        print(f"  Folder HOME dihapus: {anti_home(acc)}")


def cmd_anti_login(args):
    cfg = anti_load()
    acc = _anti_find(cfg, args.name) if args.name else None
    if args.name and not acc:
        print(f"ERROR: akun '{args.name}' tidak ada.")
        sys.exit(1)
    home = anti_home(acc) if acc else os.path.expanduser("~")
    os.makedirs(os.path.join(home, ".gemini"), exist_ok=True)
    name = acc.get("name", "(HOME utama)") if acc else "(HOME utama)"
    print("=" * 66)
    print("  LANGKAH LOGIN ANTIGRAVITY (akun: " + name + ")")
    print("  HOME: " + home)
    print("=" * 66)
    print("  1. URL login akan muncul di bawah (dimulai accounts.google.com).")
    print("  2. Salin & buka URL itu di browser.")
    print("  3. Login dengan akun Google yg mau dipakai ANTIGRAVITY.")
    print("=" * 66)
    env = dict(os.environ)
    env["HOME"] = home
    subprocess.run([agy_bin(), "-p", "ok"], env=env)


def cmd_anti_chat(args):
    cfg = anti_load()
    if not cfg["accounts"]:
        print("ERROR: belum ada akun antigravity. Tambah dulu: "
              "gemini-manager antigravity add NAMA")
        sys.exit(1)
    acc = _anti_find(cfg, args.account) if args.account else anti_pick(cfg)
    if not acc:
        print("ERROR: akun tidak ditemukan.")
        sys.exit(1)
    env = dict(os.environ)
    env["HOME"] = anti_home(acc)
    print(f"  [{acc['name']}] {anti_home(acc)}")
    subprocess.run([agy_bin(), "-p", args.prompt], env=env)


def cmd_antigravity(args):
    sub = args.anti_cmd
    handler = {
        "list": cmd_anti_list, "add": cmd_anti_add, "remove": cmd_anti_remove,
        "login": cmd_anti_login, "chat": cmd_anti_chat,
    }.get(sub)
    if not handler:
        print(__doc__)
        print("Sub-perintah antigravity: list | add | remove | login | chat")
        sys.exit(1)
    handler(args)


def cmd_anti_menu(args):
    menu = [
        ("Daftar akun", ("list",)),
        ("Tambah akun baru", ("add",)),
        ("Impor akun HOME yang sudah login", ("import-home",)),
        ("Login akun", ("login",)),
        ("Hapus akun", ("remove",)),
        ("Chat round-robin", ("chat",)),
        ("Kembali", None),
    ]
    cfg = anti_load()
    print(f"\n  [Antigravity] {len(cfg['accounts'])} akun terdaftar")
    for i, (label, _) in enumerate(menu, 1):
        print(f"  {i}. {label}")
    c = _input("  Pilih (1-6, kosong batal): ")
    try:
        idx = int(c) - 1
    except (ValueError, IndexError):
        return
    if idx < 0 or idx >= len(menu):
        return
    label, key = menu[idx]
    if key is None:
        return
    if key == ("list",):
        cmd_anti_list(Namespace(verbose=False))
    elif key == ("add",):
        name = _input("  Nama akun (mis. agy-b): ")
        if not name:
            return
        cmd_anti_add(Namespace(name=name, home=None))
        cmd_anti_login(Namespace(name=name))
    elif key == ("import-home",):
        name = _input("  Nama akun (mis. utama): ")
        home = _input("  Path HOME (kosong = /home/z): ") or os.path.expanduser("~")
        if not name:
            return
        cmd_anti_add(Namespace(name=name, home=home))
        if not os.path.exists(anti_token({"name": name, "home": home})):
            cmd_anti_login(Namespace(name=name))
        else:
            print("  Token sudah ada, tidak perlu login ulang.")
    elif key == ("login",):
        name = _input("  Nama akun (kosong = HOME utama): ")
        cmd_anti_login(Namespace(name=name or None))
    elif key == ("remove",):
        cmd_anti_list(Namespace(verbose=False))
        name = _input("  Nama akun yang dihapus: ")
        if not name:
            return
        cmd_anti_remove(Namespace(name=name, delete_home=False))
    elif key == ("chat",):
        prompt = _input("  Prompt (kosong batal): ")
        if not prompt:
            return
        cmd_anti_chat(Namespace(prompt=prompt, account=None))


def anti_bin_desc():
    return os.path.basename(agy_bin())


# ─── service unit ─────────────────────────────────────────────────────────────

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
        ("Lihat API key", cmd_list_keys),
        ("Tambah akun (paste cookie)", _add_account_interactive),
        ("Hapus akun", _remove_account_interactive),
        ("Start service", cmd_start),
        ("Stop service", cmd_stop),
        ("Restart service", cmd_restart),
        ("Monitor request real-time", cmd_monitor),
        ("Test rotasi akun", cmd_test),
        ("Antigravity (round-robin akun)", cmd_anti_menu),
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

    p = sub.add_parser("list-keys", help="daftar API key")
    p.set_defaults(fn=cmd_list_keys)

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

    p = sub.add_parser("antigravity", help="kelola akun Antigravity round-robin")
    anti = p.add_subparsers(dest="anti_cmd")

    q = anti.add_parser("list", help="daftar akun antigravity")
    q.set_defaults(anti_cmd="list")

    q = anti.add_parser("add", help="tambah akun antigravity")
    q.add_argument("name", help="nama akun, mis. agy-b")
    q.add_argument("--home", default=None, help="pakai folder HOME yang sudah ada (mis. /home/z)")
    q.set_defaults(anti_cmd="add")

    q = anti.add_parser("remove", help="hapus akun antigravity")
    q.add_argument("name", help="nama akun")
    q.add_argument("--delete-home", action="store_true",
                   help="hapus juga folder HOME terisolasi akun tsb")
    q.set_defaults(anti_cmd="remove")

    q = anti.add_parser("login", help="login OAuth ke akun (atau HOME utama jika kosong)")
    q.add_argument("name", nargs="?", default=None, help="nama akun (opsional)")
    q.set_defaults(anti_cmd="login")

    q = anti.add_parser("chat", help="kirim prompt lewat akun round-robin")
    q.add_argument("prompt", help="teks prompt")
    q.add_argument("--account", default=None, help="paksa akun tertentu")
    q.set_defaults(anti_cmd="chat")

    p.set_defaults(fn=cmd_antigravity)

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