# CONTEXT_AGENT

Last updated: 2026-04-23
Scope: internal working memory for this repository.

## Project Snapshot (EN)

- Stack: Python bot (`bot.py`) with SQLite (`database.db`).
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

## Working Rules For Agent (EN)

- Read `context/CONTEXT_ИДЕЙ.md` before substantial edits.
- Keep diffs minimal and focused.
- Do not revert unrelated user changes.
- Auto-commit substantial completed changes; push only when user explicitly asks.
- Keep user-facing communication concise and in Russian.
- If user message looks like random Latin letters, interpret it as Russian typed with English keyboard layout and respond to converted meaning (ask briefly if ambiguous).
- Project rule now requires semantic versioning in root `VERSION` (current baseline: `1.0.0`) and commit subject format `vX.Y.Z: ...`.
- Commit title standard is strict: `vX.Y.Z: <тип>` where `<тип>` is one Russian word from fixed list (`добавление`, `исправление`, `функция`, `рефакторинг`, `контекст`, `база`, `безопасность`, `производительность`).
- All clarifying questions to user must be asked via Ask Question only.
- Version policy for this repository is fixed to `1.0.x` by default; increment PATCH only unless user explicitly approves MINOR/MAJOR bump.

## Known Pending Product Items (EN)

- Client entry strategy is fixed: no Mini App, continue with current bot DM search flow.
- Optional LK improvements: monthly publication counter, drafts.
- Admin placeholders still pending:
  - `adm_stats`
  - `export_stats`
  - `adm_logs`
  - `export_logs`
  - `adm_search_posts`

---

## Commit Type Dictionary (EN)

Allowed commit types (and only these): `добавление`, `исправление`, `функция`, `рефакторинг`, `контекст`, `база`, `безопасность`, `производительность`.

---

## Перевод на русский (RU)

### Снимок проекта

- Стек: Python-бот (`bot.py`) + SQLite (`database.db`).
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

### Рабочие правила для агента

- Перед существенными правками читать `context/CONTEXT_ИДЕЙ.md`.
- Держать изменения минимальными и точечными.
- Не откатывать несвязанные изменения пользователя.
- Существенные завершенные изменения коммитить автоматически; push делать только по явной просьбе пользователя.
- Писать пользователю кратко и на русском.
- Если сообщение похоже на случайный набор латиницы, трактовать его как русский текст в английской раскладке и отвечать по конвертированному смыслу (при сомнениях коротко уточнять).
- Правило проекта теперь требует семантическую версию в корневом `VERSION` (текущая базовая: `1.0.0`) и формат заголовка коммита `vX.Y.Z: ...`.
- Стандарт заголовка коммита строгий: `vX.Y.Z: <тип>`, где `<тип>` — одно русское слово из фиксированного списка (`добавление`, `исправление`, `функция`, `рефакторинг`, `контекст`, `база`, `безопасность`, `производительность`).
- Любые уточняющие вопросы пользователю задавать только через Ask Question.
- Политика версий для этого репозитория по умолчанию фиксирована на `1.0.x`; повышать только PATCH, пока пользователь явно не согласует MINOR/MAJOR.

### Известные незавершенные пункты

- Стратегия клиентского входа зафиксирована: без Mini App, развиваем текущий поиск через личку бота.
- Опциональные улучшения ЛК: счетчик публикаций за месяц и черновики.
- В админке остаются заглушки:
  - `adm_stats`
  - `export_stats`
  - `adm_logs`
  - `export_logs`
  - `adm_search_posts`

### Словарь типов коммитов

Разрешены только эти типы коммитов: `добавление`, `исправление`, `функция`, `рефакторинг`, `контекст`, `база`, `безопасность`, `производительность`.
