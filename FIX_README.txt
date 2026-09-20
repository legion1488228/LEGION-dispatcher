ИСПРАВЛЕННАЯ ВЕРСИЯ LEGION MINI APP

Эта версия не использует папку static. HTML/CSS/JS встроены прямо в app.py.
Поэтому Railway больше не должен падать с ошибкой:
Directory /app/static does not exist

Что сделать:
1. В GitHub репозитории LEGION-dispatcher замените app.py на этот app.py.
2. Папка static больше не нужна.
3. Дождитесь нового Deploy в Railway.
4. Порт остается 8000.
