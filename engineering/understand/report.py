"""The takeoff workbook: Metraj, Özet, Kontrol and Metodoloji, as XLSX and as CSV.

The sheets follow the user's own takeoff workbook (spec §8): *Metraj* holds
the rows, *Özet* the sums, *Kontrol* what needs a person's eye and
*Metodoloji* how every number was reached - generated from the result, never
typed. The sheet names stay as the user's workbook names them; the headers,
the service names and the method text follow `lang` (tr / en / ru).

CSV is always written, one file per sheet beside the workbook
(``<stem>_metraj.csv`` ...), UTF-8 with a byte-order mark so a spreadsheet
opens the Cyrillic and Turkish letters, a point as the decimal separator. XLSX
needs openpyxl (the `office` extra); without it the CSV files are still
written and the result carries the `xlsx_write` refusal instead of a path.
Numbers are written rounded to 1 mm (three decimals of a metre).
"""

from __future__ import annotations

import csv
from pathlib import Path

from backends.capability import UnsupportedCapabilityError
from engineering.sheet.extract import xlsx_available

__all__ = ["CSV_SLUGS", "LABELS", "LANGS", "SHEETS", "check_lang", "write_workbook"]

LANGS = ("tr", "en", "ru")
SHEETS = ("Metraj", "Özet", "Kontrol", "Metodoloji")
CSV_SLUGS = {"Metraj": "metraj", "Özet": "ozet", "Kontrol": "kontrol", "Metodoloji": "metodoloji"}
_INDEX = {lang: i for i, lang in enumerate(LANGS)}

