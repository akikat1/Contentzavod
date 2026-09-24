# Доводка настройки на ПК пользователя — runbook для агента

Ты — Claude Code в Claude Desktop на Windows пользователя. Завод живёт в WSL (Ubuntu). Твоя задача — довести
`factory setup check` до «готов к автономной работе», сделав сам всё, что можно, и позвав человека только туда,
где нужны его аккаунты, пароли и согласия.

## Первое сообщение, которое пользователь вставляет в Claude Desktop

```text
Ты на моём ПК: Windows + WSL (Ubuntu). Доведи настройку моего контент-завода до конца, режим работы 24/7.
Репозиторий: https://github.com/akikat1/Contentzavod. Он должен лежать в WSL в ~/Contentzavod — если его там нет,
склонируй внутрь WSL (не в Windows). Рабочая ветка: main, если в ней уже есть папка factory/, иначе
claude/video-content-automation-plan-ky26ux. Затем прочитай docs/AGENT_SETUP.md из репозитория
(\\wsl.localhost\<дистрибутив>\home\<пользователь>\Contentzavod\docs\AGENT_SETUP.md) и работай строго по нему.
Я часть уже сделал: вставил API-ключи и поставил WSL — найди, что есть, и не делай заново.
Секреты мне в чат не пиши и не проси присылать — для них есть мастер настройки.
Перед изменением настроек питания, установкой Docker и перезагрузкой — спроси меня.
```

## Правила

1. **Секреты.** Не печатай ключи и токены в чат, логи и коммиты — только маску (`fz env check`). Не проси
   пользователя прислать секрет в чат: он вводит их в мастере настройки, который пишет прямо в `.env`.
2. **Только человек:** пароли, 2FA, вход в аккаунты, принятие условий площадок, подача заявок TikTok/Meta,
   пароль Windows в Autologon. Ты готовишь всё вокруг и говоришь, что именно нажать.
3. **Спрашивай один раз перед:** изменением настроек питания Windows, установкой Docker, `wsl --shutdown`,
   перезагрузкой ПК, включением публикации на настоящие площадки.
4. **Код.** Нашёл баг — исправь, прогони `pytest -q` и `ruff check factory tests` в WSL, закоммить локально.
   Push в GitHub — только с разрешения пользователя.
5. **Прогресс** веди в `~/Contentzavod/data/SETUP_PROGRESS.md` (чек-лист шагов ниже). После перезагрузки ПК
   новая сессия продолжит с того же места.
6. Команды в WSL — через `wsl.exe -d <дистрибутив> -- bash -lc '...'`; системные — добавь `-u root`
   (пароль не нужен). Файлы в WSL правь через `\\wsl.localhost\<дистрибутив>\...` только с переводами строк LF.
7. Между вызовами инструмента переменные и функции PowerShell могут не сохраняться — подставляй значения
   (`$D`, `$U` ниже) в каждую команду заново.

## Шаги

### 1. Разведка
```powershell
$env:WSL_UTF8 = '1'
wsl.exe -l -v                                   # дистрибутив (обычно Ubuntu или Ubuntu-24.04)
$D = 'Ubuntu'                                   # подставь фактическое имя
wsl.exe -d $D -- whoami                         # пользователь WSL
wsl.exe -d $D -- bash -lc 'ls -la ~/Contentzavod 2>/dev/null | head; git -C ~/Contentzavod status -sb 2>/dev/null | head -1'
```
Ищи уже сделанное пользователем: `~/Contentzavod/.env`, `~/Contentzavod/config/keys.yaml`, другие копии
(`find ~ -maxdepth 3 -name keys.yaml -path "*Contentzavod*"`, папки `Contentzavod` в `C:\Users\<user>`).
Нашёл секреты в копии на стороне Windows — перенеси файлы в WSL-репозиторий (`cp` из `/mnt/c/...`), клонировать
репозиторий в Windows не надо: переводы строк CRLF ломают shell-скрипты.

Репозитория в WSL нет → клонируй. Если он приватный и git просит логин, подключи Git Credential Manager из
Windows: `git config --global credential.helper "/mnt/c/Program\ Files/Git/mingw64/bin/git-credential-manager.exe"`
(окно входа в GitHub откроется у пользователя) или попроси пользователя выполнить `gh auth login`.
```powershell
wsl.exe -d $D -- bash -lc 'git clone https://github.com/akikat1/Contentzavod ~/Contentzavod'
wsl.exe -d $D -- bash -lc 'cd ~/Contentzavod && git fetch origin && (git ls-tree -d origin/main factory >/dev/null 2>&1 && git checkout main || git checkout claude/video-content-automation-plan-ky26ux) && git pull'
```

### 2. Установка в WSL
```powershell
$U = (wsl.exe -d $D -- whoami).Trim()
wsl.exe -d $D -u root -- bash "/home/$U/Contentzavod/scripts/setup_wsl.sh" --root --user $U
```
Если скрипт включил systemd — предупреди пользователя, что окна WSL закроются, и выполни `wsl.exe --shutdown`.
Затем пользовательская часть (PyTorch с CUDA и голоса Piper — ~3 ГБ загрузки):
```powershell
wsl.exe -d $D -- bash -lc 'cd ~/Contentzavod && bash scripts/setup_wsl.sh --gpu --piper'
```

