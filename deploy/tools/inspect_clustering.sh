#!/usr/bin/env bash
# Разведка идущей кластеризации на Contabo. ТОЛЬКО ЧТЕНИЕ.
#
# Ничего не запускает, не останавливает и не пишет в каталоги задачи.
# Безопасно выполнять, пока расчёт идёт.
#
#   ssh root@84.247.137.69 'bash -s' < inspect_clustering.sh > clustering_report.txt
# или на сервере:
#   bash inspect_clustering.sh | tee /tmp/clustering_report.txt
#
# Переменные окружения процессов выводятся с вырезанными секретами, но отчёт
# всё равно просмотрите перед тем, как куда-либо его отправлять.

RESULTS="${RESULTS:-/opt/alhatorah-clustering/bavli_qwen3_4b_sections/results}"
ROOT="${ROOT:-/opt/alhatorah-clustering}"

hr() { printf '\n──── %s ────\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

hr "хост"
hostname; uptime; date -u
echo "ядер: $(nproc), память:"; free -h | head -2

hr "GPU"
if have nvidia-smi; then
  nvidia-smi --query-gpu=name,memory.total,memory.used,utilization.gpu --format=csv
  nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv
else
  echo "nvidia-smi нет — расчёт идёт на CPU"
fi

hr "процессы кластеризации"
ps -eo pid,ppid,user,etime,pcpu,pmem,rss,stat,cmd --sort=-pcpu \
  | grep -Ei 'python|leiden|louvain|cluster|embed|faiss|hnsw|qwen' \
  | grep -v grep | head -25

hr "самые тяжёлые процессы (на случай другого имени)"
ps -eo pid,etime,pcpu,pmem,rss,cmd --sort=-pmem | head -8

PIDS=$(pgrep -f 'cluster|leiden|louvain|embed|qwen' 2>/dev/null | head -5)
for pid in $PIDS; do
  hr "PID $pid — подробности"
  echo "команда:"; tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null; echo
  echo "рабочий каталог: $(readlink -f /proc/$pid/cwd 2>/dev/null)"
  echo "исполняемый файл: $(readlink -f /proc/$pid/exe 2>/dev/null)"
  echo "запущен: $(ps -o lstart= -p "$pid" 2>/dev/null)"
  echo "потоков: $(ls /proc/$pid/task 2>/dev/null | wc -l)"
  echo "окружение (секреты вырезаны):"
  tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null \
    | grep -Ev '(?i)(TOKEN|SECRET|PASSWORD|KEY|CREDENTIAL|AUTH)=' \
    | grep -E '^(PATH|PYTHON|VIRTUAL_ENV|CUDA|OMP|MKL|HF_|TRANSFORMERS|TORCH|OPENBLAS|NUMBA)' | head -15
  echo "открытые файлы данных:"
  ls -l "/proc/$pid/fd" 2>/dev/null | grep -Ev 'socket|pipe|anon_inode|/dev/' | awk '{print $NF}' | head -15
done

hr "супервизия"
have systemctl && systemctl list-units --type=service --state=running 2>/dev/null | grep -Ei 'cluster|alhatorah|embed'
have tmux && (tmux ls 2>/dev/null || echo "tmux: сессий нет")
have screen && (screen -ls 2>/dev/null | head -5)
ls -la /etc/systemd/system/ 2>/dev/null | grep -Ei 'cluster|alhatorah' || echo "systemd-юнитов проекта нет"
crontab -l 2>/dev/null | grep -v '^#' | head -10 || echo "crontab пуст"

hr "дерево кода проекта"
[ -d "$ROOT" ] && find "$ROOT" -maxdepth 3 -type d -not -path '*/.git/*' -not -path '*/__pycache__*' | head -30
echo "--- скрипты ---"
[ -d "$ROOT" ] && find "$ROOT" -maxdepth 3 -name '*.py' -o -maxdepth 3 -name '*.sh' -o -maxdepth 3 -name '*.toml' -o -maxdepth 3 -name '*.yaml' 2>/dev/null | grep -v __pycache__ | head -30

hr "git-состояние кода"
if [ -d "$ROOT/.git" ]; then
  git -C "$ROOT" log --oneline -5
  git -C "$ROOT" status --short | head -10
  git -C "$ROOT" remote -v
else
  echo "$ROOT не под git — воспроизводимость держится только на файлах"
fi

hr "python-окружение"
for venv in "$ROOT/.venv" "$ROOT/venv" /opt/venv; do
  [ -x "$venv/bin/python" ] && { echo "venv: $venv"; "$venv/bin/python" -V; \
    "$venv/bin/pip" list 2>/dev/null | grep -Ei 'torch|transformers|sentence|leiden|igraph|scanpy|umap|faiss|hnswlib|numpy|scikit|polars|pandas'; }
done
have python3 && { echo "системный python: $(python3 -V)"; }

hr "результаты: $RESULTS"
if [ -d "$RESULTS" ]; then
  du -sh "$RESULTS" 2>/dev/null
  echo "--- верхний уровень ---"; ls -la "$RESULTS" | head -30
  echo "--- изменённые за сутки (что пишет текущий расчёт) ---"
  find "$RESULTS" -mmin -1440 -type f -printf '%TY-%Tm-%Td %TH:%TM  %10s  %p\n' 2>/dev/null | sort | tail -20
  echo "--- эмбеддинги и графы ---"
  find "$RESULTS" -maxdepth 3 \( -name '*.npy' -o -name '*.npz' -o -name '*.faiss' -o -name '*.index' -o -name '*.parquet' -o -name '*.h5' \) \
    -printf '%10s  %p\n' 2>/dev/null | head -15
  echo "--- конфиги и параметры ---"
  find "$RESULTS" -maxdepth 3 \( -name '*.json' -o -name '*.yaml' -o -name '*.toml' \) -size -200k \
    -printf '%p\n' 2>/dev/null | head -20
else
  echo "каталог не найден — задайте RESULTS=<путь>"
fi

hr "логи"
find "$ROOT" "$RESULTS" /var/log -maxdepth 3 -name '*.log' -mmin -1440 2>/dev/null | head -10 | while read -r f; do
  echo "=== $f (последние 25 строк) ==="; tail -25 "$f"
done
[ -f /var/log/syslog ] && echo "=== syslog, OOM ===" && grep -i 'out of memory\|oom-killer' /var/log/syslog 2>/dev/null | tail -3

hr "диск"
df -h / /opt 2>/dev/null | sort -u

hr "готово"
echo "Пришлите этот отчёт целиком. Секреты из окружения вырезаны, но перечитайте перед отправкой."
