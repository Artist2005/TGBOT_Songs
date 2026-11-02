@echo off
chcp 65001 >nul
cd /d "%~dp0"

set MSG=%~1
if "%MSG%"=="" set MSG=Update from PC

echo [1/3] Додавання файлів у Git...
git add .

echo [2/3] Створення коміту: "%MSG%"...
git commit -m "%MSG%"

echo [3/3] Відправка змін у GitHub (origin/main)...
git push origin main

echo.
echo ✅ Готово! Зміни відправлено в GitHub.
echo Віртуалка автоматично підтягне оновлення і перезапустить бота протягом 1 хвилини.
pause