#: Every word the workbook prints, as (tr, en, ru).
LABELS: dict[str, tuple[str, str, str]] = {
    # Metraj
    "room": ("Mahal", "Room", "Помещение"),
    "service": ("Hizmet", "Service", "Среда"),
    "diameter": ("Çap", "Diameter", "Диаметр"),
    "direct_m": ("Doğrudan (m)", "Direct (m)", "Прямо (м)"),
    "continuity_m": ("Süreklilik (m)", "Continuity (m)", "По непрерывности (м)"),
    "unassigned_m": ("Atanmamış (m)", "Unassigned (m)", "Не назначено (м)"),
    "net_m": ("Net (m)", "Net (m)", "Нетто (м)"),
    "allowance": ("Pay oranı", "Allowance", "Запас"),
    "allowance_m": ("Pay (m)", "Allowance (m)", "Запас (м)"),
    "total_m": ("Paylı toplam (m)", "Total incl. allowance (m)", "Итого с запасом (м)"),
    "schematic_m": ("Şematik (m)", "Schematic (m)", "По схеме (м)"),
    "status": ("Durum", "Status", "Статус"),
    "runs": ("Hatlar", "Runs", "Линии"),
    # Özet, Kontrol, Metodoloji
    "group": ("Grup", "Group", "Группа"),
    "key": ("Anahtar", "Key", "Ключ"),
    "run": ("Hat", "Run", "Линия"),
    "kind": ("Tür", "Kind", "Вид"),
    "tags": ("Etiketler", "Tags", "Теги"),
    "reason": ("Neden", "Reason", "Причина"),
    "topic": ("Konu", "Topic", "Раздел"),
    "value": ("Değer", "Value", "Значение"),
    "by_service": ("Hizmete göre", "By service", "По среде"),
    "by_diameter": ("Çapa göre", "By diameter", "По диаметру"),
    "by_room": ("Mahale göre", "By room", "По помещению"),
    "total": ("TOPLAM", "TOTAL", "ИТОГО"),
    "routed": ("Yönlendirilen", "Routed", "Трассировано"),
    "unroutable_total": ("Yönlendirilemeyen (C)", "Unroutable (C)", "Без трассы (C)"),
    "yes": ("evet", "yes", "да"),
    "no": ("hayır", "no", "нет"),
    # services
    "product": ("Ürün", "Product", "Продукт"),
    "cip_supply": ("CIP besleme", "CIP supply", "CIP подача"),
    "cip_return": ("CIP dönüş", "CIP return", "CIP возврат"),
    "glycol_supply": ("Glikol besleme", "Glycol supply", "Гликоль подача"),
    "glycol_return": ("Glikol dönüş", "Glycol return", "Гликоль возврат"),
    "steam": ("Buhar", "Steam", "Пар"),
    "condensate": ("Kondens", "Condensate", "Конденсат"),
    "cold_water": ("Soğuk su", "Cold water", "Холодная вода"),
    "hot_water": ("Sıcak su", "Hot water", "Горячая вода"),
    "ice_water": ("Buzlu su", "Ice water", "Ледяная вода"),
    "compressed_air": ("Basınçlı hava", "Compressed air", "Сжатый воздух"),
    "drain": ("Drenaj", "Drain", "Дренаж"),
    "electrical": ("Elektrik", "Electrical", "Электрика"),
    "unknown": ("Bilinmeyen", "Unknown", "Неизвестно"),
    # Kontrol kinds and reasons
    "unassigned_diameter": ("Çap atanmamış", "Diameter unassigned", "Диаметр не назначен"),
    "unroutable": ("Yönlendirilemez", "Unroutable", "Трассировка невозможна"),
    "end_not_on_equipment": (
        "Bir uç ekipmanda değil",
        "An end is on no equipment",
        "Конец не на оборудовании",
    ),
    "single_tag": ("Tek etiket", "Only one tag", "Только один тег"),
    "tag_not_on_layout": (
        "Etiket yerleşimde yok",
        "Tag not on the layout",
        "Тега нет на планировке",
    ),
    "tag_ambiguous": (
        "Etiket yerleşimde birden çok kez yazılı",
        "Tag written more than once on the layout",
        "Тег повторяется на планировке",
    ),
    "no_diameter_label": ("Çap etiketi yok", "No diameter label", "Нет обозначения диаметра"),
    # Metodoloji topics
    "source_pid": ("Kaynak: P&ID", "Source: P&ID", "Источник: P&ID"),
    "source_layout": ("Kaynak: yerleşim", "Source: layout", "Источник: планировка"),
    "length_source": ("Uzunluk kaynağı", "Length source", "Источник длины"),
    "choice": ("Seçim gerekçesi", "Why this source", "Почему этот источник"),
    "scale_verdict": ("Ölçek kontrolü: sonuç", "Scale check: verdict", "Проверка масштаба: вывод"),
    "scale_factor": (
        "Ölçek kontrolü: oran (P&ID mm / yerleşim mm)",
        "Scale check: factor (P&ID mm per layout mm)",
        "Проверка масштаба: коэффициент (мм P&ID на мм планировки)",
    ),
    "scale_iqr": ("Ölçek kontrolü: IQR", "Scale check: IQR", "Проверка масштаба: IQR"),
    "scale_within": (
        "Ölçek kontrolü: ±%10 içindeki çiftler",
        "Scale check: pairs within ±10 %",
        "Проверка масштаба: пары в пределах ±10 %",
    ),
    "scale_pairs": ("Ölçek kontrolü: çift sayısı", "Scale check: pairs", "Проверка масштаба: пар"),
    "scale_matched": (
        "Ölçek kontrolü: eşleşen etiketler",
        "Scale check: matched tags",
        "Проверка масштаба: совпавшие теги",
    ),
    "scale_ambiguous": (
        "Ölçek kontrolü: belirsiz etiketler",
        "Scale check: ambiguous tags",
        "Проверка масштаба: неоднозначные теги",
    ),
    "scale_note": ("Ölçek kontrolü: not", "Scale check: note", "Проверка масштаба: примечание"),
    "scale_verified": ("Ölçek doğrulandı", "Scale verified", "Масштаб подтверждён"),
    "units_pid": ("Birim: P&ID", "Units: P&ID", "Единицы: P&ID"),
    "units_layout": ("Birim: yerleşim", "Units: layout", "Единицы: планировка"),
    "rule_route": ("Kural: güzergâh", "Rule: route", "Правило: трасса"),
    "rule_vertical": (
        "Kural: bağlantı başına düşey pay (m)",
        "Rule: vertical allowance per connection (m)",
        "Правило: вертикальный запас на соединение (м)",
    ),
    "rule_allowance": ("Kural: pay oranı", "Rule: allowance", "Правило: запас"),
    "rule_diameter": ("Kural: çap", "Rule: diameter", "Правило: диаметр"),
    "rule_service": ("Kural: hizmet", "Rule: service", "Правило: среда"),
    "rule_status": ("Kural: durum", "Rule: status", "Правило: статус"),
    "rule_rooms": ("Kural: mahaller", "Rule: rooms", "Правило: помещения"),
    "rule_multi": (
        "Kural: çok çaplı hat",
        "Rule: several diameters",
        "Правило: несколько диаметров",
    ),
    "tol": ("Tolerans (çizim birimi)", "Tolerance (drawing units)", "Допуск (ед. чертежа)"),
    "label_search": (
        "Etiket arama mesafesi (çizim birimi)",
        "Label search distance (drawing units)",
        "Радиус поиска обозначений (ед. чертежа)",
    ),
    "overlap": (
        "Çift çizilmiş, bir kez sayılan boru (m)",
        "Double-drawn pipe counted once (m)",
        "Дважды начерченная труба, учтена один раз (м)",
    ),
    "services": ("Kapsanan hizmetler", "Services taken off", "Учтённые среды"),
    "warning": ("Uyarı", "Warning", "Предупреждение"),
    # cable takeoff
    "tag": ("Etiket", "Tag", "Тег"),
    "kw": ("Güç (kW)", "Power (kW)", "Мощность (кВт)"),
    "phases": ("Faz", "Phases", "Фазы"),
    "voltage": ("Gerilim (V)", "Voltage (V)", "Напряжение (В)"),
    "neutral": ("Nötr", "Neutral", "Нейтраль"),
    "vfd": ("Sürücü (VFD)", "VFD", "ЧРП"),
    "panel": ("Pano", "Panel", "Шкаф"),
    "length_m": ("Mesafe (m)", "Distance (m)", "Расстояние (м)"),
    "cable_m": ("Kablo (m)", "Cable (m)", "Кабель (м)"),
    "section": ("Kesit", "Section", "Сечение"),
    "package": ("Paket", "Package", "Комплект"),
    "loads": ("Yük sayısı", "Loads", "Нагрузок"),
    "item": ("Madde", "Item", "Пункт"),
    "detail": ("Ayrıntı", "Detail", "Подробности"),
    "by_panel": ("Panoya göre", "By panel", "По шкафу"),
    "missing_power": ("Güç yazılmamış", "No power stated", "Мощность не указана"),
    "missing_panel": ("Pano belirtilmemiş", "No panel named", "Шкаф не указан"),
    "no_layout": ("Yerleşim yok", "No layout", "Нет планировки"),
    "panel_not_on_layout": (
        "Pano yerleşimde yok",
        "Panel not on the layout",
        "Шкафа нет на планировке",
    ),
    "no_section_rule": ("Kesit kuralı yok", "No section rule covers it", "Нет правила сечения"),
    "conflicting_power": (
        "Çelişen güç yazıları",
        "Conflicting power texts",
        "Противоречивая мощность",
    ),
    "rule_cable_route": ("Kural: kablo güzergâhı", "Rule: cable route", "Правило: трасса кабеля"),
    "rule_rounding": ("Kural: yuvarlama", "Rule: rounding", "Правило: округление"),
    "rule_power": ("Kural: güç", "Rule: power", "Правило: мощность"),
    "rule_panel": ("Kural: pano", "Rule: panel", "Правило: шкаф"),
    "rule_package": ("Kural: pano paketleri", "Rule: panel packages", "Правило: комплектные шкафы"),
    "rule_section": ("Kural: kesit", "Rule: section", "Правило: сечение"),
    "section_rules": (
        "Kesit kuralları (çağıranın)",
        "Section rules (the caller's)",
        "Правила сечений (вызывающего)",
    ),
}

