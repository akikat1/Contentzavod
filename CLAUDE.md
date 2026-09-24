# Контент-завод — заметки для агента

Автономный конвейер длинных видео + шортсов с публикацией на 14+ площадок. Python 3.11+, ffmpeg, SQLite.
Целевое железо: i5-11400F, 16 ГБ ОЗУ, GTX 1650 4 ГБ, Windows + WSL2 Ubuntu.

**Если тебя попросили довести настройку на ПК пользователя — работай строго по `docs/AGENT_SETUP.md`.**

## Где что
- `factory/core/` — конфиг, SQLite, JSON-контракт сценария (`schema.py`), пул ключей, оркестратор (`pipeline.py`),
  CLI (`cli.py`), демон, отчёт готовности (`setup_check.py`), мастер настройки (`wizard.py`), реестр секретов
  площадок (`requirements.py`).
- `factory/stages/s01…s17` — стадии; каждая идемпотентна, артефакты в `workspace/jobs/<job>/`.
- `factory/engagement/` — драматургия, Voice Strategy, визуальный ритм, ротация форматов, банк хуков.
- `factory/providers/` — LLM, TTS, изображения, сток, звук, публикация.
- `config/factory.yaml` — всё настраиваемое (в git). Машинные переопределения — `config/local.yaml` (gitignored).
- Секреты: `.env` и `config/keys.yaml` (оба gitignored, права 600).

## Команды
```bash
. .venv/bin/activate
pytest -q                     # все тесты, включая сквозной офлайн-прогон (~40 с)
ruff check factory tests      # линтер
factory setup check           # что готово / что осталось (--json для разбора, --probe — живая проверка ключей)
factory run --offline --dry-run --topic "..." --set beatsheet.target_duration_s=90   # прогон без сети
```

## Правила
- Секреты никогда не печатать целиком, не коммитить и не просить у пользователя в чат — только маска
  (`factory env check`); ввод — через мастер настройки (`factory setup wizard`) или `factory env set KEY`.
- Имена моделей LLM и лимиты — данные в конфиге. 404 «model not found» чинится в `config/local.yaml`
  (`factory keys models` показывает актуальные), а не в коде.
- Меняешь код — прогоняй `pytest -q` и `ruff check` до коммита. Пушить в GitHub — только с разрешения пользователя.
