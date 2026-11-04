# Зависимости и лицензии

Список построен командой `python scripts/licenses.py` по окружению разработки, транзитивные пакеты включены.

## Вне списка из задания

Задание разрешает MIT, BSD, Apache 2.0 и ISC. Эти пакеты приходят транзитивно, заменить их без отказа от
`httpx` или `mypy` нельзя. Все они работают только при сборе и разработке, в сайт не попадают.

- certifi 2026.10.23: Mozilla Public License 2.0 (MPL 2.0)
- pathspec 1.1.1: Mozilla Public License 2.0 (MPL 2.0)
- typing_extensions 4.16.0: PSF-2.0

## Все пакеты

| Пакет | Версия | Лицензия |
|---|---|---|
| annotated-doc | 0.0.5 | MIT |
| anyio | 4.16.0 | MIT |
| ast_serialize | 0.13.1 | MIT |
| certifi | 2026.10.23 | Mozilla Public License 2.0 (MPL 2.0) |
| cffi | 2.1.1 | MIT-0 |
| charset-normalizer | 3.5.2 | MIT |
| colorama | 0.4.6 | BSD License |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause |
| et_xmlfile | 2.0.0 | MIT License |
| h11 | 0.16.0 | MIT License |
| httpcore | 1.0.9 | BSD-3-Clause |
| httpx | 0.28.1 | BSD License |
| idna | 3.20 | BSD-3-Clause |
| iniconfig | 2.3.1 | MIT |
| Jinja2 | 3.1.6 | BSD License |
| librt | 0.16.0 | MIT |
| lxml | 6.1.3 | BSD-3-Clause |
| markdown-it-py | 4.2.0 | MIT License |
| MarkupSafe | 3.0.4 | BSD-3-Clause |
| mdurl | 0.1.2 | MIT License |
| mypy | 2.4.1 | MIT |
| mypy_extensions | 1.2.0 | MIT |
| openpyxl | 3.1.5 | MIT License |
| opentelemetry-api | 1.45.2 | Apache-2.0 |
| opentelemetry-sdk | 1.45.2 | Apache-2.0 |
| opentelemetry-semantic-conventions | 0.66b2 | Apache-2.0 |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause |
| pathspec | 1.1.1 | Mozilla Public License 2.0 (MPL 2.0) |
| pdfminer.six | 20260107 | MIT |
| pdfplumber | 0.11.10 | MIT License |
| pillow | 13.0.0 | MIT-CMU |
| pluggy | 1.7.0 | MIT License |
| pycparser | 3.12 | BSD-3-Clause |
| Pygments | 2.21.0 | BSD-2-Clause |
| pypdfium2 | 5.14.0 | BSD-3-Clause, Apache-2.0, dependency licenses |
| pytest | 8.4.2 | MIT License |
| pytest-asyncio | 0.26.0 | Apache-2.0 |
| python-docx | 1.2.0 | MIT License |
| PyYAML | 6.0.3 | MIT License |
| rich | 15.0.0 | MIT License |
| ruff | 0.17.3 | MIT |
| shellingham | 1.5.4 | ISC License (ISCL) |
| structlog | 25.5.0 | MIT OR Apache-2.0 |
| typer | 0.27.3 | MIT |
| types-PyYAML | 6.0.12.20261103 | Apache-2.0 |
| typing_extensions | 4.16.0 | PSF-2.0 |
| xlrd | 2.0.2 | BSD License |

Шрифты Manrope, Inter и JetBrains Mono: OFL 1.1, тексты лицензий в `src/pif/site/static/fonts/`.
