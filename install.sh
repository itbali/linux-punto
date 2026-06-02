#!/usr/bin/env bash
# Установка/запуск linux-punto: автозапуск в KDE-сессии + старт прямо сейчас.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$(command -v python3)"
AUTOSTART_DIR="$HOME/.config/autostart"
DESKTOP="$AUTOSTART_DIR/linux-punto.desktop"

# Проверка зависимостей
missing=()
command -v xclip >/dev/null   || missing+=("xclip")
"$PY" -c "import dbus" 2>/dev/null || missing+=("python3-dbus")
if [ "${#missing[@]}" -gt 0 ]; then
  echo "Не хватает зависимостей: ${missing[*]}"
  echo "Установи: sudo apt install ${missing[*]}"
  exit 1
fi

mkdir -p "$AUTOSTART_DIR"
cat > "$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Name=linux-punto (Punto Switcher)
Comment=Переключение раскладки и исправление текста по хоткею
Exec=$PY $DIR/punto.py
X-KDE-autostart-phase=2
OnlyShowIn=KDE;
Terminal=false
NoDisplay=true
EOF
echo "Автозапуск прописан: $DESKTOP"

# Перезапускаем уже работающий экземпляр и стартуем заново
pkill -f "punto.py" 2>/dev/null || true
sleep 0.3
nohup "$PY" "$DIR/punto.py" >/tmp/linux-punto.log 2>&1 &
sleep 0.5
echo "Запущено. Лог: /tmp/linux-punto.log"
echo
echo "Хоткей по умолчанию: Pause."
echo "Настройка: $HOME/.config/punto-switcher/config.json"
echo
echo "Проверка: выдели текст 'ghbdtn' где-нибудь и нажми Pause."