### 3. Команда `fz`
Скопируй скрипты Windows в локальную папку (запуск .ps1 прямо с `\\wsl.localhost` блокируется политиками):
```powershell
$bin = "$env:LOCALAPPDATA\Contentzavod\bin"; New-Item -ItemType Directory -Force $bin | Out-Null
Copy-Item "\\wsl.localhost\$D\home\$U\Contentzavod\scripts\windows\*.ps1" $bin -Force
powershell -NoProfile -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\Contentzavod\bin\fz.ps1" setup check
```
Дальше в тексте **`fz <аргументы>`** — сокращение для
`powershell -NoProfile -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\Contentzavod\bin\fz.ps1" <аргументы>`
(= `factory <аргументы>` внутри WSL). Для долгих команд — `-Detached` первым аргументом: своё свёрнутое окно.
Политика выполнения по умолчанию может запрещать `.ps1`, поэтому всегда через `-ExecutionPolicy Bypass -File`.
Дистрибутив скрипт находит сам; если их несколько — задай `$env:CZ_DISTRO`.

### 4. Ключи LLM
```powershell
fz keys status
fz keys probe
```
- Ключей нет, но пользователь говорит, что вставлял → поищи их (шаг 1), импортируй: `fz keys import --provider
  gemini --file <файл>` (файл — по ключу в строке, потом удали его).
- «модель отвергнута» → `fz keys models`, выбери актуальные (для Gemini — flash для текста, flash-lite для
  метаданных, flash-image для картинок) и запиши в `config/local.yaml`:
  ```yaml
  llm:
    providers:
      gemini:
        models: {research: <...>, beatsheet: <...>, script: <...>, metadata: <...>, judge: <...>, image: <...>}
  ```
  Повтори `fz keys probe`.

### 5. Ниша канала (спроси пользователя)
Единственное содержательное решение: о чём канал. Спроси тему, аудиторию, тон, запретные темы, 5–10 тем-семян.
Создай `config/niches/<имя>.yaml` по образцу `config/niches/example.yaml` и пропиши в `config/local.yaml`:
`channel: {name: "<название>", niche_file: config/niches/<имя>.yaml}`.

### 6. Мастер настройки (здесь работает человек)
```powershell
fz -Detached setup wizard --open
```
Откроется страница в браузере. По `fz setup check --json` (`summary.human_todo`) скажи пользователю, какие
секции заполнить и в каком порядке: ключи LLM → Pexels → YouTube → Telegram → Bluesky → VK → Rutube.
В каждой секции есть шаги и ссылки — пересказывать их не надо, достаточно «откройте секцию X».
Пока пользователь заполняет — делай шаги 7–8. Проверка того, что он ввёл: `fz setup verify`.

### 7. Telegram (после того как в мастере сохранены токен, api_id и api_hash)
```powershell
wsl.exe -d $D -- bash -lc 'cd ~/Contentzavod && docker compose -f deploy/docker-compose.yml --env-file .env up -d telegram-bot-api'
fz auth telegram          # найдёт канал и личный чат; если канала нет — скажи пользователю, что сделать (выведет)
fz auth telegram-local    # logOut из облака и переход на локальный сервер (файлы до 2 ГБ)
```

### 8. Postiz (если пользователь хочет X, LinkedIn, Pinterest, Threads, Reddit)
```powershell
wsl.exe -d $D -- bash -lc 'cd ~/Contentzavod && docker compose -f deploy/docker-compose.yml --env-file .env --profile postiz up -d'
```
Пользователь открывает http://localhost:5000, подключает соцсети, копирует API-ключ в мастер. id интеграций
возьми из `fz setup verify postiz` / API Postiz и впиши в `publish.platforms.postiz.integrations`.

### 9. Пробный прогон по-настоящему (публикация в песочницу)
```powershell
fz run --dry-run
```
Потом разбери результат (`fz jobs` → id джоба):
- `workspace/jobs/<job>/qa_report.json` — все 8 проверок; `manifest.json` — время стадий, пики RAM/VRAM, заметки;
- `fz voiceplan <job> --explain` — схема голосов;
- кадры: `wsl ... ffmpeg -ss 20 -i workspace/jobs/<job>/out/master.mp4 -frames:v 1 /tmp/f.png`, посмотри через
  `\\wsl.localhost\...`;
- `manifest.json → notes`: процедурные фоны вместо картинок = не работают ключи Gemini/Pollinations;
  espeak вместо edge-tts = нет доступа к серверу edge-tts.
Попроси пользователя посмотреть `out/master.mp4` и один шортс. Исправь найденное, повтори.

### 10. Первая настоящая публикация
В `config/local.yaml` включи YouTube (`privacy: private`) и Telegram, запусти `fz run`, проверь ссылки
(`fz publish`). Пользователь смотрит ролик в YouTube Studio и пост в канале. Когда он доволен — включи остальные
площадки контура A и, по его слову, `publish.platforms.youtube.privacy: public`.

### 11. Режим 24/7 (спроси подтверждение)
```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\Contentzavod\bin\install-autostart.ps1"
```
Скрипт отключит сон от сети, пропишет `vmIdleTimeout=-1`, создаст задачи Tick / KeepAlive / Lock и откроет
Sysinternals Autologon — пользователь вводит пароль Windows и нажимает Enable. Затем с разрешения — перезагрузка.
Перед перезагрузкой запиши прогресс в `SETUP_PROGRESS.md` и скажи пользователю: «после входа откройте Claude
Desktop и напишите: продолжи настройку контент-завода после перезагрузки».
После перезагрузки: через 5 минут `fz setup check` (пункты Windows — зелёные), `fz jobs`, хвост
`data/logs/tick.log`.

### 12. Заявки TikTok и Meta
Дай пользователю `docs/APP_REVIEW.md`: тексты заявок и сценарий демо-видео готовы, ему нужно подать. Ответ через
2–4 недели; после одобрения — токены в мастер, включить площадку.

### 13. Финал
`fz setup check --probe` — всё зелёное, кроме площадок на модерации и опциональных пунктов. Отчитайся
пользователю: что работает, что ждёт модерации, где смотреть логи (`data/logs/`), как остановить
(`uninstall-autostart.ps1`).
