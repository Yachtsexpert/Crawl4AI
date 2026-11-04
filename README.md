# Range Marine / OK-Boats — Apify + Python market monitor

Пилот автоматического мониторинга пяти открытых источников яхтенных новостей с помощью Python, Apify Website Content Crawler и GitHub Actions.

Источники: Yamarin, Axopar, FJORD, BENETEAU и PressMare. Обработка ограничена двумя страницами на источник. Результаты: URL, заголовки, SHA-256 содержимого, время проверки, события изменения и отчёты об ошибках. Полные тексты и фотографии не сохраняются. Цены, наличие, комплектация и VAT **не проверяются**.

## Настройка

1. Получите токен Apify в https://console.apify.com/.
2. Откройте этот репозиторий: Settings → Secrets and variables → Actions → New repository secret.
3. Создайте секрет `APIFY_TOKEN` (не вставляйте его в код или чат).
4. Запустите Actions → OK-Boats yacht market source monitor → Run workflow.
5. Проверьте журнал Actions и файлы в `data/`.

Расписание: каждый день в 07:23 UTC (10:23 по Стамбулу), возможны задержки GitHub Actions. В первый успешный запуск формируется базовый снимок, в следующие — события `CONTENT_CHANGED`.

Код выполняет Actor `apify/website-content-crawler`. Лимиты на источник: максимум 2 страницы и `max_total_charge_usd=0.10` для модели pay-per-event. **Это не гарантирует общий потолок расходов на вычисления/прокси**; проверьте цены и настройте бюджет/оповещения в Apify до запуска.

## Проверка локально

```bash
python -m pip install -r requirements.txt
python -m monitor.main --check-config
python -m unittest discover -s tests -v
python -m monitor.main  # требует APIFY_TOKEN в окружении
```

Модуль не изменяет OK-Boats.com, CRM и рабочий репозиторий `rangemarine`. Статус сбора не следует считать подтверждённым до успешного live-запуска в GitHub Actions.
