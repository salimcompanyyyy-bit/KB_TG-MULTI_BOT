# Шпаргалка: запуск TG-бота

Полная инструкция: [CONTEXT_RUN.md](CONTEXT_RUN.md) · безопасность токена: [CONTEXT_SECURITY.md](CONTEXT_SECURITY.md)

---

## Куда заходить

```text
C:\Users\Kawa\OneDrive\Рабочий стол\Кб\KB_TG_бот_по_недвижке
```

---

## Один раз (зависимости)

```powershell
cd "C:\Users\Kawa\OneDrive\Рабочий стол\Кб\KB_TG_бот_по_недвижке"
python -m pip install -U aiogram
```

---

## Каждый запуск

```powershell
cd "C:\Users\Kawa\OneDrive\Рабочий стол\Кб\KB_TG_бот_по_недвижке"
python bot.py
```

- Окно **не закрывать**, пока бот нужен.  
- Стоп: **`Ctrl+C`**.  
- Если `python` не находится → попробуй **`py bot.py`**.

---

## Проверка

В Telegram: открыть бота → **Start** или `/start`.

---

## Не запускать дважды

Один токен = один процесс `python bot.py`. Второй запуск — отключи первый.