#: The method sheet's sentences, as (tr, en, ru).
TEXTS: dict[str, tuple[str, str, str]] = {
    "requested": ("çağıran seçti", "chosen by the caller", "выбран вызывающим"),
    "forced": (
        "ölçek kontrolü P&ID'yi şematik bulduğu halde istek üzerine (force=True) P&ID'den ölçüldü",
        "measured on the P&ID on request (force=True) although the scale check calls it schematic",
        "измерено по P&ID по запросу (force=True), хотя проверка масштаба считает его схемой",
    ),
    "no_layout": (
        "yerleşim verilmedi: P&ID'den ölçüldü, ölçek doğrulanmadı",
        "no layout given: measured on the P&ID, scale not verified",
        "планировка не задана: измерено по P&ID, масштаб не подтверждён",
    ),
    "auto_to_scale": (
        "ölçek kontrolü P&ID'yi ölçekli buldu: oran üzerinden P&ID'den ölçüldü",
        "the scale check calls the P&ID to scale: measured on it through the factor",
        "проверка масштаба считает P&ID масштабным: измерено по нему через коэффициент",
    ),
    "auto_layout": (
        "ölçek kontrolü P&ID'yi ölçekli bulmadı: yerleşimden ölçüldü",
        "the scale check does not call the P&ID to scale: measured on the layout",
        "проверка масштаба не считает P&ID масштабным: измерено по планировке",
    ),
    "scale_note": (
        "Etiketler ekipman merkezi değildir: ±%10 bant ve 2 m alt sınır bunu karşılar.",
        "Tag labels are not equipment centres: the ±10 % band and the 2 m floor absorb that.",
        "Теги — не центры оборудования: полоса ±10 % и порог 2 м это учитывают.",
    ),
    "rule_route": (
        "Yerleşim uzunluğu = hattın bağladığı etiket konumlarının dik açılı (Manhattan) en küçük "
        "kapsayan ağacı; her bağlantı önce x, sonra y boyunca.",
        "Layout length = the rectilinear (Manhattan) minimum spanning tree of the tag positions "
        "the run joins; each connection along x first, then y.",
        "Длина по планировке = прямоугольное (манхэттенское) минимальное остовное дерево "
        "позиций тегов, которые соединяет линия; каждое соединение сначала по x, затем по y.",
    ),
    "rule_diameter": (
        "Çap etiketleri çizildiği gibi alınır (Ø51, SMS51, DN20 farklı ölçülerdir). Doğrudan: "
        "arama mesafesindeki etiket; süreklilik: etiketli parçadan hat boyunca taşınır, "
        "redüksiyondan geçmez; atanmamış: ikisi de değil (Kontrol sayfasında).",
        "Diameter labels are kept as drawn (Ø51, SMS51 and DN20 are different sizes). Direct: a "
        "label within the search distance; continuity: carried along the run from a labelled "
        "piece, never across a reducer; unassigned: neither (listed on Kontrol).",
        "Обозначения диаметров сохраняются как на чертеже (Ø51, SMS51 и DN20 — разные "
        "размеры). Прямо: обозначение в радиусе поиска; по непрерывности: перенос вдоль линии "
        "от обозначенного участка, но не через переход; не назначено: ни то, ни другое (лист "
        "Kontrol).",
    ),
    "rule_service": (
        "Hizmet katmandan alınır; hattın yakınındaki besleme / dönüş sözcüğü (ПОДАЧА, ВОЗВРАТ, "
        "BESLEME, DÖNÜŞ, SUPPLY, RETURN, AANVOER, RETOUR) tüm hat için onu geçersiz kılar.",
        "Service comes from the layer; a supply / return word near the run (ПОДАЧА, ВОЗВРАТ, "
        "BESLEME, DÖNÜŞ, SUPPLY, RETURN, AANVOER, RETOUR) overrides it for the whole run.",
        "Среда определяется по слою; слово подачи / возврата рядом с линией (ПОДАЧА, ВОЗВРАТ, "
        "BESLEME, DÖNÜŞ, SUPPLY, RETURN, AANVOER, RETOUR) переопределяет её для всей линии.",
    ),
    "rule_status": (
        "A: her uç yerleşimde bulunan bir etikette ve her çap doğrudan. B: süreklilikle ya da "
        "atanmamış çap. C: yönlendirilemez - yalnız şematik uzunluk, Kontrol sayfasında, "
        "toplama katılır ve işaretlenir.",
        "A: every end on a tag placed on the layout and every diameter direct. B: a diameter by "
        "continuity or unassigned. C: not routable - schematic length only, on Kontrol, counted "
        "in the total and flagged.",
        "A: каждый конец на теге, найденном на планировке, и каждый диаметр прямо. B: диаметр "
        "по непрерывности или не назначен. C: трассировка невозможна — только длина по схеме, "
        "на листе Kontrol, входит в итог и помечена.",
    ),
    "rule_rooms": (
        "Yönlendirilen uzunluk, güzergâhın geçtiği yerleşim mahallerine paylaştırılır (duvar "
        "yüzleri ve mahal etiketlerinden çokgenler); çokgen yoksa hatta en yakın mahal etiketine.",
        "A routed length is shared among the layout rooms its route crosses (room polygons from "
        "the wall faces and room labels); without polygons, the room label nearest the run.",
        "Трассированная длина распределяется по помещениям планировки, через которые проходит "
        "трасса (контуры из граней стен и обозначений помещений); без контуров — ближайшему "
        "обозначению помещения.",
    ),
    "rule_multi": (
        "Birden çok çaplı bir hat, uzunluğunu P&ID üzerindeki çizili uzunlukları oranında "
        "paylaştırır.",
        "A run holding several diameters shares its length among them in proportion to their "
        "drawn length on the P&ID.",
        "Линия с несколькими диаметрами делит длину между ними пропорционально их длине на "
        "схеме P&ID.",
    ),
    "rule_cable_route": (
        "Uzunluk = yerleşimde yükün etiketinden panosunun etiketine Manhattan mesafesi.",
        "Length = the Manhattan distance on the layout from the load's tag to its panel's tag.",
        "Длина = манхэттенское расстояние на планировке от тега нагрузки до тега её шкафа.",
    ),
    "rule_rounding": (
        "Kablo = YUKARIYUVARLA(uzunluk × (1 + pay)) tam metreye; uzunluk önce 1e-6 m'ye "
        "yuvarlanır.",
        "Cable = ROUNDUP(length × (1 + allowance)) to the whole metre; the length is rounded to "
        "1e-6 m first.",
        "Кабель = ОКРВВЕРХ(длина × (1 + запас)) до целого метра; длина сначала округляется до "
        "1e-6 м.",
    ),
    "rule_power": (
        "Güç, P&ID'de her etiketin yakınındaki elektrik yazılarından okunur; gücü yazılmamış "
        "yükün hücresi boş kalır ve açık madde olur - güç asla uydurulmaz.",
        "Power is read from the P&ID's electrical texts near each tag; a load with no stated "
        "power keeps an empty cell and an open item - power is never invented.",
        "Мощность берётся из электрических надписей P&ID рядом с каждым тегом; у нагрузки без "
        "указанной мощности ячейка пуста и заводится открытый вопрос — мощность никогда не "
        "выдумывается.",
    ),
    "rule_panel": (
        "Pano, yerleşimdeki bağlantı notlarından ('Wiring to CP1' gibi), yoksa P&ID'den alınır; "
        "adlar normalleştirilir (CP 1 → CP-1).",
        "The panel comes from the layout's wiring callouts ('Wiring to CP1' and its variants), "
        "else from the P&ID; names are normalised (CP 1 → CP-1).",
        "Шкаф берётся из выносок подключения на планировке ('Wiring to CP1' и варианты), иначе "
        "из P&ID; имена нормализуются (CP 1 → CP-1).",
    ),
    "rule_package": (
        "Pano paketleri: kendisi güç belirten bir panoya bağlı yük o paketin parçasıdır; mahal ve "
        "pano toplamları paketin gücünü sayar, alt yüklerini saymaz - hiçbir güç iki kez "
        "sayılmaz.",
        "Panel packages: a load wired to a panel that itself states a power is part of that "
        "package; room and panel totals count the package's power and not its child loads, so "
        "no power is counted twice.",
        "Комплектные шкафы: нагрузка, подключённая к шкафу с собственной указанной мощностью, "
        "входит в его комплект; итоги по помещениям и шкафам учитывают мощность комплекта, а не "
        "его дочерних нагрузок, — ничего не учитывается дважды.",
    ),
    "rule_section": (
        "Kablo kesitleri yalnız çağıranın section_rules tablosundan gelir; tablo yoksa sütun "
        "boş kalır. Hiçbir standart tablo uygulanmaz.",
        "Cable sections come only from the caller's section_rules; without them the column "
        "stays blank. No standard table is applied.",
        "Сечения кабелей — только из таблицы section_rules вызывающего; без неё столбец пуст. "
        "Никакая стандартная таблица не применяется.",
    ),
}


