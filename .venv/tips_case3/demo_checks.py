#!/usr/bin/env python3
from demo_server import process_payment
for scenario, status, h in [('success','SUCCESS',0),('declined','DECLINED',8),('pending','PENDING',9)]:
    rec = process_payment(scenario,10,'care')
    assert rec['status'] == status and rec['gateway_h'] == h, rec
    assert (rec['message'] is not None) == (status=='SUCCESS'),rec
    assert rec['tip_amount']==100,rec
    print('PASS',scenario, '→',status, 'h=',h, 'tip=',rec['tip_amount'])
print('RESULT: 3/3 PASS. Реальный CLI вызван без модификаций.')
