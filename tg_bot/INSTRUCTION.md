# 📖 Покрокова інструкція з налаштування та запуску Telegram Music Bot

Цей посібник допоможе вам створити бота у Telegram, запустити його локально на ПК та налаштувати цілодобову (24/7) роботу на Linux VPS сервері.

---

## 📌 Зміст
1. [Крок 1. Створення бота та налаштування Inline Mode](#крок-1-створення-бота-та-налаштування-inline-mode)
2. [Крок 2. Налаштування файлу конфігурації (.env)](#крок-2-налаштування-файлу-конфігурації-env)
3. [Крок 3. Запуск на локальному комп'ютері (Windows)](#крок-3-запуск-на-локальному-компютері-windows)
4. [Крок 4. Налаштування на Linux сервері (Ubuntu/Debian VPS 24/7)](#крок-4-налаштування-на-linux-сервері-ubuntudebian-vps-247)
5. [Крок 5. Перевірка та використання бота](#крок-5-перевірка-та-використання-бота)
6. [Усунення несправностей (Troubleshooting)](#усунення-несправностей-troubleshooting)

---

## 🤖 Крок 1. Створення бота та налаштування Inline Mode

Для роботи бота потрібен токен та увімкнений Inline-режим:

1. Відкрийте Telegram і знайдіть офіційного бота [@BotFather](https://t.me/BotFather).
2. Надішліть команду `/newbot`.
3. Введіть назву бота (наприклад: `My Music Bot`) та його юзернейм (має закінчуватися на `bot`, наприклад: `MyCoolMusic_bot`).
4. Скопіюйте отриманий **API Token** (виглядає як `1234567890:ABCdefGHIjklMNOpqrsTUVwxyZ`).
5. **Обов'язково увімкніть Inline Mode**:
   - Надішліть команду `/setinline`
   - Оберіть вашого бота зі списку
   - Введіть підказку: `Шукати пісню...`
6. **Увімкніть Inline Feedback**:
   - Надішліть команду `/setinlinefeedback`
   - Оберіть вашого бота
   - Оберіть **Enabled (100%)**

---

## 🔑 Крок 2. Налаштування файлу конфігурації (.env)

1. У папці з ботом знайдіть файл `.env.example`.
2. Створіть поруч файл з назвою `.env` (без додаткових розширень).
3. Відкрийте файл `.env` у текстовому редакторі та вставте свій токен:

```env
BOT_TOKEN=1234567890:ABCdefGHIjklMNOpqrsTUVwxyZ
```

---

## 💻 Крок 3. Запуск на локальному комп'ютері (Windows)

### 1. Перевірка Python та FFmpeg
* Переконайтеся, що встановили Python 3.10 або новіше.
* Для конвертації аудіо у системі має бути встановлений **FFmpeg** (доданий до `PATH`).

### 2. Встановлення залежностей
Відкрийте командний рядок (CMD) або PowerShell у папці бота і виконайте:

```bash
pip install -r requirements.txt
```

### 3. Запуск бота
```bash
python bot.py
```

Якщо все налаштовано правильно, у консолі з'явиться повідомлення:
`Starting Music Bot... Bot is running! Press Ctrl+C to stop.`

---

## 🌐 Крок 4. Налаштування на Linux сервері (Ubuntu/Debian VPS 24/7)

Щоб бот працював цілодобово без вашої участі:

### 1. Встановлення системних пакунків
Підключіться до VPS через SSH та виконайте:

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install python3 python3-pip python3-venv ffmpeg git -y
```

### 2. Розміщення проєкту на сервері
Створіть директорію проєкту та скопіюйте сюди файли:

```bash
sudo mkdir -p /opt/tgbot
cd /opt/tgbot
# Скопіюйте файли проєкту в цю папку
```

### 3. Створення віртуального середовища Python
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 4. Створення файлу `.env` на сервері
```bash
nano .env
```
Вставте токен вашого бота:
```env
BOT_TOKEN=ваш_токен_від_botfather
```
Збережіть файл: натисніть `Ctrl + O` → `Enter` → `Ctrl + X`.

### 5. Налаштування цілодобового автозапуску (Systemd Service)
Створіть файл служби:

```bash
sudo nano /etc/systemd/system/tgbot.service
```

Вставте наступний текст конфігурації:

```ini
[Unit]
Description=Telegram Inline Music Bot
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/tgbot
ExecStart=/opt/tgbot/venv/bin/python bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Збережіть файл (`Ctrl + O`, `Enter`, `Ctrl + X`).

Запустіть та активуйте сервіс:

```bash
# Оновити конфігурацію служб
sudo systemctl daemon-reload

# Додати в автозапуск при завантаженні VPS
sudo systemctl enable tgbot

# Запустити бота зараз
sudo systemctl start tgbot
```

### Корисні команди для керування сервером:
* **Перевірити статус бота:** `sudo systemctl status tgbot`
* **Переглянути логи в реальному часі:** `sudo journalctl -u tgbot -f`
* **Перезапустити бота:** `sudo systemctl restart tgbot`
* **Зупинити бота:** `sudo systemctl stop tgbot`

---

## 🎧 Крок 5. Перевірка та використання бота

1. Відкрийте Telegram (будь-який чат чи групу).
2. Введіть `@юзернейм_вашого_бота назва пісні` (наприклад, `@MyCoolMusic_bot Believer`).
3. Зачекайте 1-2 секунди, поки Deezer знайде трек.
4. Оберіть потрібну пісню зі списку.
5. У надісланій картці натисніть кнопку **«🎵 Завантажити MP3»**.
6. Бот завантажить трек у системну папку Temp, відправить вам MP3 файл і одразу видалить файл із сервера.
7. При повторному запиті тієї ж пісні файл відправиться **миттєво з кешу**.

---

## ❓ Усунення несправностей (Troubleshooting)

| Проблема | Причина | Рішення |
|---|---|---|
| Бот не реагує на `@botname` у чаті | Не ввімкнено Inline Mode у BotFather | Надішліть `/setinline` у BotFather і увімкніть його |
| Помилка `BOT_TOKEN не знайдено` | Відсутній файл `.env` | Переконайтеся, що файл називається саме `.env` і містить `BOT_TOKEN=...` |
| Помилка `ffmpeg not found` | Не встановлено FFmpeg | Встановіть FFmpeg через `apt install ffmpeg` (Linux) або додайте в PATH (Windows) |