def check_lang(lang: str) -> str:
    """Refuse a language the workbook does not speak, naming the three it does."""
    if lang not in LANGS:
        raise ValueError(f"lang: {lang!r} unknown; choose from {', '.join(LANGS)}")
    return lang


def _t(key: str, lang: str) -> str:
    """A label in `lang`; a key with no label (a diameter, a room number) prints as itself."""
    entry = LABELS.get(key)
    return entry[_INDEX[lang]] if entry else key


def _say(key: str, lang: str) -> str:
    """One of the method sheet's sentences in `lang`."""
    return TEXTS[key][_INDEX[lang]]


def _num(value):
    return None if value is None else round(float(value), 3)


def _yes(value, lang: str):
    return None if value is None else _t("yes" if value else "no", lang)


def _pipe_sheets(result: dict, lang: str) -> dict[str, tuple[list, list]]:
    columns = (
        "room",
        "service",
        "diameter",
        "direct_m",
        "continuity_m",
        "unassigned_m",
        "net_m",
        "allowance",
        "allowance_m",
        "total_m",
        "schematic_m",
        "status",
        "runs",
    )
    rows = [
        [
            row["room"],
            _t(row["service"], lang),
            row["diameter"],
            _num(row["direct_m"]),
            _num(row["continuity_m"]),
            _num(row["unassigned_m"]),
            _num(row["net_m"]),
            row["allowance"],
            _num(row["allowance_m"]),
            _num(row["total_m"]),
            _num(row["schematic_m"]),
            row["status"],
            ", ".join(row["runs"]),
        ]
        for row in result["rows"]
    ]
    totals = result["totals"]
    a = totals["allowance"]
    rows.append(
        [
            _t("routed", lang),
            None,
            None,
            _num(sum(r["direct_m"] for r in result["rows"])),
            _num(sum(r["continuity_m"] for r in result["rows"])),
            _num(sum(r["unassigned_m"] for r in result["rows"])),
            _num(totals["routed_m"]),
            a,
            _num(totals["routed_m"] * a),
            _num(totals["routed_m"] * (1.0 + a)),
            _num(sum(r["schematic_m"] for r in result["rows"])),
            None,
            None,
        ]
    )
    rows.append(
        [
            _t("unroutable_total", lang),
            None,
            None,
            None,
            None,
            None,
            _num(totals["unroutable_m"]),
            a,
            _num(totals["unroutable_m"] * a),
            _num(totals["unroutable_m"] * (1.0 + a)),
            _num(totals["unroutable_m"]),
            "C",
            str(totals["flagged_runs"]),
        ]
    )
    rows.append(
        [
            _t("total", lang),
            None,
            None,
            None,
            None,
            None,
            _num(totals["net_m"]),
            a,
            _num(totals["allowance_m"]),
            _num(totals["total_m"]),
            None,
            None,
            None,
        ]
    )

    summary = []
    for group, field in (
        ("by_service", "service"),
        ("by_diameter", "diameter"),
        ("by_room", "room"),
    ):
        for item in result["summary"][group]:
            key = _t(item["key"], lang) if field == "service" else item["key"]
            summary.append([_t(group, lang), key, _num(item["net_m"]), _num(item["total_m"])])
    summary.append([_t("total", lang), None, _num(totals["net_m"]), _num(totals["total_m"])])

    control = [
        [
            c["run"],
            _t(c["kind"], lang),
            _t(c["service"], lang),
            ", ".join(c["tags"]),
            _num(c["schematic_m"]),
            _t(c["reason"], lang),
        ]
        for c in result["control"]
    ]
    return {
        "Metraj": ([_t(c, lang) for c in columns], rows),
        "Özet": ([_t(c, lang) for c in ("group", "key", "net_m", "total_m")], summary),
        "Kontrol": (
            [_t(c, lang) for c in ("run", "kind", "service", "tags", "schematic_m", "reason")],
            control,
        ),
        "Metodoloji": ([_t("topic", lang), _t("value", lang)], _pipe_method(result, lang)),
    }


