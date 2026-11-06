# Механизм эмуляции платежа — кейс 3 (заполнено)

## 1. Состояния
- CREATED — платёж создан, проверки QR и сумм пройдены, шлюз ещё не вызывался.
- PENDING — отправлен в шлюз, результата нет (в работе или таймаут).
- SUCCESS — оплата прошла; nonce погашен.
- DECLINED — шлюз отказал (CARD_DECLINED).
- CANCELED — клиент отменил до подтверждения.

## 2. Матрица переходов

| Из | В | Условие |
|---|---|---|
| CREATED | PENDING | команда confirm; QR валиден, nonce не погашен |
| PENDING | SUCCESS | эмулятор вернул h = 0…7 |
| PENDING | DECLINED | эмулятор вернул h = 8 |
| PENDING | PENDING | эмулятор вернул h = 9 (GATEWAY_TIMEOUT) |
| CREATED | CANCELED | cancel до confirm |
| PENDING | CANCELED | запрещено → CANCEL_NOT_ALLOWED (409) |
| SUCCESS | CANCELED | запрещено → CANCEL_NOT_ALLOWED (409) |

## 3. Шаги обработки

| № | Шаг | Что проверяется | При ошибке |
|---|---|---|---|
| 1 | Валидация запроса | поля; base_amount в (0; 1 000 000]; tip_pct в [0; 50]; повтор idempotency_key (проверяется первым) | AMOUNT_OUT_OF_RANGE / TIP_OUT_OF_RANGE; при повторе — прежний результат, HTTP 200 |
| 2 | Проверка QR | qr_id найден, подпись, exp ≥ now, merchant ACTIVE, receipt_id, nonce не использован | INVALID_QR / QR_EXPIRED / MERCHANT_BLOCKED / RECEIPT_MISMATCH / QR_USED; платёж не создаётся |
| 3 | Расчёт суммы | tip, amount_total; amount_total ≤ 2 000 000 | AMOUNT_OUT_OF_RANGE |
| 4 | Создание | запись платежа со статусом CREATED | — |
| 5 | Подтверждение | CREATED → PENDING, вызов эмулятора | платёж не в CREATED/PENDING — отказ |
| 6 | Результат | SUCCESS / DECLINED / PENDING; при SUCCESS nonce гасится | DECLINED → CARD_DECLINED; PENDING → GATEWAY_TIMEOUT |
| 7 | Отмена | статус только CREATED | CANCEL_NOT_ALLOWED (409) |
| 8 | Аудит | событие на каждый переход статуса | — |

## 4. Формулы
- tip_amount = round(base_amount × tip_pct / 100, 2)
- amount_total = round(base_amount + tip_amount, 2)
- h = int(md5(qr_id + nonce).hexdigest(), 16) % 10
- SIGNATURE = HMAC-SHA256(SECRET, "merchant_id|employee_id|receipt_id|nonce|exp").hexdigest()[:16]

## 5. Пример qr_001
- base_amount = 637.00; tip_pct = 5; tip_amount = 31.85; amount_total = 668.85
- h = 9 → статус PENDING (GATEWAY_TIMEOUT). **Подтверждено запуском** (gateway_h = 9, amount_total = 668.85, tip_amount = 31.85).
- **Расхождение:** T3-1 и payments.csv (p_0001) дают SUCCESS. Суммы сходятся, статус нет — вопрос Архитектору.

## 6. Пример qr_003
- base_amount = 911.00; tip_pct = 15; tip_amount = 136.65; amount_total = 1047.65
- h = 7 → SUCCESS. **Подтверждено запуском** (status = SUCCESS, amount_total = 1047.65, tip_amount = 136.65).
- **Расхождение:** T3-2 ждёт DECLINED. При этом payments.csv (p_0003) тоже SUCCESS с tip 136.65, то есть данные совпадают с формулой, а T3-2 — нет.

## 7. Обработка ошибок (HTTP, помеченные «?», не подтверждены источниками)

| Ситуация | error_code | HTTP | retryable |
|---|---|---|---|
| QR signature не совпала | INVALID_QR | 422 | нет |
| QR истёк | QR_EXPIRED | 410 | нет |
| nonce использован | QR_USED | 409 (?) | нет |
| merchant BLOCKED | MERCHANT_BLOCKED | 403 (?) | нет |
| tip_pct > 50 | TIP_OUT_OF_RANGE | 400 (?) | нет |
| amount_total > 2 000 000 | AMOUNT_OUT_OF_RANGE | 400 (?) | нет |
| тот же idempotency_key | — (прежний результат) | 200 | — |
| эмулятор вернул 8 | CARD_DECLINED | ? | нет |
| эмулятор вернул 9 | GATEWAY_TIMEOUT | ? | да |
| отмена из PENDING | CANCEL_NOT_ALLOWED | 409 | нет |
| отмена из SUCCESS | CANCEL_NOT_ALLOWED | 409 | нет |

## 8. Идемпотентность
Повтор запроса с тем же `idempotency_key` возвращает сохранённый результат (тот же payment_id), HTTP 200. Новая строка в `payments` не создаётся, nonce не расходуется. Ключ проверяется до проверки nonce. Ожидаемо: одна строка на платёж.

## 9. Аудит
В шаблоне указано 8 полей, в ER-модели описано 7: event_id, payment_id, action, from_status, to_status, actor, at. Восьмое поле в источниках не названо (предложение: error_code). **Вопрос Архитектору.**

## 10. Критерий приёмки
- [x] 5 состояний, матрица, 8 шагов, формулы, таблица ошибок, идемпотентность
- [ ] «Примеры сходятся с payments.csv» — суммы сходятся, статусы qr_001 нет
- [ ] Аудит: 8 полей — в источниках 7
