#!/usr/bin/env python3
"""Independent CLI regression tests using isolated synthetic fixtures; does not alter original CSV or source."""
import csv,hashlib,hmac,json,os,subprocess,sys,tempfile
from pathlib import Path
PROTOTYPE=Path(__file__).with_name('04_прототип.py')
NOW='2025-11-01T18:00:00Z'
EXP='2025-11-02T18:00:00Z'
results=[]

def nonce_for(qr_id,target):
    for i in range(10000):
        n=f'test{i:04d}'
        if int(hashlib.md5((qr_id+n).encode()).hexdigest(),16)%10==target: return n
    raise RuntimeError('nonce not found')

def fixture(td,h=0,signature_ok=True,expired=False,merchant='ACTIVE'):
    p=Path(td); data=p/'data';out=p/'output';data.mkdir();out.mkdir()
    qr='test_qr'; nonce=nonce_for(qr,h); exp='2025-10-31T18:00:00Z' if expired else EXP
    msg=f'm_001|e_001|r_test|{nonce}|{exp}'
    sig=hmac.new(b'demo-secret-k3',msg.encode(),hashlib.sha256).hexdigest()[:16]
    if not signature_ok:sig='0'*16
    with (data/'qr_links.csv').open('w',encoding='utf-8',newline='') as f:
        w=csv.DictWriter(f,['qr_id','merchant_id','employee_id','receipt_id','nonce','exp','signature']);w.writeheader();w.writerow(dict(qr_id=qr,merchant_id='m_001',employee_id='e_001',receipt_id='r_test',nonce=nonce,exp=exp,signature=sig))
    with (data/'merchants.csv').open('w',encoding='utf-8',newline='') as f:
        w=csv.DictWriter(f,['merchant_id','status']);w.writeheader();w.writerow(dict(merchant_id='m_001',status=merchant))
    return data,out,nonce

def invoke(data,out,cmd,arg,key='x',pct=5,base=637):
    env=os.environ.copy();env.update(TIP_DATA_DIR=str(data),TIP_OUT_DIR=str(out),TIP_NOW=NOW)
    if cmd in ('create','auto'):
        path=out/f'req_{key}.json';path.write_text(json.dumps(dict(qr_id='test_qr',receipt_id='r_test',base_amount=base,tip_pct=pct,idempotency_key=key,now=NOW)),encoding='utf-8');arg=str(path)
    cp=subprocess.run([sys.executable,str(PROTOTYPE),cmd,arg],capture_output=True,text=True,env=env)
    try: result=json.loads(cp.stdout)
    except Exception:raise AssertionError(f'No JSON output: {cp.stdout} stderr={cp.stderr}')
    return result,cp.returncode

def run(name,fn):
    try:
        with tempfile.TemporaryDirectory(prefix='tip_case3_') as td: fn(td)
        results.append((name,'PASS',''))
    except Exception as e:results.append((name,'FAIL',str(e)))

def success(td):
    d,o,n=fixture(td,0);r,c=invoke(d,o,'auto',None)
    assert (r['status'],r['gateway_h'],r['amount_total'],r['tip_amount'],c)==('SUCCESS',0,668.85,31.85,0),r
    with (o/'payments_out.csv').open() as f: rows=list(csv.DictReader(f))
    assert len(rows)==1 and rows[0]['status']=='SUCCESS'

def decline(td):
    d,o,n=fixture(td,8);r,c=invoke(d,o,'auto',None)
    assert (r['status'],r['gateway_h'],r['error_code'],c)==('DECLINED',8,'CARD_DECLINED',1),r

def timeout(td):
    d,o,n=fixture(td,9);r,c=invoke(d,o,'auto',None)
    assert (r['status'],r['gateway_h'],r['error_code'],c)==('PENDING',9,'GATEWAY_TIMEOUT',1),r

def invalid_sig(td):
    d,o,n=fixture(td,0,signature_ok=False);r,c=invoke(d,o,'auto',None)
    assert (r['status'],r['error_code'],r['http'],c)==('ERROR','INVALID_QR',422,2),r
    assert not (o/'payments_out.csv').exists()

def expired(td):
    d,o,n=fixture(td,0,expired=True);r,c=invoke(d,o,'auto',None)
    assert (r['error_code'],r['http'],c)==('QR_EXPIRED',410,2),r

def idem(td):
    d,o,n=fixture(td,0);r,c=invoke(d,o,'auto',None,key='same');r2,c2=invoke(d,o,'auto',None,key='same')
    assert r['payment_id']==r2['payment_id'] and r2.get('idempotent_replay') is True and r2['http']==200,(r,r2)
    with (o/'payments_out.csv').open() as f:rows=list(csv.DictReader(f))
    assert len(rows)==1,rows

def cancel(td):
    d,o,n=fixture(td,0);r,c=invoke(d,o,'create',None);r2,c2=invoke(d,o,'cancel',r['payment_id'])
    assert r['status']=='CREATED' and r2['status']=='CANCELED' and c2==0

def cancel_pending(td):
    d,o,n=fixture(td,9);r,c=invoke(d,o,'auto',None);r2,c2=invoke(d,o,'cancel',r['payment_id'])
    assert (r2['error_code'],r2['http'],c2)==('CANCEL_NOT_ALLOWED',409,2),r2

def tip_over(td):
    d,o,n=fixture(td,0);r,c=invoke(d,o,'create',None,pct=51)
    assert (r['error_code'],r['http'],c)==('TIP_OUT_OF_RANGE',400,2),r

def receipt_mismatch(td):
    d,o,n=fixture(td,0);env=os.environ.copy();env.update(TIP_DATA_DIR=str(d),TIP_OUT_DIR=str(o));q=o/'req.json';q.write_text(json.dumps(dict(qr_id='test_qr',receipt_id='WRONG',base_amount=637,tip_pct=5,now=NOW)))
    cp=subprocess.run([sys.executable,str(PROTOTYPE),'auto',str(q)],capture_output=True,text=True,env=env);r=json.loads(cp.stdout)
    assert r['error_code']=='RECEIPT_MISMATCH' and r['http']==422,r

for name,fn in [('S01 SUCCESS h=0 and tip calculation',success),('S02 DECLINED h=8',decline),('S03 PENDING/TIMEOUT h=9',timeout),('S04 INVALID_QR and no payment row',invalid_sig),('S05 QR_EXPIRED',expired),('S06 Idempotent replay one CSV row',idem),('S07 Cancel CREATED',cancel),('S08 Reject cancel PENDING',cancel_pending),('S09 Reject tip >50%',tip_over),('S10 Reject receipt mismatch',receipt_mismatch)]:run(name,fn)
print('Independent synthetic-fixture CLI regression suite')
print('Source:',PROTOTYPE.name)
print('NOTE: Not original T3-1/T3-2; tests use isolated generated CSV fixtures.')
for n,status,err in results:print(f'{status:4} | {n}'+(f' | {err}' if err else ''))
passed=sum(s=='PASS' for _,s,_ in results)
print(f'RESULT: {passed}/{len(results)} passed')
if len(sys.argv)>1:Path(sys.argv[1]).write_text('\n'.join(['Independent synthetic-fixture CLI regression suite','Source: '+PROTOTYPE.name,'NOTE: Not original T3-1/T3-2; tests use isolated generated CSV fixtures.']+[f'{s} | {n}'+(f' | {e}' if e else '') for n,s,e in results]+[f'RESULT: {passed}/{len(results)} passed'])+'\n',encoding='utf-8')
sys.exit(0 if passed==len(results) else 1)
