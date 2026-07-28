#!/usr/bin/env python3
"""Импорт готового исследования Бавли в инвентарь атласа.

Данные лежат на сервере Contabo, здесь их нет, поэтому импортёр устойчив к
схемам: сначала разведка (--inspect), потом импорт (--apply).

    # 1. посмотреть, что реально лежит на диске, ничего не меняя
    python3 import_atlas.py --inspect

    # 2. импортировать в атлас со статусом draft (гейт inventory — отдельно)
    python3 import_atlas.py --apply

Ни одна запись не создаётся сразу verified: утверждение — только через
POST /atlas/{id}/approve живым человеком.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(os.environ.get("RESEARCH_ROOT", "/opt/alhatorah-clustering/bavli_qwen3_4b_sections/results"))
PARTITION = os.environ.get("PARTITION", "k10_r1.5")
API = os.environ.get("API", "http://127.0.0.1:8080")

# Кластеры, у которых первая генерация экспертных паспортных полей оборвалась.
# Видео-сценарии для них созданы, но паспорт неполон — на гейт они идти не должны.
TRUNCATED_PASSPORTS = {"005", "105"}

EXPECTED = {
    "sizes": f"cluster_sizes_{PARTITION}.csv",
    "membership": f"membership_{PARTITION}.csv.gz",
    "passport_index": f"passport_prep_{PARTITION}/cluster_passport_index.csv",
    "partition_summary": "partition_summary.json",
    "scripts_dir": "video_scripts_2026-07-27",
}


def find(name: str) -> Path | None:
    """Файлы могли переехать — ищем по имени в поддереве, а не только по пути."""
    direct = ROOT / name
    if direct.exists():
        return direct
    base = Path(name).name
    for candidate in ROOT.rglob(base):
        return candidate
    return None


def sniff_csv(path: Path, limit: int = 3) -> dict:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as fh:
        reader = csv.reader(fh)
        try:
            header = next(reader)
        except StopIteration:
            return {"header": [], "sample": [], "rows_estimate": 0}
        sample = [row for _, row in zip(range(limit), reader)]
    return {"header": header, "sample": sample}


def count_rows(path: Path) -> int:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as fh:
        return max(sum(1 for _ in fh) - 1, 0)


def experiment_lock(paths: list[Path]) -> str:
    """Хеш по параметрам разбиения и содержимому исходных файлов.

    Без него запись атласа не может быть утверждена: утверждение, которое
    нельзя воспроизвести, для публичной лаборатории бесполезно.
    """
    h = hashlib.sha256()
    h.update(PARTITION.encode())
    for p in sorted(paths):
        if not p or not p.exists():
            continue
        h.update(p.name.encode())
        with open(p, "rb") as fh:
            while chunk := fh.read(1 << 20):
                h.update(chunk)
    return "sha256:" + h.hexdigest()


def cluster_key(value: str) -> str:
    digits = re.sub(r"\D", "", str(value))
    return digits.zfill(3) if digits else str(value)


def parse_script(path: Path) -> dict:
    """Из сценария нужны заголовок и признаки того, что это AI-черновик."""
    text = path.read_text(encoding="utf-8", errors="replace")
    title = next((l.lstrip("# ").strip() for l in text.splitlines() if l.startswith("# ")), path.stem)
    return {
        "title": title,
        "path": str(path),
        "chars": len(text),
        "is_ai_draft": "AI-черновик" in text or "AI draft" in text,
        "has_limits": "Ограничения" in text or "Замечание эксперту" in text,
        "quotes_marked": text.count("**На экране:**"),
    }


def inspect() -> int:
    print(f"корень исследования: {ROOT}")
    if not ROOT.exists():
        print("  ОШИБКА: каталог не найден — проверьте RESEARCH_ROOT", file=sys.stderr)
        return 2

    found: dict[str, Path | None] = {k: find(v) for k, v in EXPECTED.items()}
    for key, path in found.items():
        print(f"\n[{key}] {path if path else 'НЕ НАЙДЕН — ' + EXPECTED[key]}")
        if not path:
            continue
        if path.is_dir():
            files = sorted(p for p in path.iterdir() if p.is_file())
            print(f"  файлов: {len(files)}")
            for f in files[:5]:
                print(f"    {f.name}")
            scripts = [p for p in files if p.suffix in {".md", ".txt"} and p.name.lower() != "readme.md"]
            if scripts:
                meta = parse_script(scripts[0])
                print(f"  пример сценария: {meta['title'][:70]}")
                print(f"    помечен как AI-черновик: {meta['is_ai_draft']}, "
                      f"есть ограничения: {meta['has_limits']}, "
                      f"экранных подсказок: {meta['quotes_marked']}")
        elif path.suffix == ".json":
            data = json.loads(path.read_text(encoding="utf-8"))
            print(f"  ключей верхнего уровня: {len(data) if isinstance(data, dict) else 'список'}")
            print(f"  {json.dumps(data, ensure_ascii=False)[:400]}")
        else:
            info = sniff_csv(path)
            print(f"  колонки: {info['header']}")
            for row in info["sample"]:
                print(f"    {row[:8]}")
            if path.name.startswith("cluster_sizes"):
                print(f"  строк: {count_rows(path)}")

    lock = experiment_lock([found["sizes"], found["passport_index"], found["partition_summary"]])
    print(f"\nexperiment_lock: {lock}")
    print(f"кластеры с оборванным паспортом (не пойдут на гейт): {sorted(TRUNCATED_PASSPORTS)}")
    return 0


def build_entries() -> list[dict]:
    sizes_path = find(EXPECTED["sizes"])
    index_path = find(EXPECTED["passport_index"])
    scripts_dir = find(EXPECTED["scripts_dir"])
    if not sizes_path:
        raise SystemExit(f"не найден {EXPECTED['sizes']} — запустите --inspect")

    lock = experiment_lock([sizes_path, index_path, find(EXPECTED["partition_summary"])])

    sizes: dict[str, int] = {}
    with open(sizes_path, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            keys = {k.lower(): v for k, v in row.items()}
            cid = next((keys[k] for k in ("cluster", "cluster_id", "id", "label") if k in keys), None)
            size = next((keys[k] for k in ("size", "count", "n", "n_segments") if k in keys), "0")
            if cid is not None:
                sizes[cluster_key(cid)] = int(float(size or 0))

    passports: dict[str, dict] = {}
    if index_path and index_path.exists():
        with open(index_path, encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                keys = {k.lower(): v for k, v in row.items()}
                cid = next((keys[k] for k in ("cluster", "cluster_id", "id") if k in keys), None)
                if cid is not None:
                    passports[cluster_key(cid)] = row

    scripts: dict[str, dict] = {}
    if scripts_dir and scripts_dir.exists():
        for path in scripts_dir.rglob("*"):
            if path.is_file() and path.suffix in {".md", ".txt"} and path.name.lower() != "readme.md":
                key = cluster_key(path.stem)
                if key.strip("0") or key == "000":
                    scripts[key] = parse_script(path)

    entries = []
    for cid in sorted(sizes or passports or scripts):
        passport = passports.get(cid, {})
        script = scripts.get(cid)
        blockers = []
        if cid in TRUNCATED_PASSPORTS:
            blockers.append("паспорт оборван при генерации — требуется перегенерация полей")
        if not passport:
            blockers.append("нет строки в cluster_passport_index")
        if not script:
            blockers.append("нет видео-сценария")
        elif not script["is_ai_draft"]:
            blockers.append("сценарий не помечен как AI-черновик")

        title = (script or {}).get("title") or passport.get("title") or f"Кластер {cid}"
        entries.append(
            {
                "slug": f"bavli-{PARTITION}-{cid}",
                "cluster_id": int(cid),
                "title_ru": title,
                "title_en": passport.get("title_en", ""),
                "passport": {
                    "partition": PARTITION,
                    "size": sizes.get(cid, 0),
                    "source_index": passport,
                    "script": script,
                    "import_blockers": blockers,
                },
                "experiment_lock": lock,
            }
        )
    return entries


def apply(dry: bool = False) -> int:
    import httpx

    entries = build_entries()
    ready = [e for e in entries if not e["passport"]["import_blockers"]]
    blocked = [e for e in entries if e["passport"]["import_blockers"]]

    print(f"кластеров найдено: {len(entries)}")
    print(f"  готовы к гейту inventory: {len(ready)}")
    print(f"  с замечаниями (импортируются, но на гейт не идут): {len(blocked)}")
    for e in blocked[:10]:
        print(f"    {e['slug']}: {'; '.join(e['passport']['import_blockers'])}")

    if dry:
        print("\n--dry-run: ничего не отправлено")
        return 0

    created = failed = 0
    with httpx.Client(base_url=API, timeout=30) as client:
        for e in entries:
            try:
                r = client.post("/atlas", json=e)
                if r.status_code in (200, 201):
                    created += 1
                else:
                    failed += 1
                    print(f"  {e['slug']}: HTTP {r.status_code} {r.text[:120]}")
            except httpx.HTTPError as exc:
                failed += 1
                print(f"  {e['slug']}: {exc}")

    print(f"\nсоздано записей атласа: {created}, ошибок: {failed}")
    print("Все записи в статусе draft. Утверждение — POST /atlas/{id}/approve.")
    print(f"При {len(ready)} готовых записях и 8 роликах на запись это "
          f"~{len(ready) * 8} шортов, то есть ~{len(ready) * 8 // 10} дней производства.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--inspect", action="store_true", help="разведка без изменений")
    ap.add_argument("--apply", action="store_true", help="импорт в атлас")
    ap.add_argument("--dry-run", action="store_true", help="разобрать, но не отправлять")
    args = ap.parse_args()

    if args.inspect or not (args.apply or args.dry_run):
        sys.exit(inspect())
    sys.exit(apply(dry=args.dry_run))