def _unit_text(unit: dict | None) -> str:
    if not unit:
        return "—"
    return f"INSUNITS {unit['declared'] or '—'} → {unit['inferred']}"


def _pipe_method(result: dict, lang: str) -> list[list]:
    method, scale = result["method"], result["scale"]
    reason = method["reason"]
    if reason.startswith("auto_") and reason != "auto_to_scale":
        reason = "auto_layout"
    rows = [
        ["source_pid", method["sources"]["pid"]],
        ["source_layout", method["sources"]["layout"] or "—"],
        ["length_source", method["length_source"]],
        ["choice", _say(reason, lang)],
        ["scale_verdict", scale.get("verdict")],
        ["scale_factor", None if scale.get("factor") is None else round(float(scale["factor"]), 6)],
        ["scale_iqr", None if scale.get("iqr") is None else round(float(scale["iqr"]), 6)],
        [
            "scale_within",
            None if scale.get("within_10pct") is None else round(float(scale["within_10pct"]), 4),
        ],
        ["scale_pairs", scale.get("pair_count")],
        ["scale_matched", len(scale.get("matched", {}))],
        ["scale_ambiguous", ", ".join(scale.get("ambiguous", [])) or "—"],
        ["scale_note", _say("scale_note", lang)],
        ["scale_verified", _yes(result["scale_verified"], lang)],
        ["units_pid", _unit_text(result["units"]["pid"])],
        ["units_layout", _unit_text(result["units"]["layout"])],
        ["services", ", ".join(_t(s, lang) for s in method["services"])],
        ["rule_route", _say("rule_route", lang)],
        ["rule_vertical", method["vertical_allowance"]],
        ["rule_allowance", method["allowance"]],
        ["rule_diameter", _say("rule_diameter", lang)],
        ["rule_service", _say("rule_service", lang)],
        ["rule_status", _say("rule_status", lang)],
        ["rule_rooms", _say("rule_rooms", lang)],
        ["rule_multi", _say("rule_multi", lang)],
        ["tol", method["tol"]],
        ["label_search", round(float(method["label_search"]), 6)],
        ["overlap", _num(method["overlap_length_m"])],
    ]
    rows += [["warning", warning] for warning in result["warnings"]]
    return [[_t(topic, lang), value] for topic, value in rows]


