# Установка и разовая настройка

> **Проще всего — поручить настройку агенту.** Откройте Claude Desktop на своём ПК и вставьте первое сообщение из
> [AGENT_SETUP.md](AGENT_SETUP.md#первое-сообщение-которое-пользователь-вставляет-в-claude-desktop): агент сам
> поставит всё в WSL, проверит ключи, поднимет сервисы, настроит режим 24/7 и позовёт вас только для шагов
> с вашими аккаунтами — через **мастер настройки** (`factory setup wizard`, страница в браузере; секреты
> пишутся прямо в `.env` и в чат не попадают). Что осталось — всегда показывает `factory setup check`.
>
> Ниже — то же самое вручную.

Всё, что здесь описано, делается **один раз**. Дальше завод работает сам: systemd timer каждые 30 минут
запускает тик демона. Шаги, которые принципиально нельзя автоматизировать (OAuth, модерация приложений
TikTok и Meta), помечены ✋.

## 1. Система

**Linux (рекомендуется):**
```bash
sudo apt install ffmpeg espeak-ng fonts-dejavu-core python3.11 python3.11-venv git
```
**Windows:** WSL2 с Ubuntu 24.04 (`wsl --install -d Ubuntu-24.04`). Всё системное ставит
`scripts/setup_wsl.sh --root` (из Windows без пароля: `wsl -u root`), остальное — `scripts/setup_wsl.sh --gpu --piper`.
Драйвер NVIDIA для Windows даёт CUDA внутри WSL2 — GTX 1650 поддерживается; аппаратный энкодер NVENC в WSL2
обычно недоступен, поэтому видео кодирует CPU (libx264) — это штатно. Круглосуточный режим на Windows —
`scripts/windows/install-autostart.ps1` (раздел «Автономный режим»), а не systemd-таймер: WSL выключается,
когда закрыт последний терминал.

```bash
git clone https://github.com/akikat1/Contentzavod && cd Contentzavod
python3.11 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
factory init && factory doctor
```

Необязательно (ставить, только если нужно):
| Пакет | Зачем | Установка |
|---|---|---|
| torch + timm | MiDaS-глубина для параллакса (без неё — эвристика) | `pip install torch timm --index-url https://download.pytorch.org/whl/cu121` |
| faster-whisper | тайминги слов для офлайн-движков TTS (edge-tts отдаёт их сам) | `pip install faster-whisper` |
| rvc-python | слой 4 гуманизации — тембр живого человека | `pip install rvc-python` |
| piper | офлайн-TTS лучше espeak | бинарь с github.com/rhasspy/piper + модели `ru_RU-*.onnx` в `assets/piper/` |
| llama.cpp | офлайн-дно LLM | `llama-server -m qwen3-4b-instruct-q4_k_m.gguf -c 16384 --port 8080` |
| boto3 | буфер Cloudflare R2 | `pip install -e ".[r2]"` |

## 2. Пул ключей LLM ✋

По ключу на строку в файле, затем импорт:
```bash
factory keys import --provider gemini --file gemini.txt       # aistudio.google.com → Get API key
factory keys import --provider groq --file groq.txt           # console.groq.com → API Keys
factory keys import --provider openrouter --file or.txt       # openrouter.ai → Keys
factory keys probe                                            # каждый ключ реальным вызовом
factory keys status                                           # остатки квот
```
Ключи попадают в `config/keys.yaml` (права 600, в `.gitignore`). Если `probe` пишет «модель отвергнута» —
Google/Groq переименовали модель: поправьте `llm.providers.<провайдер>.models` в `config/factory.yaml`.

⚠️ Провайдеры могут расценить несколько аккаунтов ради обхода лимитов как нарушение правил и заблокировать их.
Код одинаково работает с любым числом ключей — сколько заводить, решаете вы.

## 3. Видеоряд

- **Pexels**: pexels.com/api → ключ в `.env` как `PEXELS_API_KEY`. 200 запросов/ч, 20 000/мес; безлимит бесплатно по заявке с атрибуцией.
- **Pixabay**: pixabay.com/api/docs → `PIXABAY_API_KEY`.
- **Изображения** берутся из пула ключей Gemini (Nano Banana); без ключей — Pollinations (без регистрации), затем локальный ComfyUI, затем процедурный фон.
- **Музыка** ✋: у Pixabay Music нет API. Один раз скачайте 5–10 треков на каждое настроение в
  `assets/music/<mood>/` (`tense`, `calm`, `build`, `epic`, `reflective`, `neutral`, `dark`, `uplifting`).
  Без них играет процедурная подложка — работает, но звучит просто.

## 4. Площадки

Все секреты — в `.env` (скопируйте из `.env.example`). Включение — `enabled: true` в `config/local.yaml`:
```yaml
publish:
  platforms:
    youtube: {enabled: true, privacy: private}   # начните с private, проверьте, потом public
    telegram: {enabled: true}
```

### YouTube ✋
1. console.cloud.google.com → новый проект → включить **YouTube Data API v3** и **YouTube Analytics API**.
2. OAuth consent screen → External → добавить себя в тестовые пользователи → **Publish app (In production)**.
   Важно: пока приложение в статусе Testing, Google отзывает refresh token через 7 дней, и завод «молча» встанет.
3. Credentials → OAuth client ID → тип **Desktop app** → `YT_CLIENT_ID`, `YT_CLIENT_SECRET` в `.env`.
4. `factory auth youtube` → согласие в браузере → `YT_REFRESH_TOKEN` в `.env`.
5. Квота: с 01.06.2026 загрузка стоит 1 unit в отдельном бакете (100 видео/сут на проект). У канала есть свой
   скрытый лимит — отказ по нему завод переносит на следующие сутки, а не считает ошибкой.

### Telegram
1. @BotFather → токен → `TG_BOT_TOKEN`; бот — администратор канала; `TG_CHANNEL_ID=@канал`.
2. Облачный Bot API режет файлы на 50 МБ, 5–10-минутный ролик не пролезет. Поэтому — локальный сервер
   (до 2 ГБ): my.telegram.org → `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, затем
   `docker compose -f deploy/docker-compose.yml --env-file .env up -d telegram-bot-api`.
3. `TG_ADMIN_CHAT_ID` — ваш чат для алертов (карантин, мёртвые ключи); включите `alerts.telegram.enabled`.

### Bluesky
Settings → App passwords → `BSKY_HANDLE`, `BSKY_APP_PASSWORD`. Почта аккаунта должна быть подтверждена.
Ролики длиннее 10 минут завод автоматически заменяет первым шортсом.

### VK Видео ✋
Нужен **пользовательский** токен (токен сообщества не умеет `video.save`) со scope `video,wall,groups,offline`:
создайте приложение на dev.vk.com, получите токен через VK ID, `VK_TOKEN` и `VK_GROUP_ID` в `.env`.

### Rutube ✋
Токен API из Rutube Studio → `RUTUBE_TOKEN`. Rutube забирает видео по ссылке — завод сам поднимает временный
туннель Cloudflare (раздел 5).

### TikTok ✋ (2–4 недели)
developers.tiktok.com → приложение → продукт **Content Posting API** со scope `video.publish` → заявка на аудит
с демо-видео полного сценария. До одобрения все посты принудительно приватные — завод это учитывает
(`privacy: SELF_ONLY`). После одобрения: `TIKTOK_ACCESS_TOKEN` в `.env`, `tiktok.enabled: true`.

### Instagram Reels и Facebook Reels ✋ (2–4 недели)
Instagram Professional + страница Facebook + Meta Developer App → разрешение `instagram_business_content_publish`
(и `pages_manage_posts` для Facebook) через App Review со скринкастом — тексты заявок в [APP_REVIEW.md](APP_REVIEW.md).
Обе площадки забирают видео по ссылке — через временный туннель (раздел 5).
Лимит Instagram — 25 публикаций за 24 часа.

### X, LinkedIn, Pinterest, Threads, Reddit, Mastodon — через Postiz
```bash
docker compose -f deploy/docker-compose.yml --env-file .env --profile postiz up -d
```
UI на http://localhost:5000 → подключить площадки → Settings → Public API → `POSTIZ_API_KEY`;
id интеграций — в `publish.platforms.postiz.integrations`.

### Дзен
Публичного API публикации видео нет. В MVP не реализован (см. [RISKS.md](RISKS.md)).

## 5. Ссылка на видео для Rutube, Instagram, Facebook
По умолчанию (`publish.remote_storage.mode: tunnel`) завод на время загрузки поднимает **Cloudflare quick tunnel**:
локальный сервер отдаёт один файл по случайной ссылке `https://…trycloudflare.com/<токен>/…`, туннель закрывается,
как только площадка скачала файл. Без аккаунта и без банковской карты; нужен только `cloudflared`
(ставит `setup_wsl.sh --root`).

Альтернатива — Cloudflare R2 (`mode: r2`): стабильнее, но для включения R2 Cloudflare требует привязать карту.
dash.cloudflare.com → R2 → бакет → публичный доступ (r2.dev) → API-токен S3; в `.env` — `R2_ENDPOINT`, ключи;
`config/local.yaml`: `publish: {remote_storage: {mode: r2, public_base_url: "https://pub-xxxx.r2.dev"}}`.

## 6. Первый настоящий прогон
```bash
factory run --dry-run            # тема из трендов, всё настоящее, публикация — в песочницу out/published/
factory voiceplan <job> --explain
factory qa <job>
factory run                      # YouTube private, остальные площадки — как включены
```

## Автономный режим

**Windows + WSL (ваш случай):**
```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\install-autostart.ps1
```
Скрипт отключает сон при питании от сети, прописывает `vmIdleTimeout=-1` в `.wslconfig`, создаёт задачи
Планировщика «Contentzavod Tick» (при входе и каждые 30 минут), «KeepAlive» (держит WSL и Telegram Bot API) и
«Lock» (блокирует экран сразу после входа) и открывает Sysinternals Autologon — вы вводите пароль Windows, чтобы
после перезагрузки или обновления ПК вошёл сам. Откат — `uninstall-autostart.ps1`.
Задачи «без входа в систему» не используются намеренно: они работают в изолированной сессии 0, где WSL
запускается ненадёжно.

**Чистый Linux:**
```bash
mkdir -p ~/.config/systemd/user
cp deploy/contentzavod.service deploy/contentzavod.timer ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now contentzavod.timer
loginctl enable-linger $USER                     # таймер работает без входа в систему
journalctl --user -u contentzavod -f             # логи
```
Каждый тик: публикует созревшие шортсы и отложенные ролики, раз в 6 часов собирает удержание, создаёт новый
джоб, если план дня (`schedule.videos_per_day`) не выполнен, продолжает прерванный, чистит диск.

## Своя ниша
`config/niches/example.yaml` — тема канала, аудитория, тон, запретные темы, темы-семена. Путь к файлу —
`channel.niche_file`. Голос канала — `channel.main_voice` (id из `config/voices/ru.yaml`).
