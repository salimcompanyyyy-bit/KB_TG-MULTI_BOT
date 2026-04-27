# CONTEXT_AGENT

Last updated: 2026-04-27
Scope: internal working memory for this repository.

## Project Snapshot (EN)

- **Layout:** `src/` — app code (`bot.py`, `db_path.py`); `data/` — **committed** SQLite snapshot (`data/database.db`) for backup/sharing; `scripts/` — run (`run_bot.bat`), DB import/export, `publish-github.*`; `context/`, `VERSION`, root `.env[.example]`, `requirements.txt`.
- **Telegram bot token** is read from env `BOT_TOKEN` or a local `.env` in repo root (see `.env.example`), never hardcoded in source; entrypoint: `python src/bot.py` or `scripts\run_bot.bat`.
- **Two database roles:** (1) **live DB** from `get_db_path()` in `src/db_path.py` — default **outside the repo** (e.g. `%LOCALAPPDATA%\\KB_TG-MULTI_BOT\\database.db` on Windows) so `git checkout` / rollback does **not** overwrite working data; optional `DB_PATH` in `.env`. (2) **`data/database.db` in Git** — backup. **Owner** (`OWNER_ID`): Admin «База / импорт» — sync (export live→`data/`, import `data/`→live, confirm on import) via SQLite `backup()`; CLI `scripts/import_database_from_repo.py` / `export_database_to_repo.py`. Do not add `data/*.db` to `.gitignore` without explicit agreement.
- Domain: Telegram real estate workflow (staff publishing + client-facing channel usage).
- Active idea backlog source: `context/CONTEXT_ИДЕЙ.md`.
- Current important behavior:
  - Listing number uses `posts.id`.
  - Manual deletion in channel does not remove DB row automatically.
  - Admin has DB delete flow for listings.
  - Large Telegram profile preview in post footer was disabled for new posts.
  - New staff users now pass mandatory access request flow (full name, phone, Telegram username, userinfo ID).
  - Admin staff section now has real list/cards/actions and access request moderation.
  - Staff section UI was expanded: overview counters, stricter admin-only access, and request priority flag with moderation sorting.
  - Admin reports/search are now live: stats dashboard, logs with filters, CSV/XLSX export, post search by ad number and staff ID.
  - Channel posts now include quick contact button to employee with prefilled message containing listing number.

## Working Rules For Agent (EN)

- Read `context/CONTEXT_ИДЕЙ.md` before substantial edits.
- Keep diffs minimal and focused.
- Do not revert unrelated user changes.
- Auto-commit substantial completed changes; push only when user explicitly asks.
- Keep user-facing communication concise and in Russian.
- If user message looks like random Latin letters, interpret it as Russian typed with English keyboard layout and respond to converted meaning (ask briefly if ambiguous).
- Project rule now requires semantic versioning in root `VERSION` (current baseline: `1.0.0`) and commit subject format `vX.Y.Z: ...`.
- Commit title standard is strict: `vX.Y.Z: <тип>` where `<тип>` is one Russian word from fixed list (`добавление`, `исправление`, `функция`, `рефакторинг`, `контекст`, `база`, `безопасность`, `производительность`).
- ВАЖНО: даже технические коммиты (включая `git revert`) обязательно оформлять по шаблону `vX.Y.Z: <тип>`; стандартные сообщения вида `Revert "..."` не использовать.
- All clarifying questions to user must be asked via Ask Question only.
- Version policy for this repository is fixed to `1.0.x` by default; increment PATCH only unless user explicitly approves MINOR/MAJOR bump.

## Known Pending Product Items (EN)

- Client entry strategy is fixed: no Mini App, continue with current bot DM search flow.
- Optional LK improvements: monthly publication counter, drafts.
- Admin next item:
  - Full admin delete flow for listing: remove from channel + delete all related DB/search traces.

---

## Commit Type Dictionary (EN)

Allowed commit types (and only these): `добавление`, `исправление`, `функция`, `рефакторинг`, `контекст`, `база`, `безопасность`, `производительность`.

---

## Перевод на русский (RU)

### Снимок проекта