def _cable_sheets(result: dict, lang: str) -> dict[str, tuple[list, list]]:
    columns = (
        "tag",
        "room",
        "kw",
        "phases",
        "voltage",
        "neutral",
        "vfd",
        "panel",
        "length_m",
        "allowance",
        "cable_m",
        "section",
        "package",
    )
    rows = [
        [
            row["tag"],
            row["room"],
            row["kw"],
            row["phases"],
            row["voltage"],
            _yes(row["neutral"], lang),
            _yes(row["vfd"], lang),
            row["panel"],
            _num(row["length_m"]),
            row["allowance"],
            row["cable_m"],
            row["section"],
            row["package"],
        ]
        for row in result["rows"]
    ]
    totals = result["totals"]
    rows.append(
        [
            _t("total", lang),
            None,
            _num(totals["known_kw"]),
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            totals["cable_m"],
            None,
            None,
        ]
    )
    summary = []
    for group in ("by_room", "by_panel"):
        for item in result["summary"][group]:
            summary.append(
                [_t(group, lang), item["key"], item["loads"], _num(item["kw"]), item["cable_m"]]
            )
    summary.append(
        [_t("total", lang), None, totals["loads"], _num(totals["known_kw"]), totals["cable_m"]]
    )
    control = [
        [item["tag"], _t(item["item"], lang), item["detail"]] for item in result["open_items"]
    ]
    return {
        "Metraj": ([_t(c, lang) for c in columns], rows),
        "Özet": ([_t(c, lang) for c in ("group", "key", "loads", "kw", "cable_m")], summary),
        "Kontrol": ([_t(c, lang) for c in ("tag", "item", "detail")], control),
        "Metodoloji": ([_t("topic", lang), _t("value", lang)], _cable_method(result, lang)),
    }


