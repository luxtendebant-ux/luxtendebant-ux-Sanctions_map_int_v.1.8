#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Автоматический выпуск санкционного дайджеста на основе Комплаенс-карты.

Скрипт ничего не ищет в интернете и ничего не сочиняет: он берёт данные, уже
записанные и сверенные в карте (index.html), находит ключевые события —
новые включения российских компаний и лиц в санкционные списки — и верстает
из них PDF в фирменном оформлении. Каждый факт в дайджесте — дословно из карты.

Запуск:
    python build_digest.py --map index.html --state ledger.json --out out/

Коды завершения: 0 — дайджест выпущен либо ключевых событий нет;
2 — структура карты изменилась и не читается; 3 — ошибка вёрстки PDF.
"""
import argparse
import base64
import datetime as dt
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
GROUPS = [
    ("SECTIONS", "Санкции"),
    ("EXPORT_CONTROL", "Экспортный контроль"),
    ("RUSSIAN_MEASURES", "Российские меры"),
]
GROUP_LABEL = dict(GROUPS)
DECLS = [
    "SECTIONS", "EXPORT_CONTROL", "RUSSIAN_MEASURES", "RUMORS", "CARD_ARCHIVE",
    "COMPARISON", "OFFICER_CONCLUSION", "MAP_META", "CHANGELOG", "DEADLINES",
    "SOURCE_REGISTRY", "EXEC_SUMMARY",
]
REQUIRED = ["SECTIONS", "MAP_META", "CHANGELOG"]
STATUS_LABEL = {
    "active": "Действует",
    "drafting": "Принята, не вступила",
    "pending": "В процессе принятия",
    "announced": "Заявлено официально",
    "rumor": "Слух / неподтверждено",
    "expired": "Истекла / требует сверки",
}
RU_DATE = re.compile(r"(?<![\d.])(\d{1,2})\.(\d{1,2})\.(\d{4})(?!\d)")
MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
              "августа", "сентября", "октября", "ноября", "декабря"]


class MapFormatError(Exception):
    """Карта не читается: изменилась структура данных в index.html."""


# ----------------------------------------------------------------- чтение карты

def read_map(path):
    """Достаёт объявления данных (let SECTIONS = [...]; и т.д.) из index.html."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise MapFormatError(f"файл карты не читается: {e}") from e
    dec = json.JSONDecoder()
    data = {}
    for name in DECLS:
        m = re.search(r"\n(?:let|const|var)\s+" + name + r"\s*=\s*", text)
        if not m:
            continue
        try:
            data[name], _ = dec.raw_decode(text, m.end())
        except json.JSONDecodeError as e:
            raise MapFormatError(f"блок {name} не разбирается как JSON: {e}") from e
    missing = [k for k in REQUIRED if k not in data]
    if missing:
        raise MapFormatError("в карте не найдены блоки данных: " + ", ".join(missing))
    meta = data["MAP_META"]
    if not isinstance(meta, dict) or not parse_ru_date(meta.get("cut")):
        raise MapFormatError("в MAP_META нет даты среза (cut)")
    for g, _ in GROUPS:
        for sec in data.get(g, []):
            if "id" not in sec or not isinstance(sec.get("items"), list):
                raise MapFormatError(f"раздел в блоке {g} без id или items")
            for it in sec["items"]:
                if "title" not in it or "status" not in it:
                    raise MapFormatError(f"карточка без title/status в разделе {sec['id']}")
    return data


def iter_cards(data):
    for g, _ in GROUPS:
        for sec in data.get(g, []) or []:
            for it in sec["items"]:
                yield g, sec, it


# ----------------------------------------------------------------- даты и текст

def parse_ru_date(s):
    m = RU_DATE.search(s or "")
    if not m:
        return None
    try:
        return dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


def all_ru_dates(s):
    out = []
    for m in RU_DATE.finditer(s or ""):
        try:
            out.append(dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1))))
        except ValueError:
            pass
    return out


def fmt(d):
    return d.strftime("%d.%m.%Y") if d else "—"


def fmt_long(d):
    return f"{d.day} {MONTHS_GEN[d.month - 1]} {d.year} года"


def clean(s):
    """Текст карты → чистый текст: без HTML-разметки, с нормальными пробелами."""
    s = "" if s is None else str(s)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    s = s.replace("\r", "")
    s = re.sub(r"[ \t ]+", " ", s)
    s = re.sub(r" *\n *", "\n", s)
    return s.strip()


ABBR = {"г", "гг", "т", "д", "е", "п", "пп", "ст", "ч", "см", "др", "пр", "руб", "млн", "млрд",
        "трлн", "тыс", "напр", "им", "ред", "изм", "стр", "ул", "просп", "No", "no", "vs", "Inc",
        "Ltd", "Co", "Corp", "S", "A", "L", "C", "U", "K", "N", "ок", "долл", "барр", "янв", "февр",
        "авг", "сент", "окт", "нояб", "дек", "кон", "нач", "иниц", "Доп", "доп", "Исп", "исп"}


def split_sentences(text):
    """Делит абзац на предложения, не разрывая сокращения, инициалы и номера."""
    out, start = [], 0
    for m in re.finditer(r"[.!?…]+[»\")]*\s+(?=[«\"(]?[А-ЯЁA-Z0-9])", text):
        end = m.end()
        before = text[start:m.start()]
        word = re.search(r"([A-Za-zА-Яа-яЁё]+)$", before)
        w = word.group(1) if word else ""
        if text[m.start()] == "." and w:
            if w in ABBR or len(w) == 1:        # сокращение или инициал
                continue
        if text[m.start()] == "." and re.search(r"\d$", before) and re.match(r"\d", text[end:end + 1] or ""):
            continue                             # «08.10. 2026» и т.п.
        out.append(text[start:end].strip())
        start = end
    tail = text[start:].strip()
    if tail:
        out.append(tail)
    return out


def split_top(text, sep):
    """Делит строку по разделителю только вне скобок и кавычек-ёлочек."""
    parts, depth, cur = [], 0, []
    for ch in text:
        if ch in "(«[":
            depth += 1
        elif ch in ")»]":
            depth = max(0, depth - 1)
        if ch == sep and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur).strip())
    return [p for p in parts if p]


# ----------------------------------------------------------------- разбор карточки

