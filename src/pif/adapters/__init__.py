"""Реестр адаптеров. Имя из config/sources.yaml соответствует классу здесь."""

from __future__ import annotations

from pif.adapters.alfa import AlfaAdapter
from pif.adapters.base import Adapter
from pif.adapters.catalogs import GeroiAdapter, OzonAdapter
from pif.adapters.generic import GenericAdapter
from pif.adapters.tcapital import TCapitalAdapter
from pif.adapters.wim import WimAdapter

ADAPTERS: dict[str, type[Adapter]] = {
    "generic": GenericAdapter,
    "alfa": AlfaAdapter,
    "wim": WimAdapter,
    "tcapital": TCapitalAdapter,
    "geroi": GeroiAdapter,
    "ozon": OzonAdapter,
}
