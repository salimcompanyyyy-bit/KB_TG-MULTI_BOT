# CONTEXT_AGENT

Last updated: 2026-04-23
Scope: internal working memory for this repository.

## Project Snapshot (EN)

- Stack: Python bot (`bot.py`) with SQLite (`database.db`).
- Domain: Telegram real estate workflow (staff publishing + client-facing channel usage).
- Active idea backlog source: `context/CONTEXT_КЛИЕНТСКИЙ_ИДЕЙ.md`.
- Current important behavior:
  - Listing number uses `posts.id`.
  - Manual deletion in channel does not remove DB row automatically.
  - Admin has DB delete flow for listings.
  - Large Telegram profile preview in post footer was disabled for new posts.

## Working Rules For Agent (EN)

- Read `context/CONTEXT_КЛИЕНТСКИЙ_ИДЕЙ.md` before substantial edits.
- Keep diffs minimal and focused.
- Do not revert unrelated user changes.
- Auto-commit substantial completed changes; push only when user explicitly asks.
- Keep user-facing communication concise and in Russian.
- If user message looks like random Latin letters, interpret it as Russian typed with English keyboard layout and respond to converted meaning (ask briefly if ambiguous).

## Known Pending Product Items (EN)

- Choose long-term client entry from channel: Mini App vs bot DM flow (or both).
- Optional LK improvements: monthly publication counter, drafts.
- Admin placeholders still pending:
  - `adm_stats`
  - `export_stats`
  - `adm_logs`
  - `export_logs`
  - `adm_staff_list`
  - `adm_access_requests`
  - `adm_search_posts`

---

## Перевод на русский (RU)

### Снимок проекта

- Стек: Python-бот (`bot.py`) + SQLite (`database.db`).
- Домен: Telegram-процесс по недвижимости (публикация сотрудниками + клиентский канал).
- Актуальный бэклог идей: `context/CONTEXT_КЛИЕНТСКИЙ_ИДЕЙ.md`.
- Важные текущие моменты:
  - Номер объявления берется из `posts.id`.
  - Ручное удаление поста в канале не удаляет запись из БД автоматически.
  - У админа есть сценарий удаления объявления из БД.
  - Большая плашка Telegram-профиля внизу поста отключена для новых публикаций.

### Рабочие правила для агента

- Перед существенными правками читать `context/CONTEXT_КЛИЕНТСКИЙ_ИДЕЙ.md`.
- Держать изменения минимальными и точечными.
- Не откатывать несвязанные изменения пользователя.
- Существенные завершенные изменения коммитить автоматически; push делать только по явной просьбе пользователя.
- Писать пользователю кратко и на русском.
- Если сообщение похоже на случайный набор латиницы, трактовать его как русский текст в английской раскладке и отвечать по конвертированному смыслу (при сомнениях коротко уточнять).

### Известные незавершенные пункты

- Выбрать долгосрочный клиентский вход из канала: Mini App, личка бота или оба варианта.
- Опциональные улучшения ЛК: счетчик публикаций за месяц и черновики.
- В админке остаются заглушки:
  - `adm_stats`
  - `export_stats`
  - `adm_logs`
  - `export_logs`
  - `adm_staff_list`
  - `adm_access_requests`
  - `adm_search_posts`
