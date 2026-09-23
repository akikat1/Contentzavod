# Матрица площадок

| Площадка | Что | Механизм | Разовая настройка | Лимиты | В коде |
|---|---|---|---|---|---|
| YouTube | мастер + Shorts | Data API v3, resumable upload, обложка, субтитры | OAuth ✋ (приложение «In production») | `videos.insert` — 1 unit, отдельный бакет 100/сут на проект (с 01.06.2026); скрытый лимит канала → перенос на сутки | `publish/youtube.py` |
| Telegram | мастер | локальный Bot API (2 ГБ) | BotFather, api_id/api_hash | облачный Bot API — 50 МБ | `publish/telegram.py` |
| Bluesky | мастер (> 10 мин → шортс) | `app.bsky.video.uploadVideo` | app password | 10 мин / 300 МБ, суточный лимит видео | `publish/bluesky.py` |
| VK Видео | мастер | `video.save` + загрузка | пользовательский токен ✋ | лимиты API VK → перенос на час | `publish/vk.py` |
| Rutube | мастер | загрузка по ссылке | токен ✋, буфер R2 | — | `publish/rutube.py` |
| TikTok | шортсы | Content Posting API (Direct Post) | аудит приложения ✋ 2–4 нед | до аудита — только private; 6 запросов/мин на токен | `publish/tiktok.py` |
| Instagram | шортсы (Reels) | Graph API: контейнер → публикация | Meta App Review ✋ 2–4 нед, R2 | 25 публикаций/24 ч; 9:16, 5–90 с | `publish/meta.py` |
| Facebook | шортсы (Reels) | Graph API `video_reels` | App Review ✋, R2 | лимиты Graph API | `publish/meta.py` |
| X, LinkedIn, Pinterest, Threads, Reddit, Mastodon, … | шортсы | Postiz (self-hosted, AGPL-3.0) | подключить в UI Postiz | по площадке | `publish/postiz.py` |
| Дзен | — | публичного API нет | — | — | не реализован |

## Поведение очереди
- Строка публикации = (джоб, площадка, вариант). Повторный запуск не публикует дважды.
- Шортсы разносятся на `publish.shorts_stagger_hours` (3 ч).
- Суточный лимит площадки (`daily_limit`) → перенос на следующие сутки.
- Временный отказ (429, 5xx, «видео обрабатывается») → повтор в указанное площадкой время.
- Постоянный отказ → 3 попытки с паузой, затем `failed` и алерт в Telegram.
- Синтетический контент (офлайн-фикстура) и `--dry-run` публикуются только в песочницу `out/published/`
  и не тратят лимиты площадок.

## Добавить площадку
Один файл в `factory/providers/publish/` с классом-наследником `Publisher` (метод `publish(video, meta) -> PostResult`,
исключения `Deferred`/`PublishError`), строка в `registry.py` и секция в `publish.platforms` конфига.
