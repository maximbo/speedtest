# speedtest

Консольная утилита для последовательного замера скорости скачивания по HTTP/HTTPS с детальной разбивкой таймингов (DNS, TCP, TLS, отправка запроса, ожидание заголовков, TTFB, скачивание тела).

## Возможности

- Последовательные замеры скорости скачивания по указанному URL
- Детальные тайминги по каждому запросу: DNS, TCP connect, TLS handshake, отправка запроса, ожидание заголовков, TTFB, скачивание тела
- Поддержка HTTP/1.1 и HTTP/2
- Прокси (HTTP/HTTPS/SOCKS5)
- Отключение keep-alive для «чистых» замеров
- Произвольные HTTP-заголовки
- Форматы вывода: `text`, `json`, `csv`, `prometheus`
- Статистика: среднее, медиана, min, max

## Требования

- Python **3.14+**
- [uv](https://docs.astral.sh/uv/) — для управления зависимостями

## Установка

### Через uv

```bash
# Клонировать репозиторий
git clone https://github.com/maximbo/speedtest.git
cd speedtest

# Установить и зафиксировать Python 3.14
uv python install 3.14
uv python pin 3.14

# Установить зависимости (создаст .venv)
uv sync

# Установить пакет в editable-режиме
uv pip install -e .
```

### Через pip

```bash
python3.14 -m venv .venv
source .venv/bin/activate   # Linux/macOS

pip install -e .
```

### Дополнительные зависимости

Основная зависимость — `httpx`. Для HTTP/2 и SOCKS-прокси:

```bash
uv pip install -e ".[http2,socks]"
# или
pip install -e ".[http2,socks]"
```

## Запуск

После установки доступна команда `speedtest`:

```bash
speedtest https://example.com/large-file.bin
```

Или через модуль:

```bash
python -m speedtest https://example.com/large-file.bin
```

### Примеры

```bash
# 5 последовательных запросов
speedtest https://example.com/file.bin -n 5

# JSON-вывод
speedtest https://example.com/file.bin -f json

# CSV для дальнейшей обработки
speedtest https://example.com/file.bin -f csv > results.csv

# Prometheus-метрики
speedtest https://example.com/file.bin -f prometheus

# Через прокси
speedtest https://example.com/file.bin -x http://127.0.0.1:8080
speedtest https://example.com/file.bin -x socks5://127.0.0.1:1080

# HTTP/2 + без keep-alive
speedtest https://example.com/file.bin --http2 --no-keep-alive

# Свой User-Agent и заголовки
speedtest https://example.com/file.bin \
  --user-agent "MyBot/2.0" \
  -H "Authorization: Bearer TOKEN" \
  -H "Accept-Encoding: identity"

# Без прогресса в stderr (удобно для скриптов)
speedtest https://example.com/file.bin --no-progress -f json
```

### Все опции

```
usage: speedtest [-h] [-H HEADERS] [-x PROXY] [-n COUNT] [-t TIMEOUT]
                 [-p | --progress | --no-progress] [-f {text,json,csv,prometheus}]
                 [--no-keep-alive] [--user-agent USER_AGENT] [--http2]
                 url

positional arguments:
  url                   Целевой URL для скачивания

options:
  -h, --help            показать справку
  -H, --headers HEADERS
                        HTTP-заголовок в формате 'ключ:значение' (можно несколько)
  -x, --proxy PROXY     URL прокси (http://, https://, socks5://, socks5h://)
  -n, --count COUNT     Количество последовательных запросов (по умолчанию: 10)
  -t, --timeout TIMEOUT Таймаут одного запроса в секундах (по умолчанию: 10.0)
  -p, --progress        Показывать прогресс в stderr (по умолчанию: включено)
  -f, --format FORMAT   Формат вывода: text, json, csv, prometheus (по умолчанию: text)
  --no-keep-alive       Отключить keep-alive
  --user-agent UA       User-Agent
  --http2               Использовать HTTP/2
```

### Пример вывода (text)

```
URL:                   https://example.com/file.bin
Запросов:              3
Ошибок:                0
Всего скачано:         30 000 000 Б (30.00 MB / 28.61 MiB)
Общее время загрузки:  3.612 s
Среднее время запроса: 1.204 s
Средняя скорость:      66.45 Mbps (7.92 MiB/s)
Медиана / Min / Max:   66.50 / 65.90 / 66.95 Mbps

─── Средние тайминги (по успешным запросам) ───
DNS:                   1.20 ms
TCP connect:           12.40 ms
TLS handshake:         35.10 ms
Отправка запроса:      0.30 ms
Ожидание заголовков:   48.20 ms
TTFB (до 1-го байта):  49.10 ms
Скачивание тела:       1150.00 ms
```

### Форматы вывода

| Формат       | Описание                                      |
| ------------ | --------------------------------------------- |
| `text`       | Человекочитаемый отчёт (по умолчанию)         |
| `json`       | Полный JSON со всеми запросами и агрегатами   |
| `csv`        | Таблица по запросам (удобно для Excel/pandas) |
| `prometheus` | Метрики в формате Prometheus exposition       |

## Тестирование

```bash
# Установить dev-зависимости
uv sync --dev

# Запустить все тесты
uv run pytest

# Подробный вывод
uv run pytest -v

# Только конкретный файл/класс/тест
uv run pytest tests/test_speedtest.py -v
uv run pytest tests/test_speedtest.py::TestFormatters -v
uv run pytest tests/test_speedtest.py::TestFetchOnce::test_success -v

# С покрытием
uv run pytest --cov=speedtest --cov-report=term-missing
```

Тесты используют `httpx.MockTransport`, поэтому **сеть не требуется** — всё работает офлайн.

### Структура проекта

```
.
├── pyproject.toml
├── README.md
├── uv.lock
├── src
│   └── speedtest
│       └── __init__.py
└── tests
    └── test_speedtest.py
```

## Разработка

```bash
# Линтинг (если настроен ruff)
uv run ruff check src tests

# Форматирование
uv run ruff format src tests
```
