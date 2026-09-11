#!/usr/bin/env bash
#
# install.sh - setup otomatis gemini-web2api di laptop baru
#
# Cara pakai:
#   ./install.sh
#
# Yang dilakukan:
#   1. Cek & install prasyarat (python3, venv, pip)
#   2. Buat venv + install dependensi (httpx, h2)
#   3. Buat config.json jika belum ada
#   4. Pasang command global: gemini-manager
#   5. Tampilkan langkah berikutnya
#
set -euo pipefail

BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="$BASE/config.json"
PYTHON_BIN="python3"
LIB_BIN="$HOME/.local/bin"

c_green='\033[0;32m'
c_yellow='\033[0;33m'
c_red='\033[0;31m'
c_cyan='\033[0;36m'
c_bold='\033[1m'
c_reset='\033[0m'

info()  { echo -e "${c_green}[*]${c_reset} $*"; }
warn()  { echo -e "${c_yellow}[!]${c_reset} $*"; }
die()   { echo -e "${c_red}[x]${c_reset} $*"; exit 1; }

echo -e "${c_bold}gemini-web2api installer${c_reset}"
echo

[ -f "$BASE/gemini_web2api.py" ] || die "Tidak menemukan gemini_web2api.py. Jalankan dari dalam folder project."

# ── 1. prasyarat system ──────────────────────────────────────────────────────
info "Cek prasyarat (python3 / venv / pip)..."
need_install=()
command -v "$PYTHON_BIN" >/dev/null || need_install+=("python3")
$PYTHON_BIN -m venv --help >/dev/null 2>&1 || need_install+=("python3-venv")
$PYTHON_BIN -m pip --version >/dev/null 2>&1 || need_install+=("python3-pip")

if [ ${#need_install[@]} -gt 0 ]; then
    warn "Perlu install: ${need_install[*]}"
    if command -v apt-get >/dev/null; then
        warn "Menjalankan: sudo apt-get install -y ${need_install[*]}"
        sudo -v
        sudo apt-get update -qq
        sudo apt-get install -y "${need_install[@]}"
    else
        die "Install manual dulu: ${need_install[*]} (bukan distro Debian/Ubuntu)."
    fi
fi
info "Python: $($PYTHON_BIN --version)"

# ── 2. venv + dependensi ─────────────────────────────────────────────────────
if [ ! -d "$BASE/venv" ]; then
    info "Membuat virtualenv..."
    $PYTHON_BIN -m venv "$BASE/venv"
else
    info "venv sudah ada, dilewati."
fi
VENV_PY="$BASE/venv/bin/python3"
[ -x "$VENV_PY" ] || die "venv gagal dibuat."

info "Install dependensi (httpx, h2)..."
[ -f "$BASE/requirements.txt" ] && "$VENV_PY" -m pip install -r "$BASE/requirements.txt" h2
"$VENV_PY" -m pip install -q h2

# ── 3. config.json ───────────────────────────────────────────────────────────
if [ ! -f "$CONFIG_FILE" ]; then
    if [ -f "$BASE/config.example.json" ]; then
        info "Membuat config.json dari config.example.json"
        cp "$BASE/config.example.json" "$CONFIG_FILE"
    else
        warn "Tidak ada config.example.json, config.json dibuat default."
        cat > "$CONFIG_FILE" <<JSON
{
  "port": 8081,
  "host": "0.0.0.0",
  "retry_attempts": 3,
  "retry_delay_sec": 2,
  "request_timeout_sec": 180,
  "gemini_bl": "boq_assistant-bard-web-server_20260716.08_p0",
  "auth_user": null,
  "xsrf_token": null,
  "default_model": "gemini-3.6-flash",
  "api_keys": ["sk-gemini"],
  "proxy": null,
  "log_requests": true,
  "temporary_chats": false
}
JSON
    fi
else
    info "config.json sudah ada, tidak diubah (periksa isinya: $CONFIG_FILE)."
fi

# ── 4. command global gemini-manager ────────────────────────────────────────
info "Pasang command global 'gemini-manager'..."
chmod +x "$BASE/gemini-manager.py"
mkdir -p "$LIB_BIN"
ln -sf "$BASE/gemini-manager.py" "$LIB_BIN/gemini-manager"

if ! echo "$PATH" | tr ':' '\n' | grep -qx "$LIB_BIN"; then
    warn "$LIB_BIN belum ada di PATH."
    for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
        if [ -f "$rc" ] && ! grep -q "\.local/bin" "$rc"; then
            echo '' >> "$rc"
            echo '# gemini-manager' >> "$rc"
            echo "export PATH=\"\$HOME/.local/bin:\$PATH\"" >> "$rc"
            info "PATH ditambahkan ke $rc (source ulang: source $rc)"
        fi
    done
    export PATH="$LIB_BIN:$PATH"
fi
command -v gemini-manager >/dev/null || die "gemini-manager tak terpasang."
info "gemini-manager -> $(command -v gemini-manager)"

# ── 5. cek cookie & selesai ──────────────────────────────────────────────────
NF=$(command -v gemini-manager)
echo
info "Selesai! Langkah berikutnya:"
echo "  1. Ambil cookie Google di laptop ini (cookie per-device):"
if [ -f "$BASE/cookie.txt" ]; then
    echo "     - cookie.txt sudah ada di project. Kalau cookie dari laptop lama, ganti dengan yang segar!"
fi
echo "     - export cookie akun 1  -> contoh: $(basename "$BASE")/cookie.txt"
echo "     - export cookie akun 2  -> contoh: $(basename "$BASE")/cookie2.txt"
echo "  2. Daftarkan akun (jika kosong):"
echo "     gemini-manager add-account akun-1 --cookie-file cookie.txt"
echo "     gemini-manager add-account akun-2 --cookie-file cookie2.txt"
echo "  3. Jalankan & tes:"
echo "     gemini-manager start"
echo "     gemini-manager status"
echo "     gemini-manager test --count 4"
echo "  4. (Opsional) systemd agar auto-start:"
echo "     gemini-manager install-service --enable --start"
exit 0