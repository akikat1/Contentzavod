# Заявки TikTok и Meta — готовые тексты

Обе площадки проверяют приложение вручную (2–4 недели) и просят ссылки на сайт, политику конфиденциальности и
условия использования. Для личного приложения подойдёт простая страница: опубликуйте тексты из раздела
«Политика» через GitHub Pages, Telegraph (telegra.ph) или Google Sites.

## Политика конфиденциальности (шаблон)

> **Политика конфиденциальности «<Название канала> Publisher»**
> Приложение используется владельцем канала для публикации собственных видео в собственные аккаунты.
> Приложение получает доступ только к аккаунтам, которые подключил владелец, и только для загрузки видео и
> чтения статистики этих видео. Данные третьих лиц не собираются, не продаются и не передаются. Токены доступа
> хранятся локально на компьютере владельца. Удаление данных: отключите приложение в настройках аккаунта
> площадки; по вопросам — <ваш email>.

**Условия использования:** «Приложение предназначено только для публикации контента владельцем в собственные
аккаунты. Владелец отвечает за соответствие публикуемого контента правилам площадки.»

## TikTok — Content Posting API (Direct Post)

- **App name:** `<Канал> Publisher`
- **Category:** Entertainment / Education (по теме канала)
- **Description:** «Personal publishing tool. The owner of the TikTok account publishes their own short
  educational videos to their own account directly from a local video production pipeline.»
- **Products:** Login Kit, Content Posting API (Direct Post), scope `video.publish` (+ `user.info.basic`)
- **How will you use video.publish:** «The app uploads the owner's own finished vertical videos (9:16, 15–60 s)
  to the owner's own account. Before posting, the app queries creator_info and lets the owner choose the privacy
  level; captions and AI-generated content labels (is_aigc) are set by the owner. The app never posts to other
  users' accounts.»
- **Демо-видео (1–2 мин, запись экрана):** вход в TikTok через Login Kit → экран выбора ролика → показ privacy
  level из creator_info и согласия пользователя → публикация → пост в профиле. TikTok проверяет, что пользователь
  видит превью, заголовок, настройки приватности и явно подтверждает публикацию: покажите это в ролике.
  Агент может подготовить простую страницу подтверждения публикации для записи демо — попросите его.

## Meta — Instagram Reels и Facebook Reels

- Тип приложения: **Business**; продукты: Instagram API (Instagram Login или Facebook Login), Facebook Login for
  Business.
- **Разрешения:** `instagram_business_basic`, `instagram_business_content_publish`, `pages_show_list`,
  `pages_read_engagement`, `pages_manage_posts` (для Facebook Reels).
- **Use case (для каждого разрешения):** «The app is used by the owner of the Instagram Professional account and
  Facebook Page to publish their own Reels produced by their own video pipeline. The app creates a media
  container with the owner's video URL and caption, waits for processing, and publishes it to the owner's
  account. No data of other users is accessed.»
- **Скринкаст:** вход через Facebook Login → выбор страницы и Instagram-аккаунта → публикация Reel из приложения →
  опубликованный Reel в Instagram. Для каждого разрешения — отдельный фрагмент, где видно его использование.
- **Data handling:** данные не передаются третьим лицам, токены хранятся локально, удаление — отзыв доступа в
  настройках Facebook.

После одобрения: токены в мастер настройки (секции TikTok / Instagram / Facebook), включить площадку.
Для TikTok до одобрения публикации принудительно приватные — завод это учитывает.
