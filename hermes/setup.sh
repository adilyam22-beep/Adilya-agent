#!/usr/bin/env bash
# Установка агента идей для контента на ЛОКАЛЬНЫЙ компьютер через Hermes Agent.
#
# Что делает:
#   1. Устанавливает Hermes Agent (если его нет).
#   2. Кладёт навык content-ideas и скрипт контекста в ~/.hermes.
#   3. Прописывает CONTENT_AGENT_HOME и часовой пояс.
#   4. Создаёт ежедневную задачу cron (по умолчанию 08:00) с доставкой в Telegram.
#
# Секреты (токен бота, ключ модели) этот скрипт НЕ спрашивает и НЕ хранит:
# их вводят командами `hermes model` и `hermes gateway setup` (или `hermes setup`).
#
# Запуск:  bash hermes/setup.sh            (можно с CONTENT_TZ=Asia/Almaty CONTENT_TIME=08:00)
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
TZ_NAME="${CONTENT_TZ:-$(cat /etc/timezone 2>/dev/null || echo UTC)}"
RUN_TIME="${CONTENT_TIME:-08:00}"
HOUR="${RUN_TIME%%:*}"
MINUTE="${RUN_TIME##*:}"

echo "==> Репозиторий: $REPO"
echo "==> Hermes home: $HERMES_HOME  | часовой пояс: $TZ_NAME  | время: $RUN_TIME"

# 1. Hermes Agent
if ! command -v hermes >/dev/null 2>&1; then
  echo "==> Hermes не найден, устанавливаю официальным скриптом"
  curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
  # shellcheck disable=SC1091
  export PATH="$HOME/.local/bin:$PATH"
fi
command -v hermes >/dev/null 2>&1 || { echo "Hermes не установлен"; exit 1; }
hermes --version || true

# 2. Навык и скрипт контекста
mkdir -p "$HERMES_HOME/skills/content-ideas" "$HERMES_HOME/scripts"
cp "$REPO/hermes/skills/content-ideas/SKILL.md" "$HERMES_HOME/skills/content-ideas/SKILL.md"
# Hermes не принимает симлинки вне scripts/, поэтому пишем обычный файл с путём к репозиторию.
cat > "$HERMES_HOME/scripts/content_context.py" <<PYEOF
import runpy, sys
sys.argv = ["content_context.py"]
runpy.run_path("$REPO/hermes/scripts/content_context.py", run_name="__main__")
PYEOF
echo "==> Навык установлен: $HERMES_HOME/skills/content-ideas/SKILL.md"

# 3. Переменные окружения (сохраняются в ~/.hermes/.env)
hermes config set CONTENT_AGENT_HOME "$REPO" >/dev/null
hermes config set TZ "$TZ_NAME" >/dev/null || true

# 4. Ежедневная задача. Перед повторным запуском удаляем старую с тем же именем.
JOB_NAME="Идеи для контента"
existing=$(hermes cron list 2>/dev/null | grep -F "$JOB_NAME" | awk '{print $1}' | head -1 || true)
if [ -n "${existing:-}" ]; then
  hermes cron remove "$existing" >/dev/null 2>&1 || true
  echo "==> Старая задача $existing удалена"
fi

hermes cron create "$MINUTE $HOUR * * *" \
  --name "$JOB_NAME" \
  --script content_context.py \
  --skill content-ideas \
  --deliver telegram \
  --timezone "$TZ_NAME" \
  "Вот контекст на сегодня (бренд, праздники, инфоповоды). Предложи 3 идеи постов по правилам навыка content-ideas."

cat <<EOF

Готово. Осталось два шага, которые нужно сделать самому (секреты не хранятся в репозитории):

  1. Ключ модели:      hermes model        (Anthropic / OpenRouter / другой провайдер)
  2. Telegram-бот:     hermes gateway setup  (токен от @BotFather, ваш user id)
     Затем запустите шлюз:  hermes gateway run   (или: hermes gateway install — как фоновую службу)
     В чате с ботом отправьте /sethome, чтобы утренние идеи приходили именно сюда.

Проверка без ожидания утра:  hermes cron list   и   hermes cron run <id>
EOF