def _cable_method(result: dict, lang: str) -> list[list]:
    method = result["method"]
    rules = method["section_rules"]
    rows = [
        ["source_pid", method["sources"]["pid"]],
        ["source_layout", method["sources"]["layout"] or "—"],
        ["units_pid", _unit_text(result["units"]["pid"])],
        ["units_layout", _unit_text(result["units"]["layout"])],
        ["rule_cable_route", _say("rule_cable_route", lang)],
        ["rule_allowance", method["allowance"]],
        ["rule_rounding", _say("rule_rounding", lang)],
        ["rule_power", _say("rule_power", lang)],
        ["rule_panel", _say("rule_panel", lang)],
        ["rule_package", _say("rule_package", lang)],
        ["rule_section", _say("rule_section", lang)],
        ["section_rules", "; ".join(_rule_text(rule) for rule in rules) or "—"],
        ["label_search", round(float(method["search"]["pid"]), 6)],
    ]
    rows += [["warning", warning] for warning in result["warnings"]]
    return [[_t(topic, lang), value] for topic, value in rows]


def _rule_text(rule: dict) -> str:
    phases = "" if rule["phases"] is None else f" / {rule['phases']} ph"
    return f"<= {rule['max_kw']:g} kW{phases}: {rule['section']}"


_BUILDERS = {"pipe": _pipe_sheets, "cable": _cable_sheets}


