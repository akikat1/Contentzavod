# Контент-завод

Бесплатный автономный конвейер длинных видео (5–10 мин) с нарезкой шортсов и публикацией на 14+ площадок.
Тема → исследование → **драматургия** → сценарий → **схема голосов** → озвучка → звук → видеоряд → монтаж → шортсы →
QA-гейт → публикация → статистика удержания → следующий выпуск. Без участия человека после разовой настройки.

Рассчитан на скромное железо: **i5-11400F · 16 ГБ ОЗУ · GTX 1650 4 ГБ · SSD 1 ТБ** → 2–3 ролика в сутки.

## Как это устроено

```
 trends ─► research ─► beatsheet ─► script ─► voiceplan ─► voice ─► humanize ─► align
                          │  драматургия     │  схема голосов          │  гуманизация
                          ▼  до текста       ▼  по смыслу              ▼  слои 1–4
 visualplan ─► assets ─► sound ─► render ─► clips ─► metadata ─► qa ─► publish      feedback
   ритм,        сток,     музыка,   посценно  шортсы    главы,     8        очередь,    удержание →
   регистры     картинки  тишина,   + concat  9:16      A/B        проверок лимиты      банк хуков
                          SFX
```

- **Мозг — пул API-ключей** (Gemini, Groq, OpenRouter × ~15 аккаунтов): учёт квот, cooldown после 429, смерть ключа
  после 401, перебор провайдеров по классу задачи, офлайн-дно на llama.cpp. 4 ГБ VRAM модель нужного класса не тянут.
- **Engagement Engine** — семь механик интересности: beat sheet до текста, Voice Strategy, звуковой дизайн,
  визуальный ритм, ротация форматов, шортсы как отдельный продукт, обучение на кривой удержания. См. [docs/ENGAGEMENT.md](docs/ENGAGEMENT.md).
- **Voice Strategy** — голосов столько, сколько требует контент: 1 для простого объяснения, 2–3 для расследования
  и мифов, отдельные голоса персонажей для историй. Смена голоса — только по смысловым блокам, пять инвариантов в коде.
- **Видеоряд без видеогенерации** (на 4 ГБ её нет): сток Pexels/Pixabay, изображения Nano Banana (500/сут на ключ),
  Ken Burns, 2.5D-параллакс, анимированные счётчики, графики, кинетическая типографика.
- **QA-гейт вместо человека**: техника, синхрон, факты, безопасность, дубли, антишаблон, драматургия, голоса.
  Провал → ремонт с нужной стадии с конкретным фидбэком → после 3 циклов или «ремонт не дал эффекта» — карантин и алерт.

## Настройка на своём ПК

Поручите её агенту: откройте Claude Desktop и вставьте первое сообщение из [docs/AGENT_SETUP.md](docs/AGENT_SETUP.md).
Агент поставит всё в WSL и настроит режим 24/7. Вас он позовёт только для шагов с вашими аккаунтами — через
мастер настройки в браузере, секреты в чат не попадают.
`factory setup check` в любой момент покажет, что готово и что осталось.

## Быстрый старт вручную

```bash
sudo apt install ffmpeg espeak-ng fonts-dejavu-core      # Windows: WSL2 Ubuntu, см. docs/SETUP.md
python3.11 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
factory init                                              # БД, процедурные ассеты, заготовка config/keys.yaml
factory doctor                                            # что есть, чего нет

# проверить весь конвейер без сети и ключей (шаблонный контент, на площадки не уходит)
factory run --offline --dry-run --topic "Почему Титаник не услышал предупреждения" --format investigation \
  --set beatsheet.target_duration_s=90

# настоящий прогон
factory keys import --provider gemini --file gemini_keys.txt    # по ключу в строке
factory keys probe                                              # живые ключи и модели
cp .env.example .env && $EDITOR .env                            # Pexels, YouTube, Telegram, ...
factory run --dry-run                                           # тема из трендов, публикация в песочницу
factory run                                                     # по-настоящему (YouTube начинает с private)
```

