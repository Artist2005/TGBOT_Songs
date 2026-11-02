# 🎵 Telegram Inline Music Bot

Телеграм бот для пошуку та завантаження повних MP3 файлів через Inline-режим (`@bot_name назва пісні`).

---

## ⚡ Обличчя проєкту та особливості

1. **Без cookies та без авторизації у Google / Chrome**
2. **Повний завантажений MP3 файл** (не 30-секундна демо-версія)
3. **Робота через системну папку Temp**: файл завантажується в тимчасову папку `temp/` та **автоматично видаляється** одразу після відправки користувачу.
4. **Кешування `file_id` (SQLite)**: якщо пісню завантажили хоч один раз, повторно вона відправляється миттєво з серверів Telegram без додаткового завантаження.

---

## 🚀 Крок 1. Налаштування Telegram Бота у BotFather

1. Відкрийте Telegram і знайдіть [@BotFather](https://t.me/BotFather).
2. Надішліть команду `/newbot`, придумайте назву та юзернейм для бота (наприклад, `MyMusicDownloader_bot`).
3. Скопіюйте отриманий **API TOKEN**.
4. **ВАЖЛИВО!** Налаштуйте Inline-режим:
   - Надішліть `/setinline` → оберіть вашого бота → введіть підказку (наприклад: `Шукати пісню...`).
   - Надішліть `/setinlinefeedback` → оберіть вашого бота → поставте **Enabled (100%)**.

---

## 💻 Крок 2. Локальний запуск (Windows / macOS)

### 1. Встановлення FFmpeg
* Завантажте [FFmpeg](https://ffmpeg.org/download.html).
* Переконайтеся, що `ffmpeg` додано до змінних середовища (`PATH`).

### 2. Встановлення залежностей
Відкрийте термінал у папці з ботом:
```bash
pip install -r requirements.txt
```

### 3. Створення файлу `.env`
Створіть файл `.env` у кореневій папці проєкту:
```env
BOT_TOKEN=ваш_токен_від_botfather
```

### 4. Запуск бота
```bash
python bot.py
```

---

## ☁️ Крок 3. Налаштування на Linux сервері (Ubuntu / Debian VPS)

Для того, щоб бот працював **24/7 без перерв**:

### 1. Оновлення системи та встановлення FFmpeg і Python
```bash
sudo apt update && sudo apt upgrade -y
sudo apt install python3 python3-pip python3-venv ffmpeg git -y
```

### 2. Клонування або завантаження коду
Створіть папку для бота і перейдіть у неї:
```bash
mkdir -p /opt/tgbot
cd /opt/tgbot
# Перенесіть сюди файли бота або завантажте з Git
```

### 3. Створення віртуального середовища Python
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 4. Створення конфігураційного файлу `.env`
```bash
nano .env
```
Вставте свій токен:
```env
BOT_TOKEN=ваш_токен_від_botfather
```
Збережіть файл (`Ctrl + O`, `Enter`, `Ctrl + X`).

---

## 🔄 Крок 4. Автозапуск 24/7 через systemd (Сервіс Linux)

Створіть файл служби systemd, щоб бот автоматично перезапускався при збоях або перезавантаженні сервера:

```bash
sudo nano /etc/systemd/system/tgbot.service
```

Вставте наступний вміст (замініть `username` на ваше ім'я користувача на сервері або `root`):

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

Збережіть файл. Тепер активуйте і запустіть сервіс:

```bash
# Перезавантажити конфігурацію systemd
sudo systemctl daemon-reload

# Увімкнути автозапуск при завантаженні сервера
sudo systemctl enable tgbot

# Запустити бота
sudo systemctl start tgbot

# Перевірити статус бота
sudo systemctl status tgbot
```

### Корисні команди керування на сервері:
* **Переглянути логи роботи бота в реальному часі:**
  ```bash
  sudo journalctl -u tgbot -f
  ```
* **Перезапустити бота:**
  ```bash
  sudo systemctl restart tgbot
  ```
* **Зупинити бота:**
  ```bash
  sudo systemctl stop tgbot
  ```

---

## 🎯 Як користуватися в Telegram

1. Відкрийте будь-який чат або групу.
2. Введіть: `@назва_вашого_бота назва пісні` (наприклад, `@MyMusic_bot Imagine Dragons`).
3. Оберіть потрібний трек зі списку.
4. Натисніть кнопку **«🎵 Завантажити MP3»**.
5. Бот завантажить повний MP3 у системний Temp, відправить його у чат і видалить тимчасовий файл з диска!
