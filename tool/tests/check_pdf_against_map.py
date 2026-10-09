#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сверка готового PDF с картой: в дайджесте не должно быть ни одной цифры, которой нет в карте.

Запуск: python check_pdf_against_map.py index.html digest.pdf [config.json]
Нужна утилита pdftotext (пакет poppler-utils). Для PDF под паролем: переменная DIGEST_PDF_PASSWORD.

Проверяется в обе стороны:
  1) каждое число из PDF (даты, номера записей, ИНН, номера ИМО, суммы) есть в данных карты
     либо относится к служебным (номер страницы, дата выпуска, номер раздела);
  2) все идентификаторы из карточек ключевых событий (RUS…, ИНН, ИМО) попали в PDF.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL))
import build_digest as bd  # noqa: E402


def strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from strings(v)
    elif isinstance(obj, (int, float)):
        yield str(obj)


def numbers(text):
    """Числа длиной от 2 знаков; пробел-разделитель тысяч убирается («1 650» → «1650»)."""
    text = re.sub(r"(?<=\d)[   ](?=\d{3}(?!\d))", "", text)
    return set(re.findall(r"\d{2,}", text))


def main():
    map_path, pdf_path = sys.argv[1], sys.argv[2]
    cfg = json.loads(Path(sys.argv[3] if len(sys.argv) > 3 else TOOL / "config.json").read_text(encoding="utf-8"))
    data = bd.read_map(map_path)
    cmd = ["pdftotext", "-layout"]
    pw = os.environ.get("DIGEST_PDF_PASSWORD")
    if pw:
        cmd += ["-upw", pw]
    pdf_text = subprocess.run(cmd + [pdf_path, "-"], capture_output=True, check=True).stdout.decode("utf-8")
    pages = pdf_text.count("\f")

    corpus = " ".join(bd.clean(s) for s in strings(data))
    corpus += " " + " ".join(strings(cfg))
    known = numbers(corpus)
    # служебные числа: номера страниц и разделов, год и дата выпуска, срок просмотра контрольных дат
    service = {str(n) for n in range(0, pages + 2)} | {str(cfg["content"]["deadline_days_ahead"])}
    for iso in re.findall(r"\d{4}-\d{2}-\d{2}", json.dumps(data.get("DEADLINES", {}))):
        y, m, d = iso.split("-")
        service |= {y, m, d}                      # контрольные даты записаны в карте как ГГГГ-ММ-ДД
    unknown = sorted(n for n in numbers(pdf_text) if n not in known and n not in service)
    # дата выпуска в капсуле титульного слайда — единственное, чего в карте может не быть
    first_page = pdf_text.split("\f")[0]
    release = numbers(first_page)
    unknown = [n for n in unknown if n not in release]

    rules = bd.Rules(cfg)
    key, up, other, log = bd.collect_events(data, cfg, rules, {"events": {}, "digests": []}, force=True)
    flat = re.sub(r"\s+", " ", pdf_text)
    missing = []
    for e in key:
        body = e["desc"] + " " + bd.clean(e["detail"])
        ids = set(re.findall(r"RUS\d{4}", body)) | set(re.findall(r"ИНН \d{10,12}", body)) | set(re.findall(r"\((\d{7})\)", body))
        missing += [f"{e['jur']}: {i}" for i in sorted(ids) if i not in flat]

    print(f"Страниц в PDF: {pages}. Чисел в PDF: {len(numbers(pdf_text))}. Ключевых событий: {len(key)}.")
    print("Чисел, которых нет в карте:", unknown or "нет")
    print("Идентификаторов из карточек, не попавших в PDF:", missing or "нет")
    return 1 if unknown or missing else 0


if __name__ == "__main__":
    sys.exit(main())
