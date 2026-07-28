#!/usr/bin/env bash
# Развёртывание control plane на Contabo VPS. Запускать НА СЕРВЕРЕ, от root.
#
#   ssh -i <ключ> root@<ip>
#   git clone <repo> /opt/tzoar && cd /opt/tzoar/deploy && ./bootstrap-contabo.sh
#
# Скрипт идемпотентен: повторный запуск безопасен.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESEARCH_ROOT="${RESEARCH_ROOT:-/opt/alhatorah-clustering/bavli_qwen3_4b_sections/results}"

log() { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!!\033[0m %s\n' "$*" >&2; }

# ── 1. Docker ────────────────────────────────────────────────────────────
if ! command -v docker >/dev/null 2>&1; then
  log "ставлю Docker"
  curl -fsSL https://get.docker.com | sh
else
  log "Docker уже установлен: $(docker --version)"
fi

# ── 2. .env ──────────────────────────────────────────────────────────────
cd "$REPO_DIR"
if [[ ! -f .env ]]; then
  log "создаю .env со сгенерированными секретами"
  cp .env.example .env
  gen() { openssl rand -base64 24 | tr -d '/+=' | head -c 32; }
  sed -i "s|^PG_PASSWORD=.*|PG_PASSWORD=$(gen)|" .env
  sed -i "s|^S3_ACCESS_KEY=.*|S3_ACCESS_KEY=tzoar-$(gen | head -c 12)|" .env
  sed -i "s|^S3_SECRET_KEY=.*|S3_SECRET_KEY=$(gen)|" .env
  chmod 600 .env
else
  log ".env уже существует — не трогаю"
fi
# .env не должен попасть в git ни при каких условиях
grep -qxF 'deploy/.env' ../.gitignore 2>/dev/null || echo 'deploy/.env' >> ../.gitignore

# ── 3. Исследовательские данные ──────────────────────────────────────────
if [[ -d "$RESEARCH_ROOT" ]]; then
  log "исследование найдено: $RESEARCH_ROOT"
  du -sh "$RESEARCH_ROOT" 2>/dev/null || true
else
  warn "не найден $RESEARCH_ROOT — импорт атласа будет недоступен"
  warn "задайте RESEARCH_ROOT=<путь> перед запуском"
fi

# ── 4. Диск ──────────────────────────────────────────────────────────────
AVAIL_GB=$(df -BG --output=avail / | tail -1 | tr -dc '0-9')
log "свободно на диске: ${AVAIL_GB} ГБ"
[[ "$AVAIL_GB" -lt 20 ]] && warn "меньше 20 ГБ — MinIO и Postgres быстро упрутся"

# ── 5. Control plane ─────────────────────────────────────────────────────
log "поднимаю control plane"
docker compose -f docker-compose.control.yml up -d --build

log "жду готовности оркестратора"
for i in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:8080/health >/dev/null 2>&1; then
    log "оркестратор отвечает"
    break
  fi
  [[ "$i" -eq 60 ]] && { warn "оркестратор не поднялся за 60 с"; docker compose -f docker-compose.control.yml logs --tail=40 orchestrator; exit 1; }
  sleep 1
done

# ── 6. Проверка политики ─────────────────────────────────────────────────
log "прогоняю проверку исполнения политики"
docker compose -f docker-compose.control.yml exec -T orchestrator \
  python -c "import sys; sys.path.insert(0,'/app')" 2>/dev/null || true
docker compose -f docker-compose.control.yml run --rm --no-deps \
  -v "$REPO_DIR/smoke_test.py:/app/smoke_test.py:ro" \
  -e DATABASE_URL=sqlite:////tmp/smoke.db \
  orchestrator python /app/smoke_test.py

# ── 7. Что дальше ────────────────────────────────────────────────────────
cat <<EOF

$(log "control plane развёрнут")

  состояние:   curl -s http://127.0.0.1:8080/status | python3 -m json.tool
  отчёт:       curl -s http://127.0.0.1:8080/report | python3 -m json.tool
  логи:        docker compose -f docker-compose.control.yml logs -f

Следующий шаг — разведка исследовательских данных (ничего не меняет):

  RESEARCH_ROOT=$RESEARCH_ROOT python3 tools/import_atlas.py --inspect

Порты 8080, 9000, 9001, 6333 привязаны к 127.0.0.1 и наружу не смотрят.
Доступ снаружи — только через SSH-туннель:

  ssh -i <ключ> -L 8080:127.0.0.1:8080 root@<ip>

EOF