Автономный режим: на Windows — `scripts/windows/install-autostart.ps1`, на Linux — systemd timer (`deploy/`),
см. [docs/SETUP.md](docs/SETUP.md#автономный-режим).

## Команды

| Команда | Что делает |
|---|---|
| `factory run [--topic] [--format] [--offline] [--dry-run] [--until STAGE]` | новый джоб через весь конвейер |
| `factory resume JOB [--force stage,...]` | продолжить/переделать джоб (идемпотентно) |
| `factory beatsheet --topic ...` | показать драматургию: акты, кривая напряжения, петли, callback |
| `factory voiceplan JOB --explain` | стратегия голосов, назначения и **каждое переключение с причиной** |
| `factory tts demo` | озвучка одной фразы с гуманизацией и без — для сравнения на слух |
| `factory qa JOB` | прогнать 8 проверок QA-гейта |
| `factory keys status\|probe\|import` | пул ключей: остатки квот, проверка, массовый импорт |
| `factory publish [JOB]` / `factory feedback` | очередь публикаций / сбор удержания |
| `factory daemon [--once]` | автономный тик: публикации, фидбэк, производство по плану, уборка |
| `factory setup check [--json] [--probe]` | что готово и что осталось — с категорией «агент / человек» и следующим шагом |
| `factory setup wizard [--open]` | мастер настройки в браузере: ключи, OAuth YouTube, поиск канала Telegram, VK |
| `factory setup verify [секция]` | живая проверка введённых секретов |
| `factory env check` / `factory env set KEY` | секреты в `.env`: маскированный список / скрытый ввод |
| `factory keys models` | актуальные имена моделей у провайдеров (чинить 404 конфигом) |
| `factory auth youtube\|telegram\|telegram-local` | OAuth YouTube; найти канал Telegram; перевести бота на локальный Bot API |

## Документация

- [docs/PLAN.md](docs/PLAN.md) — утверждённый план: архитектура, стек, бюджет, риски
- [docs/AGENT_SETUP.md](docs/AGENT_SETUP.md) — runbook для агента, который доводит настройку на вашем ПК
- [docs/SETUP.md](docs/SETUP.md) — установка и разовая настройка каждой площадки вручную
- [docs/APP_REVIEW.md](docs/APP_REVIEW.md) — готовые тексты заявок TikTok и Meta
- [docs/ENGAGEMENT.md](docs/ENGAGEMENT.md) — механики интересности и Voice Strategy
- [docs/HARDWARE.md](docs/HARDWARE.md) — бюджет VRAM/ОЗУ/диска по стадиям, что менять при апгрейде
- [docs/PLATFORMS.md](docs/PLATFORMS.md) — матрица площадок, лимиты, требования модерации
- [docs/RISKS.md](docs/RISKS.md) — риски, лицензионные решения, что не входит в MVP
- [docs/PROMPT_SONNET5.md](docs/PROMPT_SONNET5.md) — промт для Claude Sonnet 5 на развитие завода

## Структура

```
factory/core/        конфиг, SQLite, JSON-контракт сценария, пул ключей, GPU-мьютекс, оркестратор, CLI, демон
factory/engagement/  драматургия, Voice Strategy, визуальный ритм, ротация форматов, банк хуков
factory/stages/      16 стадий джоба + сбор статистики (s17)
factory/providers/   llm/ tts/ image/ video/ audio/ publish/
factory/render/      сцены, субтитры, обложка, шрифты
factory/qa/          8 проверок QA-гейта
config/              factory.yaml, голоса, форматы, ниша
prompts/             промты стадий
deploy/              systemd, docker-compose (локальный Telegram Bot API, Postiz)
scripts/             setup_wsl.sh (установка в WSL), windows/ (fz.ps1, режим 24/7)
```
