#!/usr/bin/env python3
"""Local-only browser UI over unchanged 04_prototip.py. Standard library only."""
import csv
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
SOURCE = HERE / '04_прототип.py'
HTML = (HERE / 'index.html').read_bytes()
SECRET = b'demo-secret-k3'  # matches unmodified educational CLI
MESSAGES = {
    'care': 'Спасибо за заботу и внимание!',
    'service': 'Спасибо за отличный сервис!',
    'evening': 'Вы сделали наш вечер приятнее!',
}


def employee_name(employee_id):
    """Opt-in genuine employees.csv; otherwise explicitly named demo record."""
    source = os.environ.get('TIP_EMPLOYEES_CSV')
    if source and Path(source).is_file():
        with open(source, encoding='utf-8-sig', newline='') as file:
            for item in csv.DictReader(file):
                if item.get('employee_id') != employee_id:
                    continue
                for field in ('full_name', 'employee_name', 'name', 'fio'):
                    if item.get(field):
                        return item[field].strip()[:100], False
                return 'Сотрудник ' + employee_id, False
    return 'Алексей (демо)', True


def find_nonce(qr_id, target):
    for i in range(10000):
        nonce = f'{secrets.token_hex(4)}{i:04d}'
        h = int(hashlib.md5((qr_id + nonce).encode()).hexdigest(), 16) % 10
        if h == target:
            return nonce
    raise RuntimeError('Unable to generate target gateway outcome')


def process_payment(scenario, tip_pct, message):
    if scenario not in ('success', 'declined', 'pending'):
        raise ValueError('Неизвестный сценарий')
    try:
        pct = float(tip_pct)
    except (ValueError, TypeError):
        raise ValueError('Некорректный процент')
    if not 0 <= pct <= 50 or pct != pct:
        raise ValueError('Процент должен быть от 0 до 50')
    if message not in MESSAGES:
        raise ValueError('Неизвестное сообщение')
    outcome_h = {'success': 0, 'declined': 8, 'pending': 9}[scenario]
    with tempfile.TemporaryDirectory(prefix='tips_ui_') as temp:
        data = Path(temp) / 'data'; data.mkdir()
        output = Path(temp) / 'out'; output.mkdir()
        qr_id = 'demo_qr_' + secrets.token_hex(6)
        nonce = find_nonce(qr_id, outcome_h)
        now = datetime.now(timezone.utc)
        exp = (now + timedelta(hours=24)).strftime('%Y-%m-%dT%H:%M:%SZ')
        payload = f'm_demo|e_001|receipt_demo|{nonce}|{exp}'
        signature = hmac.new(SECRET, payload.encode(), hashlib.sha256).hexdigest()[:16]
        with (data / 'qr_links.csv').open('w', encoding='utf-8', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=['qr_id','merchant_id','employee_id','receipt_id','nonce','exp','signature'])
            writer.writeheader()
            writer.writerow(dict(qr_id=qr_id, merchant_id='m_demo', employee_id='e_001', receipt_id='receipt_demo', nonce=nonce, exp=exp, signature=signature))
        with (data / 'merchants.csv').open('w', encoding='utf-8', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=['merchant_id', 'status'])
            writer.writeheader()
            writer.writerow(dict(merchant_id='m_demo',status='ACTIVE'))
        request = dict(qr_id=qr_id, receipt_id='receipt_demo', base_amount=1000,tip_pct=pct,
                       idempotency_key='ui-' + secrets.token_hex(8),now=now.strftime('%Y-%m-%dT%H:%M:%SZ'))
        req = output / 'request.json'
        req.write_text(json.dumps(request, ensure_ascii=False), encoding='utf-8')
        env = os.environ.copy()
        env['TIP_DATA_DIR'] = str(data)
        env['TIP_OUT_DIR'] = str(output)
        env.pop('TIP_NOW',None)
        p = subprocess.run([sys.executable, str(SOURCE), 'auto', str(req)], env=env,
                           capture_output=True, text=True, timeout=15)
        if p.returncode not in (0,1):
            raise RuntimeError(f'Ошибка запуска прототипа: {p.stdout[-500:]} {p.stderr[-250:]}')
        result = json.loads(p.stdout)
        if result['status'] not in ('SUCCESS', 'DECLINED', 'PENDING'):
            raise RuntimeError('Некорректный результат прототипа')
        name, is_demo = employee_name('e_001')
        return dict(status=result['status'], payment_id=result['payment_id'],
                    amount_total=result['amount_total'], tip_amount=result['tip_amount'],
                    error_code=result['error_code'],gateway_h=result['gateway_h'],
                    employee=name, demo_employee=is_demo, message=MESSAGES[message] if result['status']=='SUCCESS' else None,
                    simulated=True)


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, obj):
        body = json.dumps(obj,ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Content-Length',str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        if urlparse(self.path).path != '/':
            return self.reply(404, {'error':'Not found'})
        self.send_response(200)
        self.send_header('Content-Type','text/html; charset=utf-8')
        self.send_header('Cache-Control','no-store')
        self.send_header('Content-Length',str(len(HTML)))
        self.end_headers(); self.wfile.write(HTML)

    def do_POST(self):
        if urlparse(self.path).path != '/api/pay':
            return self.reply(404,{'error':'Not found'})
        try:
            length = int(self.headers.get('Content-Length','0'))
            if not 0 < length <= 4096:
                return self.reply(413, {'error':'Неверный размер запроса'})
            req = json.loads(self.rfile.read(length).decode('utf-8'))
            if not isinstance(req,dict): raise ValueError('Ожидается объект JSON')
            result = process_payment(req.get('scenario'),req.get('tip_pct'),req.get('message'))
            self.reply(200,result)
        except (ValueError, json.JSONDecodeError) as ex:
            self.reply(400,{'error':str(ex)})
        except Exception as ex:
            self.reply(500,{'error':str(ex)})


def main():
    if not SOURCE.is_file():
        raise SystemExit('Рядом с demo_server.py нужен оригинальный 04_prototip.py')
    port = int(os.environ.get('TIP_DEMO_PORT','8765'))
    server = ThreadingHTTPServer(('127.0.0.1',port),Handler)
    print(f'Демо доступно только на этом компьютере: http://127.0.0.1:{port}/', flush=True)
    print('Ctrl+C для остановки. Реальные платежи не выполняются.',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt: pass
    finally:server.server_close()

if __name__=='__main__':main()
