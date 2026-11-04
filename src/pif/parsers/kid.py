"""Разборщик ключевого информационного документа (КИД).

КИД публикуется ежемесячно по единому шаблону Банка России. В нём есть всё нужное для
самопроверки: доходность за 1, 3, 6 и 12 месяцев, СЧА и расчётная стоимость пая на дату.
Файлы встречаются в DOCX и PDF, поэтому сначала получаем текст, а затем разбираем один шаблон.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from pif.errors import NotApplicableDocument, ParseError, ScannedDocument, UnknownFormatError
from pif.parsers.numbers import parse_date, parse_decimal

_DATE_TOKEN = r"(\d{1,2}[./]\d{1,2}[./]\d{2,4}|\d{1,2}\s+[а-яё]+\s+\d{4})"
_REPORT_DATE = re.compile(rf"(?i)по\s+состоянию\s+на(?:\s+которую\s+составлен\s+документ)?[:\s]*{_DATE_TOKEN}")
_REPORT_DATE_FALLBACK = re.compile(rf"(?i)ключев\w+\s+информационн\w+\s+документ[^\n]{{0,80}}?\bна\s+{_DATE_TOKEN}")
_ANY_DATE = re.compile(r"\b\d{1,2}[./]\d{1,2}[./]\d{2,4}\b")
_PRICE_LABEL = re.compile(r"(?i)расч[её]тн\w*\s+стоимост\w*")
_NAV_LABEL = re.compile(r"(?i)стоимост\w*\s+чистых\s+актив\w*")
_RESULTS_SECTION = re.compile(r"(?i)основные\s+результаты\s+инвестирования")
_SECTION_END = re.compile(r"(?i)раздел\s*6|комисси\w+,?\s+оплачиваем")
_LIST_MARKER = re.compile(r"(?m)(^|\s)\d\.\s")
# Сумма в КИД: либо с дробной частью, либо с явной единицей. Иначе это номер сноски или пункта.
_MONEY = re.compile(
    r"(?<![\d.,])(\d{1,3}(?:[ \u00a0]\d{3})+(?:[.,]\d+)?|\d+[.,]\d+|\d+(?=\s*(?:млрд|млн|тыс|руб|₽)))"
    r"(?!\s*%)(?![\d.,])\s*(млрд|млн|тыс)?\.?\s*(руб|₽|rub|доллар|долл|usd|\$|евро|eur|€|юан|cny)?",
    re.I,
)
_RETURN_VALUE = r"[ \t]+([-−–]?[ \t]?\d+(?:[.,]\d+)?)[ \t]*%?"
_RETURN_ROW = {
    1: re.compile(rf"(?i)(?<![\d.,])1[ \t]*мес(?:яц)?\.?{_RETURN_VALUE}"),
    3: re.compile(rf"(?i)(?<![\d.,])3[ \t]*мес(?:яца)?\.?{_RETURN_VALUE}"),
    6: re.compile(rf"(?i)(?<![\d.,])6[ \t]*мес(?:яцев)?\.?{_RETURN_VALUE}"),
    12: re.compile(rf"(?i)(?<![\d.,])(?:1[ \t]*год|12[ \t]*мес(?:яцев)?\.?){_RETURN_VALUE}"),
}
_GLUED_WORDS = re.compile(r"(?i)ключевойинформ|посостоянию|общиесведения")
_HORIZONTAL_HEADER = re.compile(r"(?i)1[ \t]*мес\S*[ \t]+3[ \t]*мес\S*[ \t]+6[ \t]*мес")
_HORIZONTAL_VALUES = re.compile(r"(?i)доходность[ \t]+инвестиций[ \t]+([^\n]+)")
_FUND_NAME = re.compile(
    r"(?is)((?:открыт\w+|интервальн\w+|биржев\w+)\s+паев\w+\s+инвестиционн\w+\s+[^«\"“„]{0,120}?"
    r"(?:[«\"“„][^»\"”“]{2,120}[»\"”“]|(?=под\s+управлением)))"
)
_FUND_NAME_PLAIN = re.compile(r"(?is)((?:открыт\w+|интервальн\w+|биржев\w+)\s+паев\w+\s+инвестиционн.{0,200}?)\s+под\s+управлением")
_HEADING_NAME = re.compile(
    r"(?is)ключев\w+\s+информационн\w+\s+документ\s+((?:опиф|ипиф|бпиф|зпиф)[^«\"“„]{0,90}[«\"“„].{2,140}?[»\"”“])\s*(?:опубликовано|раздел|\n)"
)
_SHORT_NAME = re.compile(r"(?is)\b((?:опиф|ипиф|бпиф|зпиф)\s[^«\"“„]{0,90}[«\"“„][^»\"”“]{2,120}[»\"”“])")
_SECTION_ONE = re.compile(r"(?is)раздел\s*1\b.*?(?=раздел\s*2\b|внимание)")
MIN_TEXT_CHARS = 200
_MULTIPLIERS = {"тыс": Decimal(1000), "млн": Decimal(10**6), "млрд": Decimal(10**9)}
_CURRENCIES = {
    "руб": "RUB", "₽": "RUB", "rub": "RUB",
    "доллар": "USD", "долл": "USD", "usd": "USD", "$": "USD",
    "евро": "EUR", "eur": "EUR", "€": "EUR",
    "юан": "CNY", "cny": "CNY",
}  # fmt: skip


@dataclass(frozen=True, slots=True)
class KidData:
    report_date: date
    fund_name: str | None
    unit_price: Decimal | None
    nav: Decimal | None
    currency: str | None
    returns: dict[int, Decimal] = field(default_factory=dict)
    pays_income: bool | None = None


def _docx_text(content: bytes) -> str:
    try:
        xml = zipfile.ZipFile(io.BytesIO(content)).read("word/document.xml").decode("utf-8")
    except (KeyError, zipfile.BadZipFile) as exc:
        raise UnknownFormatError("КИД docx", str(exc)) from exc
    # Абзацы внутри ячейки склеиваем пробелом: строка таблицы должна остаться одной строкой текста.
    xml = re.sub(r"</w:(tc|p)>", "  ", xml)
    xml = re.sub(r"</w:tr>", "\n", xml)
    return re.sub(r"<[^>]+>", "", xml).replace("&amp;", "&").replace("&quot;", '"')


def _pdf_text(content: bytes) -> str:
    import pdfplumber

    try:
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            pages = pdf.pages[:6]
            text = "\n".join(page.extract_text() or "" for page in pages)
            # В части PDF пробелы заданы только расстоянием между символами и теряются при разборе.
            if _GLUED_WORDS.search(text):
                text = "\n".join(page.extract_text(x_tolerance=1) or "" for page in pages)
            return text
    except Exception as exc:
        raise ParseError(f"Не удалось прочитать PDF КИД: {exc}") from exc


def extract_text(content: bytes, fmt: str) -> str:
    if fmt == "docx":
        return _docx_text(content)
    if fmt == "pdf":
        return _pdf_text(content)
    raise UnknownFormatError("КИД", f"формат {fmt} не поддержан")


def _money(match: re.Match[str]) -> tuple[Decimal | None, str | None]:
    value = parse_decimal(match[1])
    if value is not None and match[2]:
        value *= _MULTIPLIERS[match[2].lower()]
    return value, _CURRENCIES.get(match[3].lower()) if match[3] else None


def _result_amounts(text: str) -> tuple[Decimal | None, Decimal | None, str | None]:
    """Находит стоимость пая и СЧА в разделе результатов.

    В PDF разделы свёрстаны в две колонки, поэтому подпись и число часто разорваны:
    «1. Расчётная стоимость пая 3. СЧА паевого\n1 064,25 руб. инвестиционного фонда 46 085 771,04 руб.».
    Поэтому берём суммы по порядку и сопоставляем с порядком подписей.
    """
    results = _RESULTS_SECTION.search(text)
    offset = results.end() if results else 0
    price_label = _PRICE_LABEL.search(text, offset)
    nav_label = _NAV_LABEL.search(text, max(offset, price_label.start() - 400 if price_label else 0))
    if price_label is None or nav_label is None:
        return None, None, None
    start = min(price_label.start(), nav_label.start())
    end_match = _SECTION_END.search(text, max(price_label.end(), nav_label.end()))
    section = text[start : end_match.start() if end_match else start + 1500]
    section = _LIST_MARKER.sub(" ", _ANY_DATE.sub(" ", section))
    amounts = [_money(m) for m in _MONEY.finditer(section)]
    amounts = [a for a in amounts if a[0] is not None and a[0] > 0][:2]
    if len(amounts) < 2:
        return None, None, amounts[0][1] if amounts else None
    first, second = amounts
    price, nav = (first, second) if price_label.start() <= nav_label.start() else (second, first)
    # СЧА всегда больше стоимости одного пая. Если нет, вёрстка переставила суммы местами.
    if (price[0] or 0) > (nav[0] or 0):
        price, nav = nav, price
    return price[0], nav[0], price[1] or nav[1]


def _returns(text: str) -> dict[int, Decimal]:
    # Разбираем только таблицу «Доходность за период»: выше неё бывают те же слова в описании рисков.
    anchor = re.search(r"(?i)доходность\s+за\s+период", text)
    body = text[anchor.start() :] if anchor else text
    if _HORIZONTAL_HEADER.search(body):
        return _horizontal_returns(body)
    result: dict[int, Decimal] = {}
    for months, pattern in _RETURN_ROW.items():
        match = pattern.search(body)
        value = parse_decimal(re.sub(r"[ 	]", "", match[1])) if match else None
        if value is not None:
            result[months] = value
    return result


def _pays_income(text: str) -> bool | None:
    """КИД прямо говорит, выплачивает ли фонд доход. От этого зависит сравнение доходности."""
    if re.search(r"(?is)доход\w*\s+от\s+управления[^.]{0,120}?не\s+выплачива|не\s+предусмотрен\w*\s+выплат\w*\s+дохода", text):
        return False
    if re.search(r"(?is)выплат\w*\s+дохода|доход\w*\s+от\s+управления[^.]{0,120}?выплачива", text):
        return True
    return None


def _horizontal_returns(body: str) -> dict[int, Decimal]:
    """Таблица повёрнута: периоды идут в шапке, значения одной строкой «Доходность инвестиций»."""
    row = _HORIZONTAL_VALUES.search(body)
    if row is None:
        return {}
    tokens = row[1].split()
    result: dict[int, Decimal] = {}
    for months, token in zip((1, 3, 6, 12), tokens, strict=False):
        value = parse_decimal(token)
        if value is not None:
            result[months] = value
    return result


def parse_kid_text(text: str, source: str = "КИД") -> KidData:
    if len(text.strip()) < MIN_TEXT_CHARS:
        raise ScannedDocument(f"{source}: в файле нет текстового слоя")
    flat = re.sub(r"[ \t ]+", " ", text)
    flat = re.sub(r"[«\"“](\d{1,2})[»\"”]", r"\1", flat)  # «31» июля 2025 г.
    if not re.search(r"(?i)паев\w+\s+инвестиционн\w+\s+фонд", flat):
        raise NotApplicableDocument(f"{source}: документ не о паевом фонде")
    date_match = _REPORT_DATE.search(flat) or _REPORT_DATE_FALLBACK.search(flat)
    report_date = parse_date(date_match[1]) if date_match else None
    if not re.search(r"(?i)ключев\w+\s+информационн\w+\s+документ", flat):
        raise NotApplicableDocument(f"{source}: документ не похож на КИД")
    if report_date is None:
        raise UnknownFormatError(source, "в документе нет даты «по состоянию на»")

    unit_price, nav, currency = _result_amounts(flat)
    # В шапке некоторых КИД перечислены все фонды УК. Имя своего фонда стоит в разделе 1.
    section = _SECTION_ONE.search(flat)
    scope = section[0] if section else flat
    candidates = [m for p in (_FUND_NAME_PLAIN, _FUND_NAME, _SHORT_NAME) if (m := p.search(scope))]
    name_match = _HEADING_NAME.search(flat) or (
        min(candidates, key=lambda m: m.start()) if candidates else _FUND_NAME.search(flat)
    )
    return KidData(
        report_date=report_date,
        fund_name=re.sub(r"\s+", " ", name_match[1]).strip() if name_match else None,
        unit_price=unit_price,
        nav=nav,
        currency=currency,
        returns=_returns(flat),
        pays_income=_pays_income(flat),
    )


def parse_kid(content: bytes, fmt: str, source: str = "КИД") -> KidData:
    return parse_kid_text(extract_text(content, fmt), source)
