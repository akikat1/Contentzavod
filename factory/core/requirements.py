"""Реестр того, что нужно каждой площадке и сервису: переменные .env, где их взять, как проверить.

Один источник правды для `factory setup check` и мастера настройки: добавили площадку —
добавили секцию здесь, и она появилась и в отчёте, и в форме.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .config import Config


@dataclass
class Var:
    config_key: str | None         # путь в конфиге, где лежит имя переменной (…_env)
    default: str                   # имя переменной по умолчанию
    label: str
    secret: bool = True
    filled_by: str | None = None   # «oauth», «telegram_discover», «vk_url» — заполняется помощником, не руками
    optional: bool = False

    def name(self, cfg: Config) -> str:
        return (cfg.get(self.config_key) if self.config_key else None) or self.default


@dataclass
class Section:
    id: str
    title: str
    group: str                     # core | contour_a | contour_b | extra
    steps: list[str]
    links: list[tuple[str, str]]
    vars: list[Var] = field(default_factory=list)
    platform: str | None = None    # ключ в publish.platforms
    note: str = ""


def sections() -> list[Section]:
    P = "publish.platforms"
    return [
        Section("llm", "Ключи LLM: Gemini, Groq, OpenRouter", "core",
                ["Создайте ключи в каждом аккаунте (ссылки ниже).",
                 "Вставьте их в поле по одному в строке — завод сам распределит нагрузку между ними."],
                [("Gemini", "https://aistudio.google.com/apikey"), ("Groq", "https://console.groq.com/keys"),
                 ("OpenRouter", "https://openrouter.ai/settings/keys")]),
        Section("stock", "Стоковое видео: Pexels и Pixabay", "core",
                ["Зарегистрируйтесь и скопируйте API-ключ на странице API."],
                [("Pexels API", "https://www.pexels.com/api/new/"), ("Pixabay API", "https://pixabay.com/api/docs/")],
                [Var("visual.pexels.key_env", "PEXELS_API_KEY", "Ключ Pexels"),
                 Var("visual.pixabay.key_env", "PIXABAY_API_KEY", "Ключ Pixabay", optional=True)]),
        Section("youtube", "YouTube", "contour_a",
                ["Google Cloud → новый проект → включите YouTube Data API v3 и YouTube Analytics API.",
                 "OAuth consent screen → External → добавьте себя в Test users → нажмите «Publish app» "
                 "(«In production»). Иначе Google отзовёт доступ через 7 дней.",
                 "Credentials → Create credentials → OAuth client ID → тип «Desktop app» → скопируйте ID и Secret.",
                 "Сохраните их здесь и нажмите «Авторизовать YouTube»."],
                [("Google Cloud Console", "https://console.cloud.google.com/apis/library/youtube.googleapis.com"),
                 ("YouTube Analytics API", "https://console.cloud.google.com/apis/library/youtubeanalytics.googleapis.com"),
                 ("Экран согласия", "https://console.cloud.google.com/apis/credentials/consent"),
                 ("Credentials", "https://console.cloud.google.com/apis/credentials")],
                [Var(f"{P}.youtube.client_id_env", "YT_CLIENT_ID", "Client ID", secret=False),
                 Var(f"{P}.youtube.client_secret_env", "YT_CLIENT_SECRET", "Client Secret"),
                 Var(f"{P}.youtube.refresh_token_env", "YT_REFRESH_TOKEN", "Refresh token", filled_by="oauth")],
                platform="youtube"),
        Section("telegram", "Telegram", "contour_a",
                ["@BotFather → /newbot → скопируйте токен.",
                 "Добавьте бота администратором в канал (право публиковать сообщения) и напишите в канал любое "
                 "сообщение. Напишите боту /start в личку — туда будут приходить алерты.",
                 "my.telegram.org → API development tools → api_id и api_hash (для локального сервера, "
                 "без него Telegram не пропустит файлы больше 50 МБ).",
                 "Сохраните и нажмите «Найти канал»."],
                [("BotFather", "https://t.me/BotFather"), ("my.telegram.org", "https://my.telegram.org/apps")],
                [Var(f"{P}.telegram.token_env", "TG_BOT_TOKEN", "Токен бота"),
                 Var(None, "TELEGRAM_API_ID", "api_id", secret=False),
                 Var(None, "TELEGRAM_API_HASH", "api_hash"),
                 Var(f"{P}.telegram.chat_id_env", "TG_CHANNEL_ID", "Канал", secret=False, filled_by="telegram_discover"),
                 Var("alerts.telegram.chat_id_env", "TG_ADMIN_CHAT_ID", "Чат для алертов", secret=False,
                     filled_by="telegram_discover", optional=True)],
                platform="telegram"),
        Section("bluesky", "Bluesky", "contour_a",
                ["Settings → Privacy and security → App passwords → Add App Password.",
                 "Почта аккаунта должна быть подтверждена — без этого Bluesky не принимает видео."],
                [("App passwords", "https://bsky.app/settings/app-passwords")],
                [Var(f"{P}.bluesky.handle_env", "BSKY_HANDLE", "Handle (name.bsky.social)", secret=False),
                 Var(f"{P}.bluesky.app_password_env", "BSKY_APP_PASSWORD", "App password")],
                platform="bluesky"),
        Section("vk", "VK Видео", "contour_a",
                ["dev.vk.com → Мои приложения → создайте приложение (Standalone) → скопируйте его ID.",
                 "Нажмите «Войти через VK», разрешите доступ, скопируйте адрес из строки браузера "
                 "(он начинается с https://oauth.vk.com/blank.html#access_token=…) и вставьте в поле.",
                 "ID сообщества — число из адреса группы или её настроек (без минуса)."],
                [("dev.vk.com", "https://dev.vk.com/ru/admin/apps-list")],
                [Var(None, "VK_APP_ID", "ID приложения VK", secret=False),
                 Var(f"{P}.vk.token_env", "VK_TOKEN", "Токен", filled_by="vk_url"),
                 Var(f"{P}.vk.group_id_env", "VK_GROUP_ID", "ID сообщества", secret=False)],
                platform="vk"),
        Section("rutube", "Rutube", "contour_a",
                ["Rutube Studio → настройки → API: получите токен (если раздела нет — запросите через поддержку).",
                 "Rutube забирает видео по ссылке — завод сам поднимет временный туннель Cloudflare."],
                [("Rutube Studio", "https://studio.rutube.ru/")],
                [Var(f"{P}.rutube.token_env", "RUTUBE_TOKEN", "Токен API")],
                platform="rutube"),
        Section("tiktok", "TikTok (после одобрения заявки)", "contour_b",
                ["developers.tiktok.com → приложение → продукт Content Posting API, scope video.publish.",
                 "Подайте заявку на аудит по шаблону docs/APP_REVIEW.md. До одобрения посты только приватные."],
                [("TikTok for Developers", "https://developers.tiktok.com/apps")],
                [Var(f"{P}.tiktok.access_token_env", "TIKTOK_ACCESS_TOKEN", "Access token")],
                platform="tiktok", note="модерация 2–4 недели"),
        Section("instagram", "Instagram Reels (после Meta App Review)", "contour_b",
                ["Instagram Professional + страница Facebook + Meta Developer App.",
                 "App Review на instagram_business_content_publish по шаблону docs/APP_REVIEW.md."],
                [("Meta for Developers", "https://developers.facebook.com/apps/")],
                [Var(f"{P}.instagram.ig_user_id_env", "IG_USER_ID", "IG User ID", secret=False),
                 Var(f"{P}.instagram.access_token_env", "META_ACCESS_TOKEN", "Access token")],
                platform="instagram", note="модерация 2–4 недели"),
        Section("facebook", "Facebook Reels (после Meta App Review)", "contour_b",
                ["То же приложение Meta, разрешение pages_manage_posts, токен страницы."],
                [("Meta for Developers", "https://developers.facebook.com/apps/")],
                [Var(f"{P}.facebook.page_id_env", "FB_PAGE_ID", "Page ID", secret=False),
                 Var(f"{P}.facebook.access_token_env", "META_PAGE_TOKEN", "Page access token")],
                platform="facebook", note="модерация 2–4 недели"),
        Section("postiz", "X, LinkedIn, Pinterest, Threads, Reddit — через Postiz", "extra",
                ["Агент поднимет Postiz в Docker. Откройте http://localhost:5000, создайте аккаунт, подключите "
                 "нужные соцсети.", "Settings → Public API → скопируйте ключ."],
                [("Postiz", "http://localhost:5000")],
                [Var(f"{P}.postiz.api_key_env", "POSTIZ_API_KEY", "API key")],
                platform="postiz", note="по желанию"),
    ]


def section(sid: str) -> Section:
    for s in sections():
        if s.id == sid:
            return s
    raise KeyError(sid)
