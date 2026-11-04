"""Иерархия ошибок. Класс ошибки определяет статус фонда и влияние на публикацию."""

from __future__ import annotations


class PifError(Exception):
    """Базовая ошибка конвейера."""


class FetchError(PifError):
    """Не удалось получить ресурс. Подклассы различают причину для честного статуса."""

    def __init__(self, url: str, detail: str = "") -> None:
        super().__init__(f"{url}: {detail}" if detail else url)
        self.url = url
        self.detail = detail


class SiteUnavailable(FetchError):
    """Таймаут, DNS, 5xx: сайт сейчас не отвечает."""


class AccessBlocked(FetchError):
    """403, 429 или капча. Такие ответы не обходим, а фиксируем как статус."""


class RobotsDisallowed(FetchError):
    """Адрес закрыт в robots.txt. Это закрытый раздел, поэтому запрос не отправляется."""


class ResourceNotFound(FetchError):
    """404: страницы или файла больше нет."""


class ParseError(PifError):
    """Файл получен, но разобрать его не удалось."""


class UnknownFormatError(ParseError):
    """Незнакомый формат. Даёт явную ошибку вместо тихого нуля."""

    def __init__(self, source: str, reason: str) -> None:
        super().__init__(f"Незнакомый формат источника {source}: {reason}")
        self.source = source
        self.reason = reason

    def __reduce__(self) -> tuple[type, tuple[str, str]]:
        # Ошибка возвращается из пула процессов, поэтому должна переживать pickle.
        return (self.__class__, (self.source, self.reason))
