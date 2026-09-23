# Контент-завод: план

> Утверждённый план проекта. Что реализовано в MVP и чем реализация отличается от плана — в [RISKS.md](RISKS.md#отличия-реализации-от-плана).

## Context

Репозиторий `akikat1/Contentzavod` пуст. Задача — конвейер, который без оператора проходит путь **тренд → исследование → драматургия → сценарий → голоса → звук → видеоряд → монтаж → нарезка → публикация на 14+ площадок → аналитика → следующая итерация**, и стоит $0.

Решения пользователя:
- **Формат:** длинные видео 5–10 мин.
- **Площадки:** все четыре группы (YouTube/Telegram/Bluesky, TikTok/IG/FB, VK/Rutube/Дзен, X/LinkedIn/Pinterest/Threads/Reddit).
- **Ресурс API:** ~15 аккаунтов × (Groq + Gemini + OpenRouter) ≈ **45+ ключей** — основной вычислительный ресурс, а не фолбэк.
- **Голос:** не один на весь ролик — Voice Strategy с ролями и автоподбором.
- **Объём:** план + рабочий MVP-код.

### Железо диктует архитектуру

```
i5-11400F (6C/12T) · 16 ГБ DDR4 · GTX 1650 4 ГБ · SSD 1 ТБ
```

| Что казалось очевидным | Реальность | Решение |
|---|---|---|
| Ollama + Qwen3-27B для сценариев | 4 ГБ VRAM / 16 ГБ ОЗУ — не влезает | **Облачный пул из 45 ключей** |
| Wan 2.2 / LTX для видеоряда | минимум 8 ГБ VRAM | Сток + Ken Burns + 2.5D-параллакс |
| FLUX локально | на 4 ГБ — до часа на кадр 1024² | **Nano Banana: 500 картинок/сут × 15 ключей = 7500/сут** |
| Один `filter_complex` на 80 входов | 16 ГБ ОЗУ → OOM | Посценный рендер + concat |
| NVENC для ускорения | TU117 несёт Volta-энкодер | `h264_nvenc` **работает**; HEVC B-frames нет → детект в рантайме |

Узкое место сместилось с «мозгов» на локальный рендер: ключей столько, что LLM и картинки перестают ограничивать, всё упирается в 4 ГБ VRAM и 6 ядер CPU.

**Предположение (не блокирующее):** язык не уточнён. Конвейер языко-параметризован, RU — основной профиль, EN — второй.

---

## Что делает контент интересным: Engagement Engine

Это ядро замысла, а не украшение. Конвейер, который берёт тему и ровным голосом читает текст под стоковые клипы, проиграет по удержанию — и попадёт под политику шаблонного контента. Поэтому «интересность» выносится в отдельную подсистему из семи механик, и каждая живёт в JSON-контракте сценария, а не в голове у промта.

| # | Механика | Что даёт | Фаза |
|---|---|---|---|
| **A** | **Beat Sheet** — драматургия проектируется до текста | самый большой прирост удержания | MVP |
| **B** | **Voice Strategy** — роли и голоса по смысловым блокам | ролик перестаёт быть монологом | MVP |
| **C** | **Sound Design** — музыкальная драматургия, SFX, тишина | «дорогое» звучание | MVP |
| **D** | **Visual Rhythm** — темп планов и ротация регистров | глаз не устаёт, нет ощущения шаблона | MVP |
| **E** | **Format Rotation** — 8 форматов передачи по ротации | разнообразие + защита от inauthentic | MVP |
| **F** | **Derivative Repackaging** — шортсы как отдельный продукт | охват без потери качества | Фаза 7 |
| **G** | **Retention Feedback** — обучение на кривой удержания | завод умнеет сам | Фаза 11 |

### A. Beat Sheet — двухпроходное письмо

Главная причина скучных видео: LLM просят «написать сценарий», и он пишет ровную статью. Поэтому пишем в два прохода — сначала **структура**, потом **текст по структуре**. Так работают сценаристы.

Стадия `s03_beatsheet` выдаёт список beat'ов, каждый со своим `intent`, `target_duration_s`, `tension` (0..1), `visual_register`, `music_cue`. Валидатор требует обязательных драматургических элементов:

- **cold_open в первые 10 с** — самый сильный факт или образ, до любого представления и титров.
- **open loop заявлен не позже 45 с** и закрыт в последней трети («через пару минут станет понятно, почему это закончилось трибуналом»).
- **pattern interrupt минимум раз в 40 с** — смена регистра подачи, а не просто смена картинки.
- **callback в финале** на образ из cold_open.
- **кривая напряжения немонотонна:** минимум два пика и спад перед финалом. Ровная кривая — brief отклоняется и перегенерируется.

Текстовый проход (`s04_script`) получает beat sheet как жёсткую рамку и пишет только реплики, не трогая структуру.

### B. Voice Strategy — подробно ниже, отдельным разделом

### C. Sound Design (стадия `s11_sound`)

- **Музыкальная драматургия, а не один трек на 7 минут.** `music_plan` расставляет cue по блокам: нарастание в расследовании, спад в объяснении, смена темы на новом акте.
- **Ducking** музыки под голос (`sidechaincompress`), а не статичное −20 дБ.
- **Тишина перед кульминацией** — 600–900 мс абсолютной тишины перед ключевым тезисом. Самый дешёвый и самый сильный приём в звуке.
- **SFX-акценты:** riser перед откровением, impact на цифре, whoosh на смене акта, тихая ambience под сцену.
- **FX-цепочки по ролям:** `radio_1940` для исторической цитаты, `phone` для телефонного разговора, `inner_thought` (реверб + фильтр) для размышления. Это делает роли слышимо разными даже на одном голосе.

### D. Visual Rhythm (внутри `s09_visualplan`)

- **Длительность плана подчинена смыслу:** перечисление 1.5–3 с, объяснение 5–8 с, кульминация — один длинный план. Не фиксированные N секунд.
- **Ротация визуальных регистров:** сток → инфографика → архив → карта → схема → текст → параллакс. Валидатор запрещает два соседних блока в одном регистре.
- **Каждое число в сценарии = анимированный счётчик.** Дёшево (ASS/matplotlib), выглядит дорого.
- **Зум-панч** на ключевом слове (0.3 с, scale 1.0→1.08) — там же, где `[emph]`.
- **Match cut** между сценами по доминирующему цвету.
- **Цветокоррекция по актам:** LUT сдвигается вместе с напряжением.
- **Акцентные karaoke-субтитры:** слова в `[emph]` идут другим цветом и кеглем. Разметка уже есть в сценарии — переиспользуется, а не придумывается заново.

### E. Format Rotation

Библиотека форматов передачи: `explainer`, `investigation`, `listicle`, `story`, `myth_vs_fact`, `timeline`, `comparison`, `case_study`. Таблица в SQLite помнит, что и когда выходило; селектор запрещает повтор формата чаще чем раз в N выпусков. Одним движением закрывается и разнообразие, и QA-проверка «антишаблон».

### F. Derivative Repackaging

Шортс — не вырезанный кусок, а самостоятельный продукт: свой хук в первые 1.5 с, свой финал-крючок («полная версия по ссылке»), вертикальная перекомпоновка кадра, свои субтитры. Кандидаты приходят прямо из beat sheet — LLM на этапе структуры знает, какие блоки самодостаточны.

### G. Retention Feedback

YouTube Analytics API → кривая удержания → точки отвала. Копятся `hook_bank` (какие хуки держали) и `beat_stats` (какая длина cold_open работала). Следующий beat sheet генерируется с этой статистикой в контексте. Плюс A/B: три варианта заголовка и обложки, выбор по истории CTR.

---

## Voice Strategy

### Библиотека голосов — что реально доступно

edge-tts даёт два родных русских голоса (`ru-RU-DmitryNeural`, `ru-RU-SvetlanaNeural`) **и около одиннадцати мультиязычных** (`AvaMultilingual`, `AndrewMultilingual`, `EmmaMultilingual`, `BrianMultilingual`, `RemyMultilingual`, `VivienneMultilingual`, `SeraphinaMultilingual`, `FlorianMultilingual`, `GiuseppeMultilingual`, `ThalitaMultilingual`, `HyunsuMultilingual`), которые говорят по-русски. Итого ~13 базовых тембров бесплатно.

У мультиязычных на русском слышен лёгкий акцент — и это **используется как художественное средство**, а не терпится: иностранный эксперт, зарубежная пресса, перевод цитаты. Родные русские голоса резервируются под нарратора, где акцент недопустим.

Дальше библиотека расширяется без новых движков:
- **просодические варианты:** один голос при `pitch −2st, rate −6%` воспринимается как другой человек → ×3 к числу персонажей;
- **FX-цепочки** (`radio_1940`, `phone`, `inner_thought`) → ещё ×3;
- **RVC** (фаза 9) → произвольное число уникальных тембров.

`config/voices/ru.yaml`:
```yaml
voices:
  - id: nar_main_m
    engine: edge
    voice: ru-RU-DmitryNeural
    roles: [main_narrator]
    timbre: warm_low
    native: true
  - id: nar_second_f
    engine: edge
    voice: ru-RU-SvetlanaNeural
    roles: [secondary_narrator, character]
    timbre: bright_mid
    native: true
  - id: char_expert_m
    engine: edge
    voice: en-US-AndrewMultilingualNeural
    roles: [character, quote]
    timbre: neutral_mid
    accent: light_foreign      # осознанно: иностранный эксперт
  - id: quote_archive
    engine: edge
    voice: ru-RU-DmitryNeural
    roles: [quote]
    prosody: { rate: "-8%", pitch: "-2st" }
    fx_chain: radio_1940
```

### Селектор — детерминированный, не LLM

Стадия `s05_voiceplan` не спрашивает модель «сколько голосов взять». Она читает `content_type` и состав beat sheet и применяет правила:

| content_type | strategy | роли |
|---|---|---|
| `explainer` простой (≤6 блоков, 0 цитат) | `single` | main_narrator |
| `explainer` / `timeline` с цитатами | `single_plus_quote` | main + quote |
| `investigation`, `listicle`, `comparison` | `dual_narrator` | main + secondary |
| `myth_vs_fact` | `dual_opposing` | main + «оппонент» |
| `story`, `case_study` с персонажами | `cast` | main + до 3 character |

Назначение конкретных голосов ролям детерминировано и стабильно внутри серии (канал узнаваем), но ротируется между сериями. Голоса, занятые в прошлом выпуске под роль `character`, при прочих равных не переиспользуются — иначе все персонажи завода звучат одинаково.

### Правило смены голоса

Требование «не менять механически каждые N секунд» реализуется тремя инвариантами, которые проверяет валидатор `s05`, а не добрыми намерениями промта:

1. **Смена только на границе блока.** У каждого сегмента есть `block_id` из beat sheet. Внутри одного блока роль не меняется — **единственное исключение — `quote`**: цитата по смыслу живёт внутри абзаца.
2. **Минимальная длительность роли — 12 с** (`voice.min_role_duration_s`). Если LLM разметил `secondary_narrator` на три секунды не-цитаты, валидатор схлопывает сегмент обратно в `main_narrator`. Это и есть защита от дробления.
3. **Потолок переключений** — `voice.max_switches_per_minute: 2`. Превышение → сегменты с наименьшей длительностью схлопываются к main, пока норма не выполнена.

Плюс два правила связности: ролик всегда открывается и закрывается `main_narrator` (рамка канала), и `secondary_narrator` не может произнести подряд больше двух блоков — иначе он перестаёт быть вторым голосом и становится первым.

### Где это живёт

- **JSON-контракт:** `voice_plan` на уровне ролика + `role`/`speaker`/`prosody`/`fx_chain` на каждом сегменте (см. ниже).
- **Конфиг:** `config/factory.yaml → voice:` (стратегии, пороги, включение RVC) и `config/voices/<lang>.yaml` (библиотека).
- **Стадия `s06_voice`:** синтезирует **посегментно**, беря движок и просодию из назначенного голоса, применяет `fx_chain` роли, склеивает с паузами из beat sheet. Роль сегмента, а не глобальная настройка, определяет всё.

---

## JSON-контракт сценария

Позвоночник всей системы: сюда стекаются драматургия, голоса, визуал, звук и источники. Каждая стадия ниже читает свой срез, и ни одна не догадывается о намерениях по тексту.

```jsonc
{
  "job_id": "2026-09-23-titanic-radio",
  "content_type": "investigation",        // из Format Rotation
  "language": "ru",
  "title_candidates": ["...", "...", "..."],   // A/B
  "target_duration_s": 420,

  "voice_plan": {
    "strategy": "dual_narrator",
    "assignments": {
      "main_narrator": "nar_main_m",
      "secondary_narrator": "nar_second_f",
      "quote": "quote_archive"
    }
  },

  "beats": [
    { "block_id": "b01", "act": "cold_open", "intent": "шок-факт без контекста",
      "target_duration_s": 11, "tension": 0.9,
      "visual_register": "archive", "music_cue": "silence_then_impact" },
    { "block_id": "b02", "act": "promise", "intent": "открыть петлю",
      "target_duration_s": 18, "tension": 0.5,
      "visual_register": "motion_graphics", "music_cue": "bed_low" }
  ],

  "segments": [
    { "id": "s001", "block_id": "b01", "role": "main_narrator", "speaker": null,
      "text": "[emph]Девять минут[/emph]. [pause:450] Столько у них было.",
      "prosody": { "rate": "-4%", "pitch": "-1st" },
      "fx_chain": null,
      "visual": { "marker": "PARALLAX", "prompt": "..." },
      "sfx": ["impact_low"],
      "source_ids": ["r03"] },

    { "id": "s014", "block_id": "b04", "role": "quote",
      "speaker": "Корреспондент The Times, 1912",
      "text": "...", "fx_chain": "radio_1940",
      "visual": { "marker": "TEXT", "content": "..." },
      "source_ids": ["r07"] }
  ],

  "sources": [ { "id": "r03", "url": "...", "claim": "..." } ],
  "music_plan": [ { "from_block": "b01", "to_block": "b03",
                    "mood": "tense", "duck_db": -12 } ],
  "chapters": [ { "block_id": "b02", "title": "Что пошло не так" } ],
  "shorts_candidates": [ { "block_ids": ["b01","b02"], "hook": "...",
                           "outro_hook": "..." } ]
}
```

Схема фиксируется в `factory/core/schema.py` (pydantic) и валидируется на выходе `s04`. Невалидный сценарий не идёт дальше — перегенерируется с текстом ошибки в контексте.

---

## Честная граница автономности

Человек делает **ровно один раз на старте**:

| Шаг | Где | Сколько |
|---|---|---|
| OAuth каждой площадки | все | часы |
| Заведение 45 ключей в пул | Groq/Gemini/OpenRouter | часы |
| TikTok app audit — заявка + демо-видео | TikTok | 2–4 нед; **до аудита посты private** |
| Meta app review | IG + FB | 2–4 нед, отдельно на каждое разрешение |
| Сессия для Playwright (публичного API нет) | Дзен | обновлять ~раз в месяц |

Запуск двухфазный: **контур A** (YouTube, Telegram, Bluesky, VK, Rutube) — за день; **контур B** (TikTok, IG, FB) подключается сам после одобрения.

---

## Архитектура

```
         ┌──────────────────── daemon (systemd timer) ─────────────────────┐
         ▼                                                                 │
 [1] Trends ─► [2] Research ─► [3] BeatSheet ─► [4] Script(JSON) ─► [5] VoicePlan
                                                                          │
   ┌──────────────────────────────────────────────────────────────────────┘
   ▼
 [6] Voice(посегментно) ─► [7] Humanize ─► [8] Align ─► [9] VisualPlan ─► [10] Assets
                                                                          │
   ┌──────────────────────────────────────────────────────────────────────┘
   ▼
 [11] Sound ─► [12] Render(посценно+concat) ─► [13] Clips ─► [14] Metadata
                                                                          │
   ┌──────────────────────────────────────────────────────────────────────┘
   ▼
 [15] QA Gate ──fail→ авто-ремонт──┐
        │ pass                     │
        ▼                          ▲
 [16] Publish ─► [17] Feedback ────┴──────────────────────────────────────┘
```

Состояние в SQLite; каждая стадия — идемпотентная `(job_id) -> artifacts`; падение откатывает только свою стадию. Оркестратор — Python + SQLite + systemd timer. Не n8n: его Sustainable Use License ограничивает коммерческое использование, а GUI-воркфлоу не ревьюится в git.

---

## Подсистема: пул ключей

Самый ценный актив — 45 ключей, поэтому отдельный модуль `core/keypool.py`, а не разрозненные `try/except`.

```
keys(id, provider, account_label, secret_ref, task_classes,
     rpm/rpd/tpd/ipd_limit, rpm/rpd/tpd/ipd_used, window_reset_at,
     health_score, cooldown_until, last_error, consecutive_failures)
```

`acquire(task_class)` отдаёт живой ключ с наибольшим остатком квоты. 429 → экспоненциальный cooldown; 401/403 → ключ мёртв + алерт. Классы задач: `research`, `beatsheet`, `script`, `metadata`, `image`, `judge` — у каждого свой порядок предпочтения провайдеров.

| Провайдер | Лимиты на ключ | Куда ставим |
|---|---|---|
| Gemini free | Flash-текст до 1M контекста; **Nano Banana ~500 изображений/сут**; TPM 250K | research (большой контекст), **image** |
| Groq free | 30 RPM, 1000 RPD, 200K токенов/сут | script, metadata (быстро) |
| OpenRouter `:free` | 20 RPM, 50 RPD (1000 после разовой покупки 10 кредитов) | judge, резерв |

С 15 аккаунтами это ≈ 7500 изображений и миллионы токенов в сутки — на порядок больше, чем нужно для 2–3 видео.

⚠️ Провайдеры обычно трактуют мультиаккаунт ради обхода лимитов как нарушение ToS и могут забанить. Код поддерживает N ключей независимо от того, сколько их у вас; сколько заводить — ваше решение. Архитектурно запас ключей полезен и просто на случай отказа провайдера.

**Офлайн-дно:** `llama.cpp` + Qwen3-4B Q4 на CPU (~3 ГБ ОЗУ, 5–12 tok/s). Сценарий выйдет хуже, но конвейер не встанет.

---

## Подсистема: голос без «робота»

Четыре слоя; первые три не трогают GPU и дают ~90 % эффекта.

**Слой 1 — писать под ухо (0 GPU, самый большой вклад).** Главная причина «робота» — не движок, а текст. LLM пишет партитуру: фразы 8–14 слов, разговорные связки, риторические вопросы, инлайн-разметка `[pause:350]`, `[emph]`, `[slow]`, `[breath]`.

**Слой 2 — просодия (0 GPU).** Синтез **посегментно**, не одним куском: rate ±4 %, pitch ±2 полутона по кривой, привязанной к смыслу — вопрос поднимает тон к концу, вывод замедляется. Паузы 120–300 мс между фразами, 400–700 мс между абзацами. Монотонность исчезает здесь.

**Слой 3 — постобработка (0 GPU, FFmpeg).**
```
highpass f=80                      убрать гул
deesser                            шипящие
equalizer f=3000 g=+2.5            presence — голос выходит вперёд
acompressor thr=-18dB ratio=3      ровная громкость
[вставка дыханий]                  банк из 6–10 вдохов перед длинными фразами
afir (короткий IR комнаты, wet 8%) ← сильнее всего убивает «сухого робота»
loudnorm I=-14 TP=-1.5 LRA=11      норма для всех площадок
```
Конволюционная реверберация недооценена: синтез звучит неестественно во многом потому, что записан в абсолютной тишине, чего с живым человеком не бывает никогда.

**Слой 4 — RVC v2 (GPU 4 ГБ, фаза 9).** Переносит на готовое аудио тембр конкретного человека; минимум для инференса — 4 ГБ, GTX 1650 подходит впритык, 7 минут обрабатываются чанками за ~5–15 мин. Снимает «тембр TTS» полностью и заодно снимает потолок библиотеки голосов.

⚠️ Тренировать RVC только на голосе, права на который есть — не на голосе публичной персоны.

В MVP работают слои 1–3 (`voice.humanize: basic`), слой 4 включается конфигом за тем же интерфейсом.

---

## Подсистема: видеоряд без видеогенерации

Локальная генерация видео на 4 ГБ невозможна, и это не потеря: длинному формату нужна документальная фактура, а не нейросетевые галлюцинации.

| Маркер | Провайдер | Стоимость |
|---|---|---|
| `[STOCK: запрос]` | Pexels (200 req/ч, 20K/мес, без атрибуции) + Pixabay (~100/мин) | 0 |
| `[IMAGE: промпт]` | Nano Banana через пул → Pollinations (FLUX, без ключа) → локальный SDXL-Turbo | 0 |
| `[PARALLAX: промпт]` | картинка + MiDaS-small depth → 2.5D-облёт камеры | ~3 с/кадр |
| `[CHART: данные]` | matplotlib / Revideo | 0 |
| `[COUNTER: число]` | анимированный счётчик (ASS) | 0 |
| `[TEXT: тезис]` | кинетическая типографика | 0 |

`[PARALLAX]` — замена нейровидео: из статики получается настоящее движение камеры с разделением планов, на 4 ГБ работает, выглядит дорого. Remotion не берём (бесплатен только до 3 сотрудников), Revideo — MIT.

---

## Подсистема: GPU-планировщик и память

- **Глобальный мьютекс GPU.** Whisper, MiDaS, SDXL-Turbo, RVC выстраиваются в очередь; модель выгружается (`torch.cuda.empty_cache()`) перед передачей карты.
- **Whisper:** `faster-whisper small`, `compute_type=int8` (~1 ГБ). `large-v3` не влезает вместе с моделью выравнивания.
- **Рендер посценно.** Каждая сцена (1.5–15 с) — отдельный процесс FFmpeg, затем `concat demuxer` без перекодирования. Один `filter_complex` на 80 входов при 16 ГБ уходит в OOM.
- **NVENC:** `h264_nvenc` детектится в рантайме тестовым кадром. TU117 несёт Volta-энкодер — H.264 работает, HEVC B-frames нет. Фолбэк `libx264 -preset veryfast`.
- **Диск:** джоб занимает 3–5 ГБ промежуточных файлов. После публикации остаются только финалы + манифест, история 20 джобов. Иначе 1 ТБ кончится за месяц.

---

## Бюджет одного видео (7 минут, это железо)

| Стадия | Время | Ресурс |
|---|---|---|
| Research + beat sheet + script (пул) | 5–9 мин | сеть |
| Voice plan (детерминированный) | <1 с | CPU |
| edge-tts посегментно (2–4 голоса) | 2–3 мин | CPU |
| Гуманизация слои 1–3 | ~1 мин | CPU |
| *(опц.) RVC* | *5–15 мин* | *GPU* |
| WhisperX small int8 | 2–3 мин | GPU |
| 60–80 картинок через Nano Banana | 4–8 мин | сеть |
| Стоковые клипы | 1–3 мин | сеть |
| Параллакс 10–15 сцен | 3–5 мин | GPU |
| Sound design (музыка, SFX, ducking) | 1–2 мин | CPU |
| Посценный рендер + concat (NVENC) | 5–10 мин | GPU+CPU |
| 4 шортса с перепаковкой | 4–6 мин | CPU |
| Публикация на 5–14 площадок | 5–15 мин | сеть |
| **Итого** | **40–70 мин** (с RVC до 85) | |

→ **2–3 мастер-видео в сутки** с запасом на ретраи. Честная цифра для i5 + GTX 1650.

---

## Стек: сводка

| Слой | Выбор | Почему |
|---|---|---|
| Мозг | Пул 45 ключей (Gemini / Groq / OpenRouter) + llama.cpp Qwen3-4B офлайн | 4 ГБ VRAM не тянет модель нужного класса |
| Голос | edge-tts (13 тембров с мультиязычными) + 3 слоя постобработки + RVC опц. | бесплатно, CPU, освобождает карту |
| Субтитры | WhisperX, пословные тайминги <100 мс, `.ass` karaoke с акцентами | стандарт вовлечения |
| Картинки | Nano Banana → Pollinations → SDXL-Turbo локально | 7500 изображений/сут бесплатно |
| Видеоряд | Pexels + Pixabay + Ken Burns + 2.5D-параллакс (MiDaS) | нейровидео недоступно и не нужно |
| Музыка/SFX | Pixabay Music + банк SFX; MusicGen если влезет | без атрибуции |
| Монтаж | FFmpeg + h264_nvenc (детект) / libx264; Revideo для анимаций | Remotion платный >3 сотрудников |
| Нарезка | свой модуль в духе ClipsAI / OpenShorts (MIT) | кандидаты приходят из beat sheet |
| Публикация | адаптеры площадок + self-hosted Postiz (AGPL, 33 платформы) | один контейнер вместо 8 интеграций |
| Буфер | Cloudflare R2 free (10 ГБ, egress $0) | площадки тянут по URL |
| CI | GitHub Actions (public repo = безлимит) | линт, тесты, валидация схемы сценария |

---

## Публикация: матрица площадок

| Платформа | Что льём | Механизм | Лимиты / оговорки |
|---|---|---|---|
| YouTube | мастер + Shorts | Data API v3 | с 01.06.2026 `videos.insert` = **1 unit, отдельный бакет 100 загрузок/сут на проект** (было 1600 units ≈ 6/сут). Скрытый per-channel лимит отдаётся как 429 |
| VK Video | мастер | `video.save` | токен приложения |
| Rutube | мастер | Rutube API | токен |
| Telegram | мастер | **локальный Bot API server** (2 ГБ) | обычный Bot API режет на 50 МБ |
| Bluesky | мастер | `app.bsky.video.uploadVideo` | до 10 мин / 300 МБ; суточный лимит видеопостов |
| Facebook Reels | шортсы | Graph API | после review |
| Instagram Reels | шортсы | Graph API | 9:16, 5–90 с, **25 постов/24 ч** |
| TikTok | шортсы | Content Posting API | бесплатен; 6 req/мин на токен; до аудита private |
| Дзен | мастер | Playwright (API нет) | самое хрупкое звено |
| X, LinkedIn, Pinterest, Threads, Reddit, Mastodon | шортсы/анонсы | Postiz self-hosted | per-platform |

---

## QA-гейт: то, что заменяет человека

Публикация только после автоматической приёмки. Каждая проверка с авто-ремонтом (перегенерация конкретного блока, до 3 попыток, затем карантин + алерт в Telegram):

1. **Техника:** нет чёрных кадров >1.5 с, нет тишины >2 с *вне запланированных пауз из `music_plan`*, громкость −14 LUFS ±1, длительность и кодеки под площадку.
2. **Синхрон:** рассинхрон субтитров <150 мс, текст внутри safe-zone для 9:16.
3. **Факты:** LLM-судья проверяет, что каждое фактическое утверждение имеет `source_ids`; без источника блок переписывается.
4. **Безопасность:** NSFW-классификатор по кадрам + стоп-слова.
5. **Дедупликация:** эмбеддинг сценария против истории выпусков.
6. **Антишаблон:** формат отличается от последних N (Format Rotation), структура beat sheet не повторяет предыдущую, визуальные регистры не совпадают поблочно.
7. **Драматургия:** cold_open ≤10 с, open loop заявлен и закрыт, кривая tension немонотонна, есть callback.
8. **Голоса:** инварианты Voice Strategy соблюдены — смена только на границах блоков, ≥12 с на роль, ≤2 переключения в минуту, открытие и финал на main_narrator.

Проверки 6–7 — не перфекционизм. В июле 2026 YouTube переименовал «Repetitious Content» в **«Inauthentic Content»** и прямо запретил монетизацию шаблонного и масштабируемо-повторяющегося контента; в январе 2026 прошла массовая зачистка каналов. Конвейер, штампующий по одному шаблону, — ровно эта мишень.

---

## Структура репозитория

```
contentzavod/
  factory/
    core/       config.py  db.py  state.py  schema.py  keypool.py
                gpu_lock.py  retry.py  storage.py  cli.py
    stages/     s01_trends s02_research s03_beatsheet s04_script s05_voiceplan
                s06_voice s07_humanize s08_align s09_visualplan s10_assets
                s11_sound s12_render s13_clips s14_metadata s15_qa
                s16_publish s17_feedback
    engagement/ beatsheet_rules.py  voice_strategy.py  visual_rhythm.py
                format_rotation.py  hook_bank.py
    providers/
      llm/      gemini.py groq.py openrouter.py llamacpp.py router.py
      tts/      edge.py  voice_library.py  fx_chains.py  humanize.py  rvc.py
      image/    nanobanana.py pollinations.py sdxl_local.py router.py
      video/    pexels.py pixabay.py kenburns.py parallax.py
      audio/    pixabay_music.py sfx_bank.py ducking.py musicgen.py
      publish/  base.py youtube.py telegram.py bluesky.py vk.py rutube.py
                dzen.py tiktok.py instagram.py facebook.py postiz.py
    render/     scene.py concat.py encoder.py subtitles.py counters.py
    qa/         technical.py factcheck.py dedup.py safety.py
                antitemplate.py dramaturgy.py voicecheck.py
  config/       factory.yaml  keys.example.yaml
                voices/{ru,en}.yaml  formats/  styles/  niches/
  assets/       breaths/  irs/  sfx/  luts/  fonts/
  prompts/      research.md beatsheet.md script.md metadata.md clips.md judge.md
  workspace/    jobs/<job_id>/{script.json,audio/,subs/,assets/,scenes/,out/}
  .github/workflows/ci.yml
  docs/         SETUP.md PLATFORMS.md HARDWARE.md ENGAGEMENT.md RISKS.md
```

Три ключевых контракта: `core/schema.py` (JSON сценария), `providers/publish/base.py` (`publish(video, meta) -> PostResult` — новая площадка = один файл), `core/keypool.py` (`acquire(task_class) -> Key` — новый провайдер = одна запись).

---

## Дорожная карта

| Фаза | Содержание | Результат |
|---|---|---|
| **0** ⭐ | Скелет, конфиг, SQLite, **keypool**, GPU-мьютекс, **schema.py**, CLI, CI | `factory keys status` показывает 45 ключей и остатки |
| **1** ⭐ | Research → **beat sheet** → script в JSON + валидатор драматургии | структура, а не ровный текст |
| **2** ⭐ | **Voice Strategy**: библиотека, селектор, инварианты, посегментный TTS | многоголосый ролик |
| **3** ⭐ | Гуманизация слои 1–3, WhisperX, акцентные karaoke-субтитры | живая озвучка |
| **4** ⭐ | Visual plan с ритмом и ротацией регистров, Nano Banana, стоки, счётчики, посценный рендер | **сквозной mp4 одной командой** |
| **5** ⭐ | Sound design: музыкальная драматургия, ducking, тишина, SFX, FX-цепочки ролей | «дорогое» звучание |
| **6** ⭐ | Параллакс 2.5D, LUT по актам, зум-панч, обложки | визуальный уровень |
| **7** ⭐ | Шортсы с перепаковкой (свой хук и финал) | деривативы |
| **8** ⭐ | QA-гейт: все 8 проверок + авто-ремонт | приёмка без человека |
| **9** ⭐ | Контур A: YouTube, Telegram (local Bot API), Bluesky, VK, Rutube | **первое видео вышло само** |
| **10** | Postiz → X, LinkedIn, Pinterest, Threads, Reddit | длинный хвост |
| **11** | TikTok + Meta после одобрения; Дзен через Playwright | полный охват |
| **12** | RVC — уникальные тембры канала | `voice.humanize: rvc` |
| **13** | Retention Feedback: hook_bank, beat_stats, A/B заголовков | завод умнеет сам |
| **14** | Демон, systemd, метрики, алерты, очистка диска | «запустил и всё» |

⭐ — входит в MVP этой задачи.

---

## Риски и митигации

| Риск | Митигация |
|---|---|
| **Inauthentic Content policy** — главный | Format Rotation + QA-проверки 6–7, источники, раскрытие ИИ |
| Мультиголосье звучит искусственно (дробление) | три инварианта Voice Strategy + QA-проверка 8 |
| Акцент мультиязычных голосов на русском | родные ru-RU только под нарратора; акцент используется осознанно под иностранные роли |
| Бан аккаунтов за мультиаккаунт-обход лимитов | пул деградирует штатно: мёртвый ключ → алерт, работа на оставшихся; офлайн-режим llama.cpp как дно |
| edge-tts блокирует при интенсиве | посегментный синтез с джиттером; фолбэк на локальный Piper (CPU) |
| 4 ГБ VRAM — OOM при совмещении моделей | глобальный GPU-мьютекс + явная выгрузка |
| 16 ГБ ОЗУ — OOM на рендере | посценный рендер, concat без перекодирования, параллелизм ≤2 |
| Скрытый per-channel лимит YouTube (429 при свободной квоте) | backoff + перенос джоба на следующие сутки |
| Дзен ломается (Playwright по живому UI) | изолированный адаптер; падение не блокирует остальные площадки |
| 1 ТБ кончится | промежутки удаляются после публикации, история 20 джобов |
| Free-тиры сжимаются без предупреждения | лимиты в конфиге, а не в коде; `factory keys probe` перепроверяет фактически |

---

## Верификация

1. **Пул:** `factory keys probe` — реальный вызов каждым ключом, таблица живых/мёртвых с остатками. Затем принудительный 429 на одном ключе — пул переключился, а не упал.
2. **Драматургия:** `factory beatsheet --topic "..." --show` — таблица beat'ов с кривой tension. Критерий: cold_open ≤10 с, ≥2 пика, callback присутствует. Прогнать на 5 темах — структуры должны различаться.
3. **Voice Strategy:** `factory voiceplan <job_id> --explain` печатает выбранную стратегию, назначение голосов и **каждое переключение с причиной**. Критерий: нет переключений внутри блока (кроме `quote`), нет ролей короче 12 с, ≤2 переключения в минуту.
4. **Голос:** `factory tts demo --humanize off|basic|rvc` — три wav для слепого сравнения; `basic` слышимо живее `off`.
5. **Многоголосье:** прогнать один и тот же текст как `explainer` и как `story` — в первом случае один голос, во втором каст. Переключение только конфигом `content_type`, без правки кода.
6. **Сухой прогон:** `factory run --topic "..." --dry-run` — все стадии, публикация мокается, реальный mp4 в `workspace/jobs/<id>/out/`.
7. **Ресурсы:** лог пика VRAM и RSS. Критерий: VRAM <3.6 ГБ, RSS <12 ГБ — иначе конвейер упадёт под нагрузкой.
8. **Покадрово:** `ffprobe` + кадры на 0/25/50/75/100 % — нет чёрных кадров, субтитры в safe-zone, визуальные регистры соседних блоков различаются.
9. **QA отдельно:** `factory qa <job_id>` на артефакте с вставленными 3 с тишины — отклоняет и чинит. Отдельно подсунуть сценарий с дроблёными ролями — проверка 8 обязана поймать.
10. **Песочница:** YouTube `privacyStatus=private`, тестовый канал Telegram, тестовый аккаунт Bluesky — `PostResult` содержит реальные URL.
11. **Идемпотентность:** повторный запуск стадии не дублирует артефакты и не публикует дважды.
12. **Суточный цикл:** демон на 24 ч → 2–3 видео, форматы не повторились, ретраи отработали, диск не переполнен.
13. `pytest` на keypool (ротация, cooldown, исчерпание), на валидатор схемы сценария, на инварианты Voice Strategy, на Format Rotation, на QA-гейт.

---

## Приложение: промт для Claude Sonnet 5

Вынесен в [PROMPT_SONNET5.md](PROMPT_SONNET5.md).

---

## Источники

Prompt engineering: [Prompting best practices](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices) · [Prompting Claude Sonnet 5](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-sonnet-5)

Голос: [edge-tts](https://github.com/rany2/edge-tts) · [список голосов Edge TTS](https://edgetts.github.io/) · [Azure multilingual voices](https://github.com/MicrosoftDocs/azure-ai-docs/blob/main/articles/ai-services/speech-service/includes/language-support/multilingual-voices.md) · [SSML для production TTS](https://picovoice.ai/blog/ssml-text-to-speech/) · [RVC](https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI) · [tts-with-rvc](https://github.com/Atm4x/tts-with-rvc)

Железо: [AI models on GTX 1650 4GB](https://willitrunai.com/gpus/gtx-1650-4gb) · [SD on low-end GPU 2026](https://blog.vasudevai.in/article/stable-diffusion-low-end-gpu) · [GTX 1650 NVENC / TU117](https://www.techpowerup.com/254861/nvidia-gtx-1650-lacks-turing-nvenc-encoder-packs-voltas-multimedia-engine)

Бесплатные API: [Free LLM APIs 2026](https://openrouter.ai/blog/tutorials/free-llm-apis-compared/) · [Gemini rate limits](https://ai.google.dev/gemini-api/docs/rate-limits) · [Nano Banana free tier](https://www.aifreeapi.com/en/posts/gemini-image-generation-free-api) · [WhisperX](https://github.com/m-bain/whisperx) · [Pexels API](https://www.pexels.com/api/) · [Pollinations](https://github.com/pollinations/pollinations)

Инструменты: [Revideo vs Remotion](https://www.pkgpulse.com/guides/remotion-vs-motion-canvas-vs-revideo-programmatic-video-2026) · [n8n Sustainable Use License](https://docs.n8n.io/sustainable-use-license/) · [Postiz](https://github.com/gitroomhq/postiz-app) · [ClipsAI](https://github.com/ClipsAI/clipsai) · [OpenShorts](https://github.com/mutonby/openshorts) · [telegram-bot-api](https://github.com/tdlib/telegram-bot-api)

Площадки: [YouTube quota 2026](https://www.getphyllo.com/post/youtube-api-limits-how-to-calculate-api-usage-cost-and-fix-exceeded-api-quota) · [TikTok Direct Post](https://developers.tiktok.com/docs/en/content-posting-api-reference-direct-post) · [Instagram Graph API 2026](https://www.netrows.com/blog/instagram-graph-api-guide-2026) · [Bluesky video](https://docs.bsky.app/docs/tutorials/video) · [YouTube Inauthentic Content](https://techcrunch.com/2026/07/20/youtube-clarifies-policies-around-ai-slop-and-upsetting-videos/)
