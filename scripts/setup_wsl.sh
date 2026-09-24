#!/usr/bin/env bash
# Установка контент-завода в WSL (Ubuntu 22.04 / 24.04). Идемпотентно — можно запускать повторно.
#
#   Системная часть (агент из Windows запускает без пароля):
#       wsl.exe -d Ubuntu -u root -- bash /home/<user>/Contentzavod/scripts/setup_wsl.sh --root --user <user>
#   Пользовательская часть:
#       bash scripts/setup_wsl.sh [--gpu] [--piper]
#
#   --gpu    PyTorch с CUDA + timm + faster-whisper (MiDaS-глубина для параллакса, тайминги для офлайн-TTS), ~3 ГБ
#   --piper  офлайн-TTS Piper с русскими голосами — резерв лучше espeak-ng, ~300 МБ
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE=user GPU=0 PIPER=0 TARGET_USER="${SUDO_USER:-}"
while [ $# -gt 0 ]; do
  case "$1" in
    --root) MODE=root ;;
    --user) TARGET_USER="$2"; shift ;;
    --gpu) GPU=1 ;;
    --piper) PIPER=1 ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "неизвестный параметр: $1" >&2; exit 2 ;;
  esac
  shift
done

say() { printf '\n\033[1;34m== %s\033[0m\n' "$*"; }

root_part() {
  [ "$(id -u)" = 0 ] || { echo "системную часть запускайте от root: wsl -u root" >&2; exit 1; }
  [ -n "$TARGET_USER" ] || TARGET_USER="$(stat -c %U "$ROOT")"
  export DEBIAN_FRONTEND=noninteractive
  say "Пакеты"
  apt-get update -q
  apt-get install -y -q ffmpeg espeak-ng fonts-dejavu-core python3 python3-venv python3-pip git curl \
    ca-certificates unzip docker.io
  apt-get install -y -q docker-compose-v2 2>/dev/null || apt-get install -y -q docker-compose-plugin 2>/dev/null || true
  if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    say "Python 3.11 (в этой версии Ubuntu старее)"
    apt-get install -y -q software-properties-common
    add-apt-repository -y ppa:deadsnakes/ppa
    apt-get update -q
    apt-get install -y -q python3.11 python3.11-venv
  fi

  say "cloudflared (временные ссылки на видео для Rutube/Instagram/Facebook)"
  if ! command -v cloudflared >/dev/null; then
    arch="$(dpkg --print-architecture)"
    curl -fsSL -o /tmp/cloudflared.deb \
      "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${arch}.deb"
    dpkg -i /tmp/cloudflared.deb && rm -f /tmp/cloudflared.deb
  fi

  say "systemd в WSL (нужен Docker)"
  touch /etc/wsl.conf
  if ! grep -qE '^\s*systemd\s*=\s*true' /etc/wsl.conf; then
    if grep -q '^\[boot\]' /etc/wsl.conf; then
      sed -i '/^\[boot\]/a systemd=true' /etc/wsl.conf
    else
      printf '\n[boot]\nsystemd=true\n' >> /etc/wsl.conf
    fi
    echo "systemd включён — нужен перезапуск WSL: wsl.exe --shutdown (из Windows)"
  fi

  say "Docker"
  usermod -aG docker "$TARGET_USER" || true
  if [ "$(ps -p 1 -o comm=)" = "systemd" ]; then
    systemctl enable --now docker
  else
    echo "PID 1 не systemd — Docker включится после wsl.exe --shutdown и повторного входа"
  fi
  say "Системная часть готова"
}

user_part() {
  [ "$(id -u)" != 0 ] || { echo "пользовательскую часть запускайте без root" >&2; exit 1; }
  cd "$ROOT"
  PY=python3
  for c in python3.12 python3.11 python3; do
    if command -v "$c" >/dev/null && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
      PY="$c"; break
    fi
  done
  say "Виртуальное окружение ($PY)"
  [ -x .venv/bin/python ] || "$PY" -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -e ".[dev]"

  if [ "$GPU" = 1 ]; then
    say "PyTorch с CUDA (GTX 1650: cu121)"
    .venv/bin/pip install -q torch torchvision --index-url https://download.pytorch.org/whl/cu121
    .venv/bin/pip install -q timm faster-whisper
    .venv/bin/python -c 'import torch; print("CUDA:", torch.cuda.is_available())'
  fi

  if [ "$PIPER" = 1 ]; then
    say "Piper (офлайн-TTS) и русские голоса"
    mkdir -p "$HOME/.local/bin" "$HOME/.local/share" assets/piper
    if ! command -v piper >/dev/null && [ ! -x "$HOME/.local/share/piper/piper" ]; then
      curl -fsSL -o /tmp/piper.tgz \
        "https://github.com/rhasspy/piper/releases/latest/download/piper_linux_x86_64.tar.gz"
      tar -xzf /tmp/piper.tgz -C "$HOME/.local/share" && rm -f /tmp/piper.tgz
      ln -sf "$HOME/.local/share/piper/piper" "$HOME/.local/bin/piper"
    fi
    base="https://huggingface.co/rhasspy/piper-voices/resolve/main/ru/ru_RU"
    for v in dmitri irina denis ruslan; do
      f="assets/piper/ru_RU-${v}-medium.onnx"
      [ -s "$f" ] || curl -fsSL -o "$f" "$base/$v/medium/ru_RU-${v}-medium.onnx"
      [ -s "$f.json" ] || curl -fsSL -o "$f.json" "$base/$v/medium/ru_RU-${v}-medium.onnx.json"
    done
  fi

  say "factory init"
  .venv/bin/factory init
  [ -f .env ] || { cp .env.example .env; chmod 600 .env; echo "создан .env из шаблона"; }
  say "Готово. Дальше: .venv/bin/factory setup check"
}

if [ "$MODE" = root ]; then root_part; else user_part; fi