def write_workbook(result: dict, *, kind: str, lang: str, path: str) -> dict:
    """Write the takeoff: CSV always, XLSX when openpyxl is installed.

    ``{"xlsx": path | None, "csv": [paths], "refused": {...} | None}``. Refused
    before a file is written: a `kind` it builds no sheets for, a `lang` other
    than tr / en / ru, and a `path` with a suffix other than .xlsx. Without
    openpyxl the CSV files are written and `refused` carries the
    `xlsx_write` capability refusal naming the `office` extra.
    """
    if kind not in _BUILDERS:
        raise ValueError(f"kind: {kind!r} unknown; choose from {', '.join(_BUILDERS)}")
    check_lang(lang)
    target = Path(path)
    if target.suffix.lower() not in ("", ".xlsx"):
        raise ValueError(
            f"path: the workbook is written as .xlsx with CSV files beside it; got {target.suffix!r}"
        )
    sheets = _BUILDERS[kind](result, lang)
    xlsx = target.with_suffix(".xlsx")
    xlsx.parent.mkdir(parents=True, exist_ok=True)
    written = []
    for name, (header, rows) in sheets.items():
        csv_path = xlsx.with_name(f"{xlsx.stem}_{CSV_SLUGS[name]}.csv")
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(header)
            writer.writerows([["" if cell is None else cell for cell in row] for row in rows])
        written.append(str(csv_path))
    if not xlsx_available():
        refusal = UnsupportedCapabilityError(
            "xlsx_write",
            "The takeoff workbook needs openpyxl for XLSX, which is not installed. Install it "
            'with: pip install -e ".[office]" (or pip install openpyxl). The same sheets were '
            f"written as CSV: {', '.join(written)}",
        )
        return {"xlsx": None, "csv": written, "refused": refusal.to_dict()}
    from openpyxl import Workbook

    book = Workbook()
    for i, (name, (header, rows)) in enumerate(sheets.items()):
        sheet = book.active if i == 0 else book.create_sheet()
        sheet.title = name
        sheet.append(header)
        for row in rows:
            sheet.append(row)
    book.save(xlsx)
    return {"xlsx": str(xlsx), "csv": written, "refused": None}