- **Структура:** `src/` — код бота (`bot.py`, `db_path.py`); `data/` — закоммиченный снимок SQLite (`data/database.db`); `scripts/` — `run_bot.bat`, импорт/экспорт БД, публикация в GitHub; `context/`, `VERSION`, корневой `.env` / `.env.example`, `requirements.txt`.
- **Токен** — из `BOT_TOKEN` / `.env` в корне репо; запуск: `python src/bot.py` или `scripts\run_bot.bat`.
- **Два уровня БД:** (1) **живая** — `get_db_path()` в `src/db_path.py` (по умолчанию **вне** репо); при необходимости `DB_PATH` в `.env`. (2) **`data/database.db` в Git** — снимок. **Владелец** (`OWNER_ID`): в админке «База / импорт» — кнопки синхронизации (экспорт в `data/`, импорт из `data/` в рабочую БД, с подтверждением на импорт). Скрипты в `scripts/` остаются для ручного запуска. В `.gitignore` `data/*.db` не прятать без согласования.
- Домен: Telegram-процесс по недвижимости (публикация сотрудниками + клиентский канал).
- Актуальный бэклог идей: `context/CONTEXT_ИДЕЙ.md`.
- Важные текущие моменты:
  - Номер объявления берется из `posts.id`.
  - Ручное удаление поста в канале не удаляет запись из БД автоматически.
  - У админа есть сценарий удаления объявления из БД.
  - Большая плашка Telegram-профиля внизу поста отключена для новых публикаций.
  - Новые сотрудники проходят обязательную заявку на доступ (ФИО, телефон, Telegram username, ID из @userinfobot).
  - Раздел админки по сотрудникам теперь рабочий: список, карточка, редактирование, роли, обработка заявок.
  - Окно «Сотрудники» расширено: сводные счетчики, строгий доступ только админам и приоритет заявок с сортировкой для модерации.
  - Блок админ-отчетов и поиска работает: статистика, логи с фильтрами, экспорт CSV/XLSX, поиск публикаций по № и ID сотрудника.
  - В постах канала добавлена кнопка быстрого контакта с сотрудником с автотекстом по номеру объявления.

### Рабочие правила для агента

- Перед существенными правками читать `context/CONTEXT_ИДЕЙ.md`.
- Держать изменения минимальными и точечными.
- Не откатывать несвязанные изменения пользователя.
- Существенные завершенные изменения коммитить автоматически; push делать только по явной просьбе пользователя.
- Писать пользователю кратко и на русском.
- Если сообщение похоже на случайный набор латиницы, трактовать его как русский текст в английской раскладке и отвечать по конвертированному смыслу (при сомнениях коротко уточнять).
- Правило проекта теперь требует семантическую версию в корневом `VERSION` (текущая базовая: `1.0.0`) и формат заголовка коммита `vX.Y.Z: ...`.
- Стандарт заголовка коммита строгий: `vX.Y.Z: <тип>`, где `<тип>` — одно русское слово из фиксированного списка (`добавление`, `исправление`, `функция`, `рефакторинг`, `контекст`, `база`, `безопасность`, `производительность`).
- ВАЖНО: даже технические коммиты (включая `git revert`) обязательно оформлять по шаблону `vX.Y.Z: <тип>`; стандартные сообщения вида `Revert "..."` не использовать.
- Любые уточняющие вопросы пользователю задавать только через Ask Question.
- Политика версий для этого репозитория по умолчанию фиксирована на `1.0.x`; повышать только PATCH, пока пользователь явно не согласует MINOR/MAJOR.

### Известные незавершенные пункты

- Стратегия клиентского входа зафиксирована: без Mini App, развиваем текущий поиск через личку бота.
- Опциональные улучшения ЛК: счетчик публикаций за месяц и черновики.
- Следующая задача админки:
  - Полное удаление объявления из админки: удалять из канала и убирать все связанные следы в БД/поиске.

### Словарь типов коммитов

Разрешены только эти типы коммитов: `добавление`, `исправление`, `функция`, `рефакторинг`, `контекст`, `база`, `безопасность`, `производительность`.
