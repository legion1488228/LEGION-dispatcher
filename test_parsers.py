import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from parsers import parse_agent_ready_form


def test_ramenskoe():
    text = """Выдача 21.09.2026
Ум.Гудковский Александр Владимирович
Подача смэ Жуковский 9:30
Выдача смэ Жуковский 10:00
Отпевание храм Космы 10:30
Далее кладбище Загорново
Бригада сопровождения - ВИП 4 чел
Агент Арсений
8-999-836-93-51"""
    d = parse_agent_ready_form(text, "Раменское")
    assert d["work_date"] == "2026-09-21"
    assert d["issue_time"] == "10:00"
    assert d["arrival_time"] == "09:30"
    assert d["team_size"] == 4
    assert d["category"] == "vip"
    assert "Гудковский" in d["deceased_name"]


def test_marina_kickback():
    text = """Ум. Радецкая Татьяна Николаевна
Выдача 21.09.26 морг Буянова в 11:00
Доставка к 10:30
-грузчики 4 чел СТ
Маршрут: морг Буянова-храм Пролетарский пр. 4к6-ПДХ"""
    d = parse_agent_ready_form(text, "Марина контора")
    assert d["team_size"] == 4
    assert d["category"] == "standard"
    assert d["kickback_rub"] == 1000
