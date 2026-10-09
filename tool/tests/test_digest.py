# -*- coding: utf-8 -*-
"""Самопроверка сборщика дайджеста. Запуск: python -m unittest discover -s tool/tests

Проверяется логика без вёрстки PDF: чтение карты, отбор событий, разбор текста карточек.
Данные в тестах учебные, к реальным санкциям отношения не имеют.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL))
import build_digest as bd  # noqa: E402

CFG = json.loads((TOOL / "config.json").read_text(encoding="utf-8"))


def card(title, status="active", date="08.10.2026", desc="Описание.", detail="", tags=None, url="https://example.org/a", **kw):
    c = {"status": status, "date": date, "title": title, "desc": desc, "detail": detail,
         "tags": tags or [], "source": {"label": "Источник", "url": url, "level": "A"}, "verified": "09.10.2026"}
    c.update(kw)
    return c


def make_map(sections, changelog, export=None, cut="09.10.2026", prev="07.10.2026"):
    def js(name, value, kw="let"):
        return f"\n{kw} {name} = {json.dumps(value, ensure_ascii=False, indent=1)};\n"
    data = {
        "SECTIONS": sections,
        "EXPORT_CONTROL": export or [],
        "RUSSIAN_MEASURES": [],
        "MAP_META": {"version": 5, "cut": cut, "prev": prev},
        "CHANGELOG": {"from": prev, "to": cut, "items": changelog},
        "DEADLINES": {"items": []},
        "EXEC_SUMMARY": {"title": "Сводка", "points": [], "actions": []},
    }
    return "<html><body><script>\n/* data */" + "".join(js(k, v) for k, v in data.items()) + "</script></body></html>"


def write_map(text):
    f = tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8")
    f.write(text)
    f.close()
    return f.name


def ch(sec, title, kind="new", group="SECTIONS", status="active"):
    return {"group": group, "sec": sec, "secLabel": sec, "title": title, "status": status, "kind": kind}


class ReadMap(unittest.TestCase):
    def test_reads_declarations(self):
        t = "Тестовая страна: санкции против 5 физлиц"
        p = write_map(make_map([{"id": "uk", "title": "Великобритания", "items": [card(t)]}], [ch("uk", t)]))
        d = bd.read_map(p)
        self.assertEqual(d["MAP_META"]["cut"], "09.10.2026")
        self.assertEqual(len(list(bd.iter_cards(d))), 1)

    def test_broken_structure_is_reported(self):
        p = write_map("<html><script>\nlet SECTIONS = [;\n</script></html>")
        with self.assertRaises(bd.MapFormatError):
            bd.read_map(p)
        p = write_map("<html><script>\nlet NOTHING = 1;\n</script></html>")
        with self.assertRaises(bd.MapFormatError):
            bd.read_map(p)


class Classifier(unittest.TestCase):
    rules = bd.Rules(CFG)

    def check(self, title, expected, tags=None):
        got, why = self.rules.classify({"title": title, "tags": tags or []})
        self.assertEqual(got, expected, f"{title!r}: {why}")

    def test_inclusions(self):
        for t in ["UK: 38 новых позиций — две нефтяные компании и 12 танкеров",
                  "ЕС: санкции против 10 физлиц и 17 организаций",
                  "Австралия — 106 новых позиций: 33 физлица, 35 организаций и 38 судов",
                  "Банк дополнительно назначен по иранской программе",
                  "SDN-листинг двух компаний",
                  "ЕС: послы одобрили ≈1 650 новых назначений",
                  "Запрет распространён ещё на 33 российских кредитно-финансовых организации",
                  "ЕС: транзакционный запрет на 11 крипто-платформ",
                  "UK: в список внесены три СПГ-газовоза и 12 танкеров",
                  "Канада: санкции против 8 российских должностных лиц"]:
            self.check(t, True)
        self.check("Группа компаний — санкции за компоненты", True, tags=["Листинги"])

    def test_not_inclusions(self):
        for t in ["GL 13S — административные операции: выпущена 08.10.2026",
                  "ЕС продлил на год режим санкций: в списке 80 физических лиц и 20 организаций",
                  "OFAC исключил из SDN двух лиц",
                  "Делистинг 4 индийских компаний",
                  "Реестр Russia designations — динамика по уведомлениям",
                  "Price cap ЕС на нефть — $44,10/барр.",
                  "OFAC 02–06.10.2026: действий по российской программе нет",
                  "Великобритания: две генеральные торговые лицензии на перевозку СПГ",
                  "22-й пакет санкций ЕС — обсуждение началось",
                  "Второй иск ЦБ РФ в CJEU",
                  "Список судов теневого флота — 673 судна"]:
            self.check(t, False)

    def test_date_is_not_a_count(self):
        self.check("Мера действует до 31.12.2026 лиц не затрагивает", False)


class Events(unittest.TestCase):
    def run_collect(self, sections, changelog, ledger=None, export=None, force=False):
        p = write_map(make_map(sections, changelog, export=export))
        d = bd.read_map(p)
        return bd.collect_events(d, CFG, bd.Rules(CFG), ledger or {"events": {}, "digests": []}, force=force)

    def test_new_inclusion_is_key_event(self):
        t = "UK: 12 новых позиций — банки и суда"
        key, up, other, log = self.run_collect([{"id": "uk", "title": "Великобритания", "items": [card(t)]}], [ch("uk", t)])
        self.assertEqual([e["title"] for e in key], [t])
        self.assertEqual(key[0]["jur"], "Великобритания")

    def test_pending_inclusion_is_upcoming_not_key(self):
        t = "ЕС: послы одобрили 100 новых назначений"
        key, up, other, log = self.run_collect(
            [{"id": "eu", "title": "ЕС", "items": [card(t, status="pending")]}], [ch("eu", t, status="pending")])
        self.assertEqual(key, [])
        self.assertEqual(len(up), 1)

    def test_licence_and_old_card_are_not_events(self):
        a = "GL 99 — лицензия продлена"
        b = "Старый пакет: 20 физлиц"
        key, up, other, log = self.run_collect(
            [{"id": "us", "title": "США", "items": [card(a), card(b, date="07.05.2026")]}],
            [ch("us", a), ch("us", b, kind="upd")])
        self.assertEqual(key, [])
        self.assertEqual(len(other), 2)

    def test_untouched_card_is_ignored(self):
        t = "UK: 12 новых позиций"
        key, up, other, log = self.run_collect([{"id": "uk", "title": "Великобритания", "items": [card(t)]}], [])
        self.assertEqual((key, up, other, log), ([], [], [], []))

    def test_watchlist_mention_alone_is_not_an_event(self):
        # лицензия или новость с упоминанием ИНК дайджестом не становится
        a = "OFSI: генеральная лицензия на сворачивание операций"
        b = "Экспортный контроль: уточнение порядка"
        key, up, other, log = self.run_collect(
            [{"id": "uk", "title": "Великобритания", "items": [card(a, desc="Лицензия касается АО «ИНК-Капитал».")]}],
            [ch("uk", a), ch("ec-us", b, kind="upd", group="EXPORT_CONTROL")],
            export=[{"id": "ec-us", "title": "США", "items": [card(b, desc="Мера затрагивает АО «ИНК-Капитал».")]}])
        self.assertEqual(key, [])
        self.assertEqual(len(other), 2)

    def test_inclusion_with_watchlist_mention_is_marked(self):
        t = "UK: 12 новых позиций — нефтяные компании"
        key, up, other, log = self.run_collect(
            [{"id": "uk", "title": "Великобритания", "items": [card(t, desc="В пакете АО «ИНК-Капитал».")]}], [ch("uk", t)])
        self.assertEqual(len(key), 1)
        self.assertTrue(key[0]["hits"])
        self.assertTrue(bd.file_name(CFG, key, bd.parse_ru_date("09.10.2026"), None, set()).endswith("_INK.pdf"))

    def test_watchlist_can_trigger_alone_when_enabled(self):
        cfg = json.loads(json.dumps(CFG))
        cfg["watchlist"]["trigger_alone"] = True
        t = "Экспортный контроль: уточнение порядка"
        p = write_map(make_map([], [ch("ec-us", t, kind="upd", group="EXPORT_CONTROL")],
                               export=[{"id": "ec-us", "title": "США", "items": [card(t, desc="Мера затрагивает АО «ИНК-Капитал».")]}]))
        key, up, other, log = bd.collect_events(bd.read_map(p), cfg, bd.Rules(cfg), {"events": {}, "digests": []})
        self.assertEqual(len(key), 1)

    def test_watchlist_does_not_match_inside_other_words(self):
        rules = bd.Rules(CFG)
        self.assertEqual(rules.watch_hits({"title": "Поставки ЦИНКА и ВИНКЕЛЬ", "desc": "", "detail": ""}), [])
        self.assertTrue(rules.watch_hits({"title": "Проверить связь с ИНК.", "desc": "", "detail": ""}))

    def test_explicit_flags_win(self):
        a = "UK: 12 новых позиций"
        b = "Разъяснение регулятора"
        key, up, other, log = self.run_collect(
            [{"id": "uk", "title": "Великобритания", "items": [card(a, digest=False), card(b, digest=True)]}],
            [ch("uk", a)])
        self.assertEqual([e["title"] for e in key], [b])

    def test_ledger_prevents_second_release_and_force_overrides(self):
        t = "UK: 12 новых позиций — банки и суда"
        sec = [{"id": "uk", "title": "Великобритания", "items": [card(t)]}]
        key, *_ = self.run_collect(sec, [ch("uk", t)])
        ledger = {"events": {key[0]["key"]: {"digest": "x.pdf"}}, "digests": []}
        key2, up2, other2, log2 = self.run_collect(sec, [ch("uk", t, kind="upd")], ledger=ledger)
        self.assertEqual(key2, [])
        self.assertEqual(log2[0]["decision"], "done")
        key3, *_ = self.run_collect(sec, [ch("uk", t, kind="upd")], ledger=ledger, force=True)
        self.assertEqual(len(key3), 1)

    def test_retitled_card_keeps_same_key(self):
        a = card("UK: 12 новых позиций — банки и суда")
        b = card("UK: 12 новых позиций — банки, суда и трейдеры (уточнено)")
        k1, *_ = self.run_collect([{"id": "uk", "title": "Великобритания", "items": [a]}], [ch("uk", a["title"])])
        k2, *_ = self.run_collect([{"id": "uk", "title": "Великобритания", "items": [b]}], [ch("uk", b["title"])])
        self.assertEqual(k1[0]["key"], k2[0]["key"])

    def test_later_note_about_watchlist_name_does_not_reissue(self):
        base = "UK: 12 новых позиций"
        a = card(base, desc="В пакете АО «ИНК-Капитал».")
        b = card(base, desc="В пакете АО «ИНК-Капитал».", detail="ОБНОВЛЕНИЕ 12.10.2026: выпущена лицензия по АО «ИНК-Капитал».")
        k1, *_ = self.run_collect([{"id": "uk", "title": "Великобритания", "items": [a]}], [ch("uk", base)])
        k2, *_ = self.run_collect([{"id": "uk", "title": "Великобритания", "items": [b]}], [ch("uk", base, kind="upd")])
        self.assertEqual(k1[0]["key"], k2[0]["key"])


class Text(unittest.TestCase):
    def test_sentences_keep_abbreviations_and_initials(self):
        s = bd.split_sentences("Меры ввёл А.Б. Иванов в 2026 г. в Москве. Далее см. п. 5 Указа № 1. Итого 12 млн руб. выплачено!")
        self.assertEqual(len(s), 3)

    def test_blocks_and_lists(self):
        detail = ("Первоисточники (A): пресс-релиз.\n\n"
                  "ФИНАНСЫ. Банк А (RUS0001, Москва; также «А-банк»); Банк Б (RUS0002); Банк В (RUS0003). По пресс-релизу — три банка.\n\n"
                  "СУДА (по тексту уведомления): Alpha (9000001), Beta (9000002), Gamma (9000003), Delta (9000004).\n\n"
                  "ЧТО НЕ УСТАНОВЛЕНО: лицензии нет. РЕАКЦИЯ РОССИИ: заявление посольства.\n\n"
                  "ПРАКТИЧЕСКИЙ ВЫВОД: (1) проверить договоры; (2) сверить суда.\n\n"
                  "ПРЕДЫДУЩАЯ РЕДАКЦИЯ КАРТОЧКИ (срез 01.10.2026): старый текст.")
        blocks = bd.split_blocks(detail)
        heads = [b["head"] for b in blocks]
        self.assertEqual(heads, [None, "ФИНАНСЫ", "СУДА", "ЧТО НЕ УСТАНОВЛЕНО", "РЕАКЦИЯ РОССИИ", "ПРАКТИЧЕСКИЙ ВЫВОД"])
        fin = bd.body_units(blocks[1]["body"])
        self.assertEqual([u["t"] for u in fin], ["li", "li", "li", "p"])
        self.assertIn("также «А-банк»", fin[0]["text"])          # «;» внутри скобок не разрывает пункт
        ships = bd.body_units(blocks[2]["body"])
        self.assertEqual(len(ships), 4)
        steps = bd.body_units(blocks[5]["body"])
        self.assertEqual([(u["t"], u.get("n")) for u in steps], [("li", 1), ("li", 2)])

    def test_heading_case(self):
        self.assertEqual(bd.cap("ПОСТАВЩИКИ ВПК"), "Поставщики ВПК")
        self.assertEqual(bd.cap("РЕАКЦИЯ РОССИИ"), "Реакция России")
        self.assertEqual(bd.cap("ЧТО НЕ УСТАНОВЛЕНО"), "Что не установлено")

    def test_html_is_stripped(self):
        self.assertEqual(bd.clean("<strong>Вывод:</strong>&nbsp;текст<br>строка"), "Вывод: текст\nстрока")


class Model(unittest.TestCase):
    def test_model_contains_only_map_text(self):
        t = "UK: 12 новых позиций — банки и суда"
        p = write_map(make_map(
            [{"id": "uk", "title": "Великобритания",
              "items": [card(t, desc="В список внесены 12 позиций.", detail="ПРАКТИЧЕСКИЙ ВЫВОД: (1) проверить платежи; (2) сверить суда.")]}],
            [ch("uk", t)]))
        d = bd.read_map(p)
        key, up, other, log = bd.collect_events(d, CFG, bd.Rules(CFG), {"events": {}, "digests": []})
        import datetime as dt
        m = bd.build_model(d, CFG, key, up, other, dt.datetime(2026, 10, 9, 12, 0))
        types = [s["type"] for s in m["slides"]]
        self.assertEqual(types[0], "title")
        self.assertEqual(types[-1], "final")
        self.assertIn("divider", types)
        text = json.dumps(m, ensure_ascii=False)
        self.assertIn("В список внесены 12 позиций.", text)
        self.assertIn("Проверить платежи.", text)


if __name__ == "__main__":
    unittest.main()