HEAD_RE = re.compile(
    r"(?:^|(?<=[.;:!?»)]\s))"
    r"([А-ЯЁA-Z][А-ЯЁA-Z0-9 \-–—«»/,№.]{3,70}?)"
    r"(\s*\([^)]{1,220}\))?[.:]\s+")
HISTORY_RE = re.compile(r"^(ПРЕДЫДУЩАЯ РЕДАКЦИЯ КАРТОЧКИ|РАНЕЕ В КАРТОЧКЕ|ИСТОРИЯ КАРТОЧКИ)")
PRACTICAL_RE = re.compile(r"^(ПРАКТИЧЕСК|ЗНАЧЕНИЕ ДЛЯ|ЧТО ОТСЛЕЖИВАТЬ|ЗНАЧЕНИЕ$)")
CONTEXT_RE = re.compile(r"^(РЕАКЦИЯ|КОНТЕКСТ)")
GAP_RE = re.compile(r"^(ЧТО НЕ УСТАНОВЛЕНО|ОГРАНИЧЕНИЕ ВЫВОДА|РАСХОЖДЕНИЯ|ИСПРАВЛЕН)")
SOURCES_RE = re.compile(r"^(?:Перво)?источник\w*(?:\s*\([^)]*\))?\s*:\s*", re.I)


def is_heading(h):
    if re.search(r"[а-яёa-z]", h):
        return False
    return bool(re.search(r"[А-ЯЁA-Z]{4,}", h))


PROPER = re.compile(r"^(росси|япони|канад|великобритани|швейцари|австрали|кита|турци|инди|украин|беларус|"
                    r"европ|иран|киргизи|казахстан|серби|венгри|герман|франци|итали|москв|лондон|брюссел|вашингтон)", re.I)
KEEP_UPPER = {"КНДР", "ОАЭ", "СНБО", "НОВАТЭК", "ТЭК", "СПГ", "СУГ", "ВПК", "МИД", "ЦБ", "РФ", "США", "ЕС", "ИНК",
              "НПЗ", "БПЛА", "ООН", "ФНС", "ФТС", "ВЭД", "НДС", "КНР", "СНГ", "ЕАЭС", "ИМО"}


def cap(h):
    """«ПОСТАВЩИКИ ВПК» → «Поставщики ВПК», «РЕАКЦИЯ РОССИИ» → «Реакция России»."""
    out = []
    for i, w in enumerate(h.strip().split(" ")):
        core = re.sub(r"[^А-ЯЁA-Zа-яёa-z]", "", w)
        if not core or core in KEEP_UPPER or re.fullmatch(r"[A-Z]+", core):
            out.append(w)
        elif i == 0 or PROPER.match(core):
            out.append(w[:1] + w[1:].lower())
        else:
            out.append(w.lower())
    return " ".join(out)


def split_blocks(detail):
    """Развёрнутый текст карточки → блоки [{head, note, body}] по заголовкам ПРОПИСНЫМИ."""
    blocks = []
    for para in [p.strip() for p in clean(detail).split("\n\n") if p.strip()]:
        if HISTORY_RE.match(para):
            continue
        marks = [m for m in HEAD_RE.finditer(para) if is_heading(m.group(1))]
        if not marks:
            blocks.append({"head": None, "note": None, "body": para})
            continue
        if marks[0].start() > 0:
            blocks.append({"head": None, "note": None, "body": para[:marks[0].start()].strip()})
        for i, m in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(para)
            note = (m.group(2) or "").strip()
            text = para[m.end():end].strip()
            blocks.append({"head": m.group(1).strip(), "note": note[1:-1] if note else None,
                           "body": text[:1].upper() + text[1:]})
    return [b for b in blocks if b["body"]]


def up1(t):
    return t[:1].upper() + t[1:] if re.match(r"[а-яё]", t) else t


