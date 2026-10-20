# Сборка и передача демо для macOS

## Сборка через GitHub с Windows

Репозиторий: `vergilum/visual-rozdr`. Приложение не использует GitHub во время мероприятия.
В репозиторий входят исходники и пустой `.env.example`, без `.env`, базы, медиа и бинарных сборок.

1. Откройте репозиторий → Actions → **Build Mac demo** → **Run workflow**.
2. Выберите `both`, `arm64` либо `x64`. По умолчанию собираются обе архитектуры.
3. Дождитесь зелёного результата выбранных jobs. При ошибке сборки архив не публикуется.
4. Внизу страницы запуска скачайте artifacts `VisualPozdr-mac-arm64-demo` / `VisualPozdr-mac-x64-demo`.
5. Распакуйте внешний ZIP от GitHub. Внутри находится уже готовый `VisualPozdr-mac-…-demo.zip`,
   инструкция `READ-ME-MAC.md`, сведения `BUILD-INFO.json` и `SHA256SUMS.txt`.
6. Отправьте пользователю внутренний ZIP. Инструкция включена также внутрь этого ZIP.
   Распаковывать/пересобирать `.app` на Windows перед отправкой не следует: macOS использует symlinks и executable bits.

С GitHub CLI из PowerShell можно запустить и скачать сборку:

```powershell
gh workflow run mac-demo.yml --repo vergilum/visual-rozdr -f architecture=both
gh run list --repo vergilum/visual-rozdr --workflow mac-demo.yml
gh run watch <RUN_ID> --repo vergilum/visual-rozdr
gh run download <RUN_ID> --repo vergilum/visual-rozdr --dir artifacts/mac-demo
```

Workflow запускается вручную, не на каждый push. Нужны включённые Actions и доступные минуты
macOS runners в аккаунте; тариф GitHub для приватного репозитория проверяется владельцем.
Артефакты хранятся 14 дней, поэтому скачайте их к себе после успешной сборки.

## Что делает workflow

- Нативные runners `macos-15` (arm64) и `macos-15-intel` (x64), Python 3.12, Node 24.
- Сборка FFmpeg 7.1.1 и x264 из фиксированных исходников с проверкой SHA-256.
  FFmpeg/x264 связаны статически, допустимы только системные dylib из `/usr/lib` и `/System/Library`.
  Точные URL, хеши и лицензии включены в `Contents/Resources/runtime/ffmpeg/`.
- Установка закреплённых Python-зависимостей и `npm ci` с lockfile.
- Проверки Electron-платформы, сборка Vite, PyInstaller и Electron ZIP.
- Backend-тесты, проверка bundled backend, H.264/AAC/JPEG, WebSocket и штатной остановки.
- Запуск настоящего Electron из `.app` в отдельном временном профиле без Telegram-токена:
  панель, окно показа, обе темы, PNG, QR и регистрация экрана.
- Ad-hoc подпись и `codesign --verify --deep --strict`; это не Developer ID и не notarization.
- Повторная распаковка готового ZIP и GUI smoke-test извлечённого приложения.

Это подтверждает сборку и автоматические проверки на ОС runner, но не приёмку
Golden Gate 27.0.1, реального Telegram, проектора, звука и длительного мероприятия.
Отчёт пользователя вести по `docs/MAC_DEMO_USER_GUIDE.md`.

## Локальная сборка на Mac

Нужны Xcode Command Line Tools, Python 3.12, Node 24 и `pkg-config` для сборщика.
Пользователю готового приложения они не нужны.

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/build_mac_media.py
npm --prefix frontend ci
npm --prefix frontend run desktop:test
npm --prefix frontend run desktop:package:mac
```

Для тестирования исходников создайте личный `.env` из `.env.example`.
Для публикации без предупреждений Gatekeeper понадобится отдельный этап:
Developer ID Application, hardened runtime/entitlements, notarization и проверка скачанной поставки.
Секреты подписи должны находиться в GitHub Secrets, не в исходниках.