def body_units(body, max_par=520, lists=True):
    """Текст блока → единицы вёрстки: нумерованные пункты, перечни, абзацы."""
    units = []
    if not lists:
        cur = ""
        for s in split_sentences(body):
            if cur and len(cur) + len(s) + 1 > max_par:
                units.append({"t": "p", "text": cur})
                cur = s
            else:
                cur = (cur + " " + s).strip()
        if cur:
            units.append({"t": "p", "text": cur})
        return units
    # 1) перечисление «(1) …; (2) …»
    enum = list(re.finditer(r"\((\d{1,2})\)\s+", body))
    if len(enum) >= 2 and [int(m.group(1)) for m in enum][:2] == [1, 2]:
        lead = body[:enum[0].start()].strip(" :—-")
        if lead:
            units.append({"t": "p", "text": lead + ":"})
        for i, m in enumerate(enum):
            end = enum[i + 1].start() if i + 1 < len(enum) else len(body)
            item = body[m.end():end].strip().rstrip(";").strip()
            if item:
                item = item[:1].upper() + item[1:]
                units.append({"t": "li", "ordered": True, "n": int(m.group(1)),
                              "text": item if item.endswith((".", "!", "?")) else item + "."})
        return units
    # 2) перечень через «;» вне скобок: названия организаций, лиц, судов
    parts = split_top(body, ";")
    if len(parts) >= 3 and sorted(len(p) for p in parts)[len(parts) // 2] <= 230:
        tail = None
        sents = split_sentences(parts[-1])
        if len(sents) > 1:
            parts[-1], tail = sents[0], " ".join(sents[1:])
        for p in parts:
            units.append({"t": "li", "ordered": False, "text": up1(p.rstrip(".").strip())})
        if tail:
            units.extend(body_units(tail, max_par))
        return units
    # 3) перечень судов «Название (номер ИМО), …»
    parts = split_top(body.rstrip("."), ",")
    if len(parts) >= 4 and all(re.search(r"\(\d{6,8}\)$", p) for p in parts):
        return [{"t": "li", "ordered": False, "text": p} for p in parts]
    # 4) обычный текст; длинный абзац делится по предложениям
    if len(body) <= max_par:
        return [{"t": "p", "text": body}]
    cur = ""
    for s in split_sentences(body):
        if cur and len(cur) + len(s) + 1 > max_par:
            units.append({"t": "p", "text": cur})
            cur = s
        else:
            cur = (cur + " " + s).strip()
    if cur:
        units.append({"t": "p", "text": cur})
    return units


def first_date_in(text, fallback=None):
    return parse_ru_date(text) or fallback


# ----------------------------------------------------------------- отбор событий

class Rules:
    def __init__(self, cfg):
        ev = cfg["events"]
        self.groups = set(ev["groups"])
        self.trigger = set(ev["trigger_statuses"])
        self.upcoming = set(ev["upcoming_statuses"])
        self.tags = {t.lower() for t in ev["positive_tags"]}
        self.pos = [re.compile(p, re.I) for p in ev["positive_patterns"]]
        self.neg = [re.compile(p, re.I) for p in ev["negative_patterns"]]
        self.watch = [re.compile(p) for p in cfg["watchlist"]["patterns"]]
        self.watch_alone = bool(cfg["watchlist"].get("trigger_alone", False))

    def classify(self, card):
        """→ (это включение?, пояснение). Смотрим заголовок и метки карточки."""
        title = clean(card.get("title"))
        for r in self.neg:
            m = r.search(title)
            if m:
                return False, f"не включение: в заголовке «{m.group(0).strip()}»"
        for r in self.pos:
            m = r.search(title)
            if m:
                return True, f"включение: в заголовке «{m.group(0).strip()}»"
        tags = {str(t).lower() for t in (card.get("tags") or [])}
        hit = tags & self.tags
        if hit:
            return True, "включение: метка «" + sorted(hit)[0] + "»"
        return False, "признаков включения нет"

    def watch_hits(self, card):
        """Предложения карточки, где упомянуты наименования из списка наблюдения."""
        seen, out = set(), []
        for field in ("title", "desc", "detail"):
            for para in clean(card.get(field)).split("\n\n"):
                if HISTORY_RE.match(para.strip()):
                    continue
                for sent in split_sentences(para):
                    for s in re.split(r"(?<=[;:])\s*(?=\(\d{1,2}\)\s)", sent):
                        s = re.sub(r"^\(\d{1,2}\)\s+", "", s.strip()).rstrip(";").strip()
                        s = up1(s)
                        if s and not s.endswith((".", "!", "?", "…")):
                            s += "."
                        if s and any(r.search(s) for r in self.watch) and s not in seen:
                            seen.add(s)
                            out.append(s)
        return out


def sec_names(cfg, sec_id, sec_title, card_title):
    for prefix, pair in cfg.get("title_prefix_overrides", {}).items():
        if prefix.startswith("_"):
            continue
        if clean(card_title).startswith(prefix):
            return pair[0], pair[1]
    pair = cfg.get("sections", {}).get(sec_id)
    if pair and not sec_id.startswith("_"):
        return pair[0], pair[1]
    return clean(sec_title), re.sub(r"[^A-Za-z0-9]", "", sec_id).upper() or "X"


JUR_PREFIX = re.compile(r"^(?:UK|EU|US|ЕС|США|Канада|Япония|Австралия|Швейцария|Великобритания|Новая Зеландия)\s*[:—–]\s*")


def bare_title(ev):
    """Заголовок карточки без повторного названия страны в начале («UK: …» → «…»)."""
    t = JUR_PREFIX.sub("", ev["title"], count=1)
    return t[:1].lower() + t[1:] if re.match(r"[А-ЯЁ][а-яё]", t) else t


def sentence(text):
    text = text.strip()
    return text if text.endswith((".", "!", "?", "…")) else text + "."


def event_key(ev):
    raw = "|".join([ev["group"], ev["sec"], fmt(ev["event_date"]), ev["url"] or ev["title"],
                    ev["status_class"], ev["watch_hash"]])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def collect_events(data, cfg, rules, ledger, force=False):
    """Просматривает изменившиеся карточки и решает, что идёт в дайджест.

    Возвращает (ключевые события, ожидаемые включения, прочие изменения, журнал решений).
    """
    meta = data["MAP_META"]
    cut = parse_ru_date(meta["cut"])
    prev = parse_ru_date(meta.get("prev")) or cut
    changes = {}
    for i in data["CHANGELOG"].get("items", []):
        changes[(i.get("group"), i.get("sec"), i.get("title"))] = i
    seen = ledger.get("events", {})
    key_events, upcoming, others, log = [], [], [], []

    for g, sec, card in iter_cards(data):
        ch = changes.get((g, sec["id"], card["title"]))
        flag = card.get("digest")
        payload = flag if isinstance(flag, dict) else {}
        forced = flag is True or isinstance(flag, dict)
        if not ch and not forced:
            continue
        kind = ch.get("kind") if ch else "flag"
        status = card.get("status")
        is_incl, why = rules.classify(card)
        hits = rules.watch_hits(card)
        ev_date = first_date_in(card.get("date"), None)
        in_window = bool(ev_date and prev <= ev_date <= cut)
        name, code = sec_names(cfg, sec["id"], sec.get("title"), card["title"])
        src = card.get("source") or {}
        status_class = ("in_force" if status in rules.trigger else
                        "upcoming" if status in rules.upcoming else "other")
        ev = {
            "group": g, "sec": sec["id"], "jur": name, "code": code,
            "title": clean(card["title"]), "status": status,
            "status_label": STATUS_LABEL.get(status, status or "—"),
            "status_class": status_class,
            "date_text": clean(card.get("date")), "event_date": ev_date or cut,
            "desc": clean(card.get("desc")), "detail": card.get("detail") or "",
            "tags": [clean(t) for t in (card.get("tags") or [])],
            "source_label": clean(src.get("label")), "url": (src.get("url") or "").strip(),
            "level": clean(src.get("level")), "verified": clean(card.get("verified")),
            "verified_approx": bool(card.get("verifiedApprox")),
            "kind": kind, "backfill": bool(ch and ch.get("note")), "hits": hits,
            "payload": payload,
            # выдержки входят в ключ события, только если упоминание само вызывает выпуск
            "watch_hash": (hashlib.sha1("\n".join(sorted(hits)).encode("utf-8")).hexdigest()[:8]
                           if hits and rules.watch_alone else ""),
        }
        ev["key"] = event_key(ev)

        # --- решение
        if flag is False:
            decision, reason = "skip", "в карточке стоит digest: false"
        elif forced:
            decision, reason = "key", "в карточке стоит отметка digest"
        elif hits and rules.watch_alone:
            decision, reason = "key", "упомянуто наименование из списка наблюдения"
        elif g in rules.groups and is_incl and status_class == "in_force" and (kind == "new" or in_window):
            decision, reason = "key", why + ("; новая карточка" if kind == "new" else "; событие в периоде среза")
        elif g in rules.groups and is_incl and status_class == "upcoming" and (kind == "new" or in_window):
            decision, reason = "upcoming", why + "; мера ещё не принята"
        else:
            decision = "other"
            if g not in rules.groups:
                reason = "раздел вне периметра включений"
            elif not is_incl:
                reason = why
            elif status_class == "other":
                reason = f"статус «{ev['status_label']}»"
            else:
                reason = "обновление старой карточки: дата события вне периода среза"
        if decision == "key" and not force and ev["key"] in seen:
            decision, reason = "done", "уже вошло в дайджест " + str(seen[ev["key"]].get("digest", ""))

        log.append({"decision": decision, "reason": reason, "kind": kind, "jur": name,
                    "group": g, "status": ev["status_label"], "title": ev["title"], "key": ev["key"]})
        if decision == "key":
            key_events.append(ev)
        elif decision == "upcoming":
            upcoming.append(ev)
        elif decision in ("other", "done"):
            others.append(ev)

    key_events.sort(key=lambda e: (0 if e["hits"] else 1, -e["event_date"].toordinal(), e["jur"]))
    upcoming.sort(key=lambda e: (-e["event_date"].toordinal(), e["jur"]))
    return key_events, upcoming, others, log


# ----------------------------------------------------------------- модель дайджеста

def P(text):
    return {"t": "p", "text": text}


def H(text):
    return {"t": "h", "text": text}


def LI(text, n=None):
    u = {"t": "li", "ordered": n is not None, "text": text}
    if n is not None:
        u["n"] = n
    return u


def NOTE(text):
    return {"t": "note", "text": text}


def CALL(text, label=None):
    return {"t": "callout", "text": text, "label": label}


def SRC(label, url):
    return {"t": "src", "text": label, "url": url}


def qr_data_uri(url):
    try:
        import segno
    except ImportError:
        return None
    try:
        return segno.make(url, error="m").svg_data_uri(dark="#0f4a49", light="#ffffff", scale=6, border=2)
    except Exception:
        return None


def domain(url):
    m = re.match(r"https?://([^/]+)/?", url or "")
    return re.sub(r"^www\.", "", m.group(1)) if m else ""


def event_slides(ev, data, cfg, idx, total):
    """Слайды одного события: разделитель, суть, состав мер, выводы и сроки, упоминания."""
    watch_label = cfg["watchlist"]["label"]
    head = f"{ev['jur']}, {fmt(ev['event_date'])}"
    slides = []
    slides.append({
        "type": "divider", "title": ev["jur"],
        "sub": ("Раздел %d из %d · " % (idx, total) if total > 1 else "") + fmt_long(ev["event_date"]),
        "qr": qr_data_uri(ev["url"]) if ev["url"] else None,
        "qr_caption": "Первоисточник" if ev["url"] else None,
        "qr_sub": domain(ev["url"]),
    })

    blocks = split_blocks(ev["detail"])
    sources_block = None
    practical, gaps, body, context = [], [], [], []
    for b in blocks:
        if b["head"] is None and SOURCES_RE.match(b["body"]):
            sources_block = SOURCES_RE.sub("", b["body"])
        elif b["head"] and PRACTICAL_RE.match(b["head"]):
            practical.append(b)
        elif b["head"] and CONTEXT_RE.match(b["head"]):
            context.append(b)
        elif b["head"] and GAP_RE.match(b["head"]):
            gaps.append(b)
        else:
            body.append(b)

    # --- 1. суть
    units = [H(ev["title"])]
    facts = f"Статус: {ev['status_label'].lower()}. Дата: {ev['date_text'] or fmt(ev['event_date'])}."
    units.append(P(facts))
    if ev["desc"]:
        units.extend(body_units(ev["desc"], 700))
    if ev["backfill"]:
        units.append(NOTE("Событие внесено в карту с опозданием (восполнение пропуска)."))
    slides.append({"type": "content", "title": f"{head}: что произошло", "units": units})

    # --- 2. упоминания по списку наблюдения — сразу после сути
    if ev["hits"]:
        units = [P(f"Ниже — дословно все места карточки, где упомянуты компании и лица из списка наблюдения ({watch_label}). "
                   "Это выдержки из карты, а не новая оценка.")]
        for s in ev["hits"]:
            if s.rstrip(".") == ev["title"].rstrip("."):
                continue
            units.append(CALL(s))
        units.append(NOTE("Развёрнутая оценка последствий для группы ведётся в закрытом разделе Комплаенс-карты."))
        slides.append({"type": "content", "title": f"{head}: что относится к {watch_label}", "units": units})

    # --- 3. состав мер
    units = []
    for b in body:
        if b["head"]:
            units.append(H(cap(b["head"])))
            if b["note"]:
                units.append(NOTE(b["note"][:1].upper() + b["note"][1:] + "."))
        units.extend(body_units(b["body"]))
    for extra in ev["payload"].get("lists", []) if ev["payload"] else []:
        units.append(H(clean(extra.get("title", "Перечень"))))
        for n, item in enumerate(extra.get("items", []), 1):
            units.append(LI(clean(item), n))
    if units:
        slides.append({"type": "content", "title": f"{head}: состав мер", "units": units})

    # --- 4. практические выводы, пробелы, сроки
    units = []
    if practical:
        units.append(H("Что это значит на практике"))
        for b in practical:
            units.extend(body_units(b["body"]))
    for key, ttl in (("conclusions", "Выводы"), ("forecast", "Прогноз на квартал (оценка, не факт)")):
        items = ev["payload"].get(key) if ev["payload"] else None
        if items:
            units.append(H(ttl))
            units.extend(LI(clean(x)) for x in items)
    dls = related_deadlines(ev, data)
    if dls:
        units.append(H("Сроки по этой мере"))
        for d in dls:
            units.append(LI(d))
    if gaps:
        units.append(H("Что не подтверждено"))
        for b in gaps:
            units.extend(body_units(b["body"]))
    for b in context:
        units.append(H(cap(b["head"])))
        units.extend(body_units(b["body"]))
    if units:
        slides.append({"type": "content", "title": f"{head}: выводы и сроки", "units": units})

    ev["sources_block"] = sources_block
    return slides


def related_deadlines(ev, data):
    out = []
    for d in (data.get("DEADLINES") or {}).get("items", []):
        if d.get("group") == ev["group"] and d.get("sec") == ev["sec"] and clean(d.get("title")) == ev["title"]:
            out.append(deadline_line(d, with_jur=False))
    return out


def deadline_line(d, with_jur=True):
    try:
        day = dt.date.fromisoformat(d["date"])
        ds = fmt(day)
    except Exception:
        ds = str(d.get("date"))
    approx = "≈ " if d.get("approx") else ""
    jur = (clean(d.get("jur")) + ". ") if with_jur and d.get("jur") else ""
    return f"{approx}{ds} — {jur}{clean(d.get('text'))}"


def build_model(data, cfg, key_events, upcoming, others, now):
    meta = data["MAP_META"]
    cut = parse_ru_date(meta["cut"])
    prev = parse_ru_date(meta.get("prev"))
    rev = meta.get("rev")
    watch = any(e["hits"] for e in key_events)
    watch_label = cfg["watchlist"]["label"]
    header = cfg["header_text"].format(version=cfg["map_version"])
    cut_text = fmt(cut) + (f" (редакция {rev})" if rev else "")

    jurs = []
    for e in key_events:
        if e["jur"] not in jurs:
            jurs.append(e["jur"])
    if len(key_events) == 1:
        e = key_events[0]
        description = f"{cfg['unit_name']} — {e['jur']}: {bare_title(e)}"
        acts = f"{e['jur']} — {e['date_text'] or fmt(e['event_date'])}"
    else:
        description = f"{cfg['unit_name']} — новые включения в санкционные списки: {', '.join(jurs)}"
        acts = "; ".join(f"{e['jur']} — {fmt(e['event_date'])}" for e in key_events)
    if len(description) > 260:
        description = description[:257].rsplit(" ", 1)[0] + "…"
    if len(acts) > 300:
        acts = acts[:297].rsplit(" ", 1)[0] + "…"

    slides = [{"type": "title", "description": description, "acts": acts}]

    # --- главное
    units = [P(f"Дайджест собран автоматически по срезу Комплаенс-карты от {cut_text}"
               + (f"; предыдущий срез — {fmt(prev)}." if prev and prev != cut else "."))]
    units.append(H("Новые включения в санкционные списки"))
    for e in key_events:
        t = bare_title(e)
        units.append(LI(f"{e['jur']}, {fmt(e['event_date'])}: {sentence(t)} Статус: {e['status_label'].lower()}."))
    for e in key_events:
        if e["hits"]:
            quote = next((h for h in e["hits"] if h.rstrip(".") != e["title"].rstrip(".")), e["hits"][0])
            units.append(CALL(quote, f"Касается {watch_label} — {e['jur']}, {fmt(e['event_date'])}"))
    if upcoming and cfg["content"].get("include_upcoming", True):
        units.append(H("На подходе (решение ещё не принято)"))
        for e in upcoming:
            units.append(LI(f"{e['jur']}: {sentence(bare_title(e))} Статус: {e['status_label'].lower()}."))
    slides.append({"type": "content", "title": "Главное", "units": units})

    # --- сводка для руководства
    ex = data.get("EXEC_SUMMARY") or {}
    if cfg["content"].get("include_exec_summary", True) and (ex.get("points") or ex.get("actions")):
        units = [NOTE(f"Сводка из Комплаенс-карты на {cut_text}: общая картина по всем юрисдикциям, не только по новым включениям.")]
        for p in ex.get("points", []):
            units.append(H(clean(p.get("h"))))
            units.append(P(clean(p.get("t"))))
        if ex.get("actions"):
            units.append(H("Что сделать"))
            for n, a in enumerate(ex["actions"], 1):
                units.append(LI(clean(a), n))
        slides.append({"type": "content", "title": "Общая картина и действия", "units": units})

    # --- события
    for i, e in enumerate(key_events, 1):
        slides.extend(event_slides(e, data, cfg, i, len(key_events)))

    # --- контрольные даты
    days = int(cfg["content"].get("deadline_days_ahead", 30))
    dls = []
    for d in (data.get("DEADLINES") or {}).get("items", []):
        try:
            day = dt.date.fromisoformat(d["date"])
        except Exception:
            continue
        if cut <= day <= cut + dt.timedelta(days=days):
            dls.append((day, deadline_line(d)))
    if dls:
        dls.sort(key=lambda x: x[0])
        units = [NOTE(f"Сроки из Комплаенс-карты на ближайшие {days} дней от даты среза. Знак ≈ означает ориентировочную дату.")]
        units.extend(LI(t) for _, t in dls)
        slides.append({"type": "content", "title": "Контрольные даты", "units": units})

    # --- прочие изменения
    if others and cfg["content"].get("include_other_changes", True):
        mx = int(cfg["content"].get("max_other_changes", 18))
        units = [NOTE("Остальные изменения в карте за период: продления, лицензии, экспортный контроль, российские меры. "
                      "Подробности — в самой карте.")]
        shown = 0
        for e in sorted(others, key=lambda x: (0 if x["kind"] == "new" else 1)):
            if shown >= mx:
                break
            label = e["jur"] if e["group"] == "SECTIONS" else GROUP_LABEL[e["group"]]
            named = JUR_PREFIX.match(e["title"]) or e["title"].startswith(label)
            units.append(LI(e["title"] if named else f"{label}: {e['title']}"))
            shown += 1
        if len(others) > shown:
            units.append(NOTE(f"И ещё {len(others) - shown} изменений — см. блок «Что изменилось» в карте."))
        slides.append({"type": "content", "title": "Также изменилось в карте", "units": units})

    # --- заключение комплаенс-офицера (по настройке)
    oc = data.get("OFFICER_CONCLUSION") or {}
    if cfg["content"].get("include_officer_conclusion") and oc.get("text"):
        units = []
        for b in split_blocks(oc["text"]):
            if b["head"]:
                units.append(H(cap(b["head"])))
            units.extend(body_units(b["body"]))
        slides.append({"type": "content", "title": "Заключение комплаенс-офицера", "units": units})

    # --- источники и верификация
    units = [P("Дайджест не содержит новых проверок: все сведения взяты из Комплаенс-карты в том виде, "
               f"в каком они записаны на {cut_text}. Уровень источника и дата сверки указаны по карте.")]
    weak = []
    for e in key_events:
        units.append(H(f"{e['jur']}, {fmt(e['event_date'])}"))
        line = e["source_label"] or "источник в карте не указан"
        lvl = f" Уровень источника: {e['level']}." if e["level"] else ""
        ver = (f" Сверено: {'≈ ' if e['verified_approx'] else ''}{e['verified']}." if e["verified"] else " Дата сверки в карте не указана.")
        units.append(P(line.rstrip(".") + "." + lvl + ver))
        if e["url"]:
            units.append(SRC(e["url"], e["url"]))
        if e.get("sources_block"):
            units.extend(body_units("По карте: " + e["sources_block"], 640, lists=False))
        for q in (e["payload"].get("quotes", []) if e["payload"] else []):
            units.append(P("Дословно: «" + clean(q.get("text")).strip("«»\"") + "»"))
            if q.get("url"):
                units.append(SRC(q["url"], q["url"]))
        lv = (e["level"] or "").upper()
        if not e["url"] or not lv or "A" not in lv or "требует сверки" in e["tags"] or e["verified_approx"]:
            why = []
            if not e["url"]:
                why.append("нет прямой ссылки")
            if lv and "A" not in lv:
                why.append(f"уровень источника {e['level']}, не первоисточник")
            if not lv:
                why.append("уровень источника не указан")
            if "требует сверки" in e["tags"]:
                why.append("карточка помечена «требует сверки»")
            if e["verified_approx"]:
                why.append("дата сверки ориентировочная")
            weak.append(f"{e['jur']}, {fmt(e['event_date'])}: " + "; ".join(why) + ".")
    if weak:
        units.append(H("Требует сверки перед использованием"))
        units.extend(LI(w) for w in weak)
    units.append(NOTE("Это аналитический материал, а не юридическое заключение. Перед принятием решений "
                      "сверьте ключевые реквизиты с официальным текстом акта."))
    slides.append({"type": "content", "title": "Источники и верификация", "units": units})
    slides.append({"type": "final"})

    return {
        "meta": {
            "header": header, "map_url": cfg["map_url"].replace("https://", "").replace("http://", ""),
            "map_href": cfg["map_url"], "capsule": f"{fmt(now.date())}, {cfg['city']}",
            "confidential": cfg["confidential_text"], "unit": cfg["unit_name"],
            "digest_title": cfg["digest_title"], "contact_text": cfg["contact_text"],
            "contact_email": cfg["contact_email"],
        },
        "slides": slides,
    }


# ----------------------------------------------------------------- вёрстка PDF

def data_uri(path, mime):
    return f"data:{mime};base64," + base64.b64encode(Path(path).read_bytes()).decode("ascii")


def find_chrome():
    env = os.environ.get("CHROME_BIN")
    if env and Path(env).exists():
        return env
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"):
        p = shutil.which(name)
        if p:
            return p
    for base in ("/opt/pw-browsers", str(Path.home() / ".cache/ms-playwright")):
        for cand in sorted(Path(base).glob("chromium-*/chrome-linux*/chrome"), reverse=True):
            return str(cand)
    return None


def render_html(model, assets):
    tpl = (HERE / "template.html").read_text(encoding="utf-8")
    fonts = {
        "FONT_REGULAR": data_uri(assets / "fonts/Inter-Regular.woff2", "font/woff2"),
        "FONT_ITALIC": data_uri(assets / "fonts/Inter-Italic.woff2", "font/woff2"),
        "FONT_SEMIBOLD": data_uri(assets / "fonts/Inter-SemiBold.woff2", "font/woff2"),
        "FONT_BOLD": data_uri(assets / "fonts/Inter-Bold.woff2", "font/woff2"),
        # логотип и фон необязательны: без этих файлов слайды собираются в нейтральном оформлении
        "IMG_LOGO": data_uri(assets / "logo_white.png", "image/png") if (assets / "logo_white.png").exists() else "",
        "IMG_PHOTO": data_uri(assets / "building.jpg", "image/jpeg") if (assets / "building.jpg").exists() else "",
    }
    out = tpl
    for k, v in fonts.items():
        out = out.replace("{{" + k + "}}", v)
    payload = json.dumps(model, ensure_ascii=False).replace("</", "<\\/")
    return out.replace("{{MODEL_JSON}}", payload)


def run_chrome(chrome, args, timeout=180):
    base = [chrome, "--headless=new", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
            "--hide-scrollbars", "--force-color-profile=srgb", "--virtual-time-budget=20000",
            "--run-all-compositor-stages-before-draw"]
    return subprocess.run(base + args, capture_output=True, timeout=timeout)


def make_pdf(model, assets, out_pdf):
    chrome = find_chrome()
    if not chrome:
        raise RuntimeError("не найден Chrome/Chromium (переменная CHROME_BIN)")
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "digest.html"
        page.write_text(render_html(model, assets), encoding="utf-8")
        url = page.as_uri()
        # 1) проверочный прогон: вёрстка отработала, ничего не обрезано
        r = run_chrome(chrome, ["--dump-dom", url])
        dom = r.stdout.decode("utf-8", "replace")
        m = re.search(r'<html[^>]*data-ready="1"[^>]*>', dom)
        if not m:
            raise RuntimeError("вёрстка не завершилась: " + r.stderr.decode("utf-8", "replace")[-400:])
        tag = m.group(0)
        slides = int(re.search(r'data-slides="(\d+)"', tag).group(1))
        overflow = int(re.search(r'data-overflow="(\d+)"', tag).group(1))
        if overflow:
            raise RuntimeError(f"текст не поместился на {overflow} слайдах — вёрстка остановлена")
        # 2) печать
        raw = Path(tmp) / "raw.pdf"
        r = run_chrome(chrome, ["--no-pdf-header-footer", "--print-to-pdf-no-header",
                                f"--print-to-pdf={raw}", url])
        if not raw.exists() or raw.stat().st_size < 2000:
            raise RuntimeError("Chrome не создал PDF: " + r.stderr.decode("utf-8", "replace")[-400:])
        shutil.copyfile(raw, out_pdf)
    return slides


def finalize_pdf(path, title, expect_pages, password=None):
    """Проверяет число и размер страниц, записывает свойства, при наличии пароля шифрует (AES-256)."""
    try:
        import pikepdf
    except ImportError:
        if password:
            raise RuntimeError("для защиты паролем нужна библиотека pikepdf")
        return None
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        n = len(pdf.pages)
        if n != expect_pages:
            raise RuntimeError(f"в PDF {n} страниц, по вёрстке должно быть {expect_pages}")
        for i, pg in enumerate(pdf.pages, 1):
            box = [float(x) for x in pg.mediabox]
            w, h = box[2] - box[0], box[3] - box[1]
            if abs(w - 960) > 2 or abs(h - 540) > 2:
                raise RuntimeError(f"страница {i}: размер {w:.0f}×{h:.0f}, ожидается 960×540")
        pdf.docinfo["/Title"] = title
        pdf.docinfo["/Author"] = "Санкционный комплаенс"
        pdf.docinfo["/Subject"] = "Автоматическая сборка на базе Комплаенс-карты"
        kw = {}
        if password:
            kw["encryption"] = pikepdf.Encryption(user=password, owner=password, R=6)
        pdf.save(path, **kw)
    return n


# ----------------------------------------------------------------- состояние и отчёт

def load_ledger(path):
    p = Path(path)
    if p.exists():
        led = json.loads(p.read_text(encoding="utf-8"))
        led.setdefault("events", {})
        led.setdefault("digests", [])
        return led
    return {"version": 1, "events": {}, "digests": []}


def file_name(cfg, key_events, cut, rev, taken):
    codes = []
    for e in key_events:
        if e["code"] not in codes:
            codes.append(e["code"])
    dates = {e["event_date"] for e in key_events}
    day = dates.pop() if len(dates) == 1 else cut
    name = "Sanctions_Digest_" + "_".join(codes[:4]) + "_" + fmt(day)
    if any(e["hits"] for e in key_events):
        name += "_" + cfg["watchlist"]["file_suffix"]
    cand, n = name, 1
    while cand + ".pdf" in taken:
        n += 1
        cand = f"{name}_v{n}"
    return cand + ".pdf"


def write_report(path, data, log, result):
    meta = data["MAP_META"]
    mark = {"key": "В ДАЙДЖЕСТ", "upcoming": "на подходе", "other": "прочее", "done": "уже выпущено", "skip": "пропуск"}
    lines = [f"## Санкционный дайджест — срез {meta.get('cut')}" + (f" (редакция {meta['rev']})" if meta.get("rev") else ""), ""]
    lines.append(result)
    lines.append("")
    lines.append("| Решение | Раздел | Изменение | Статус | Карточка | Почему |")
    lines.append("|---|---|---|---|---|---|")
    order = {"key": 0, "upcoming": 1, "done": 2, "other": 3, "skip": 4}
    for r in sorted(log, key=lambda x: order.get(x["decision"], 9)):
        t = r["title"].replace("|", "\\|")
        if len(t) > 110:
            t = t[:107] + "…"
        lines.append(f"| {mark.get(r['decision'], r['decision'])} | {r['jur']} | "
                     f"{ {'new': 'новая', 'upd': 'обновлена', 'flag': 'отметка'}.get(r['kind'], r['kind']) } | "
                     f"{r['status']} | {t} | {r['reason']} |")
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def update_readme(path, ledger):
    """Обновляет в README перечень выпущенных дайджестов (между метками digests:start / digests:end)."""
    p = Path(path)
    if not p.exists():
        return
    text = p.read_text(encoding="utf-8")
    a, b = "<!-- digests:start -->", "<!-- digests:end -->"
    if a not in text or b not in text:
        return
    rows = ["| Дата выпуска | Срез карты | Файл | Страниц | События |", "|---|---|---|---|---|"]
    for d in sorted(ledger.get("digests", []), key=lambda x: x.get("built_at", ""), reverse=True):
        when = d.get("built_at", "")[:16].replace("T", " ")
        cut = d.get("cut", "") + (f" (ред. {d['rev']})" if d.get("rev") else "")
        lock = " (под паролем)" if d.get("protected") else ""
        ev = "<br>".join(t.replace("|", "\\|") for t in d.get("titles", []))
        rows.append(f"| {when} | {cut} | [{d['file']}](out/{d['file']}){lock} | {d.get('pages', '')} | {ev} |")
    if len(rows) == 2:
        rows = ["Пока не выпущено ни одного дайджеста."]
    head, rest = text.split(a, 1)
    tail = rest.split(b, 1)[1]
    p.write_text(head + a + "\n" + "\n".join(rows) + "\n" + b + tail, encoding="utf-8")


def selftest(cfg, assets):
    """Проверка вёрстки на учебных данных: Chrome, шрифты и сборка PDF работают в этой среде."""
    t = "Учебная страна: 3 новые позиции — проверка вёрстки"
    card = {"status": "active", "date": "01.01.2030", "title": t,
            "desc": "Учебная запись для проверки вёрстки. К реальным санкциям отношения не имеет.",
            "detail": "ОРГАНИЗАЦИИ. Первая (г. Москва); Вторая (г. Иркутск); Третья (г. Казань).\n\n"
                      "ПРАКТИЧЕСКИЙ ВЫВОД: (1) проверить платежи; (2) сверить перечень.",
            "tags": [], "source": {"label": "учебный источник", "url": "https://example.org/", "level": "A"},
            "verified": "01.01.2030"}
    data = {"SECTIONS": [{"id": "uk", "title": "Учебный раздел", "items": [card]}],
            "MAP_META": {"cut": "01.01.2030", "prev": "31.12.2029"},
            "CHANGELOG": {"items": [{"group": "SECTIONS", "sec": "uk", "title": t, "kind": "new"}]},
            "DEADLINES": {"items": []}, "EXEC_SUMMARY": {}}
    key, up, other, _ = collect_events(data, cfg, Rules(cfg), {"events": {}, "digests": []})
    model = build_model(data, cfg, key, up, other, dt.datetime(2030, 1, 1, 12, 0))
    with tempfile.TemporaryDirectory() as tmp:
        pdf = Path(tmp) / "selftest.pdf"
        slides = make_pdf(model, Path(assets), pdf)
        finalize_pdf(pdf, "selftest", slides, None)
        size = pdf.stat().st_size
    print(f"Проверка вёрстки пройдена: {slides} стр., {size // 1024} КБ.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Выпуск санкционного дайджеста на основе Комплаенс-карты")
    ap.add_argument("--selftest", action="store_true", help="проверить вёрстку на учебных данных и выйти")
    ap.add_argument("--map", help="путь к index.html карты")
    ap.add_argument("--state", help="журнал выпущенных событий (ledger.json)")
    ap.add_argument("--out", help="папка для PDF")
    ap.add_argument("--config", default=str(HERE / "config.json"))
    ap.add_argument("--assets", default=str(HERE / "assets"))
    ap.add_argument("--report", default=None, help="куда записать отчёт о решениях (Markdown)")
    ap.add_argument("--readme", default=None, help="README с перечнем выпусков (обновляется после выпуска)")
    ap.add_argument("--notes", default=None, help="куда записать краткое описание выпуска (Markdown)")
    ap.add_argument("--force", action="store_true", help="выпустить заново, не глядя в журнал")
    ap.add_argument("--dry-run", action="store_true", help="только показать решения, PDF не собирать")
    ap.add_argument("--mark-seen", metavar="ПОМЕТКА", default=None,
                    help="не собирать PDF, а отметить текущие ключевые события как уже учтённые")
    ap.add_argument("--password-env", default="DIGEST_PDF_PASSWORD",
                    help="имя переменной окружения с паролем PDF (значение нигде не выводится)")
    ap.add_argument("--now", default=None, help="дата и время выпуска в формате ISO (для проверок)")
    a = ap.parse_args(argv)

    cfg = json.loads(Path(a.config).read_text(encoding="utf-8"))
    if a.selftest:
        try:
            return selftest(cfg, a.assets)
        except Exception as e:  # noqa: BLE001
            print(f"ОШИБКА: проверка вёрстки не пройдена — {e}", file=sys.stderr)
            return 3
    if not (a.map and a.state and a.out):
        ap.error("нужны параметры --map, --state и --out")
    try:
        data = read_map(a.map)
    except MapFormatError as e:
        print(f"ОШИБКА: карта не читается — {e}", file=sys.stderr)
        return 2
    rules = Rules(cfg)
    ledger = load_ledger(a.state)
    key_events, upcoming, others, log = collect_events(data, cfg, rules, ledger, force=a.force)
    meta = data["MAP_META"]
    cut = parse_ru_date(meta["cut"])
    tz = dt.timezone(dt.timedelta(hours=cfg.get("timezone_offset_hours", 0)))
    now = dt.datetime.fromisoformat(a.now) if a.now else dt.datetime.now(tz)
    report = a.report or str(Path(a.out) / "last-run.md")
    Path(a.out).mkdir(parents=True, exist_ok=True)

    def out(result, code=0, **extra):
        write_report(report, data, log, result)
        print(result)
        gh = os.environ.get("GITHUB_OUTPUT")
        if gh:
            with open(gh, "a", encoding="utf-8") as f:
                for k, v in extra.items():
                    f.write(f"{k}={v}\n")
        return code

    if not key_events:
        return out(f"Ключевых событий нет: новых включений в срезе {meta['cut']} не найдено "
                   f"(изменений в карте — {len(log)}). Дайджест не выпускается.", released="false")
    if a.dry_run:
        return out(f"Проверочный запуск: ключевых событий — {len(key_events)}. PDF не собирался.", released="false")

    if a.mark_seen:
        stamp = now.isoformat(timespec="seconds")
        for e in key_events:
            ledger["events"][e["key"]] = {"title": e["title"], "jur": e["jur"], "event_date": fmt(e["event_date"]),
                                          "status": e["status"], "cut": meta["cut"], "digest": a.mark_seen,
                                          "at": stamp, "watch": bool(e["hits"])}
        Path(a.state).write_text(json.dumps(ledger, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        return out(f"Отмечено как уже учтённые: {len(key_events)} событий ({a.mark_seen}). PDF не собирался.",
                   released="false")

    mode = cfg.get("publish", {}).get("mode", "encrypted")
    password = os.environ.get(a.password_env) or None
    if mode == "encrypted" and not password:
        return out("Дайджест НЕ выпущен: в настройках включена защита паролем, а пароль не задан "
                   f"(секрет {a.password_env}). Задайте пароль либо переключите publish.mode на open.",
                   released="false", blocked="true")

    model = build_model(data, cfg, key_events, upcoming, others, now)
    taken = {d["file"] for d in ledger["digests"]} | {p.name for p in Path(a.out).glob("*.pdf")}
    if a.force:
        taken = set()
    name = file_name(cfg, key_events, cut, meta.get("rev"), taken)
    pdf = Path(a.out) / name
    try:
        slides = make_pdf(model, Path(a.assets), pdf)
        title = f"{cfg['digest_title']} — " + ", ".join(dict.fromkeys(e["jur"] for e in key_events)) + f" — срез {meta['cut']}"
        finalize_pdf(pdf, title, slides, password if mode == "encrypted" else None)
    except Exception as e:  # noqa: BLE001 — любая ошибка вёрстки останавливает выпуск
        if pdf.exists():
            pdf.unlink()
        print(f"ОШИБКА вёрстки: {e}", file=sys.stderr)
        write_report(report, data, log, f"Дайджест НЕ выпущен: ошибка вёрстки — {e}")
        return 3

    stamp = now.isoformat(timespec="seconds")
    for e in key_events:
        ledger["events"][e["key"]] = {"title": e["title"], "jur": e["jur"], "event_date": fmt(e["event_date"]),
                                      "status": e["status"], "cut": meta["cut"], "digest": name, "at": stamp,
                                      "watch": bool(e["hits"])}
    ledger["digests"] = [d for d in ledger["digests"] if d["file"] != name]
    ledger["digests"].append({"file": name, "cut": meta["cut"], "rev": meta.get("rev"), "built_at": stamp,
                              "pages": slides, "events": [e["key"] for e in key_events],
                              "titles": [f"{e['jur']}, {fmt(e['event_date'])}: {e['title']}" for e in key_events],
                              "watch": any(e["hits"] for e in key_events),
                              "protected": bool(mode == "encrypted")})
    Path(a.state).write_text(json.dumps(ledger, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    if a.readme:
        update_readme(a.readme, ledger)
    if a.notes:
        lines = [f"Срез Комплаенс-карты: {meta['cut']}" + (f" (редакция {meta['rev']})" if meta.get("rev") else "") + ".", "",
                 "Ключевые события:"]
        lines += [f"- {e['jur']}, {fmt(e['event_date'])}: {e['title']}" for e in key_events]
        if mode == "encrypted":
            lines += ["", "Файл защищён паролем."]
        Path(a.notes).write_text("\n".join(lines) + "\n", encoding="utf-8")
    watch = " Касается " + cfg["watchlist"]["label"] + "." if any(e["hits"] for e in key_events) else ""
    return out(f"Дайджест выпущен: {name} — {slides} стр., ключевых событий: {len(key_events)}.{watch}"
               + (" Файл защищён паролем." if mode == "encrypted" else ""),
               released="true", file=name, watch=str(any(e["hits"] for e in key_events)).lower())


if __name__ == "__main__":
    sys.exit(main())
