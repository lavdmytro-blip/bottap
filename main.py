import os, json, time, hmac, hashlib, secrets, asyncio, logging, threading
from pathlib import Path
from urllib.parse import parse_qsl
from contextlib import asynccontextmanager
import httpx
import psycopg
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from psycopg.types.json import Jsonb

logging.basicConfig(level=logging.INFO)
logging.getLogger('httpx').setLevel(logging.WARNING)  # avoid logging Bot API URLs containing secrets
log = logging.getLogger('popov')
TOKEN = os.getenv('BOT_TOKEN', '').strip()
DATABASE_URL = os.getenv('DATABASE_URL', '')
PUBLIC_URL = os.getenv('PUBLIC_URL', '').strip().rstrip('/')
BOT_USERNAME = os.getenv('BOT_USERNAME', 'popov_tap_game_bot').strip().lstrip('@')
LOCK = threading.RLock()
SESSIONS = {}
RECENTS = {}
PRICE = {'tap':75,'energy':140,'regen':230,'auto':500}
SKIN_COST = {'ember':2500,'frost':8500,'royal':25000}
STARS = {'neon':25,'pink':40}
BONUSES = {'ember': {'tap':1}, 'frost': {'regen':1},'royal':{'tap':2,'passive':120}}
REWARDS = {'100':500,'500':3000,'2000':15000}
state = {'users':{},'charges':{}}
conn = None

def db_conn():
    global conn
    if not DATABASE_URL:
        raise RuntimeError('DATABASE_URL missing. Add PostgreSQL addon and a secret variable.')
    if conn is None or conn.closed:
        conn = psycopg.connect(DATABASE_URL, autocommit=True, connect_timeout=10)
    return conn

def initialize():
    global state
    c=db_conn()
    c.execute('CREATE TABLE IF NOT EXISTS game_state (id INTEGER PRIMARY KEY, state JSONB NOT NULL)')
    c.execute('INSERT INTO game_state (id,state) VALUES (1,%s) ON CONFLICT (id) DO NOTHING',[Jsonb(state)])
    row=c.execute('SELECT state FROM game_state WHERE id=1').fetchone()
    state=row[0] if row else state
    state.setdefault('users',{});state.setdefault('charges',{})

def persist():
    db_conn().execute('UPDATE game_state SET state=%s WHERE id=1',[Jsonb(state)])

def initial():
    return {'balance':0,'totalTaps':0,'energy':1000,'last':time.time()*1000,'upgrades':{'tap':0,'energy':0,'regen':0,'auto':0},'skin':'classic','skins':['classic'],'claimed':[]}

def bonus(p,k): return BONUSES.get(p['skin'],{}).get(k,0)
def max_energy(p): return 1000 + 300*p['upgrades']['energy']
def recharge(p): return 1+.75*p['upgrades']['regen']+bonus(p,'regen')
def passive(p): return 120*p['upgrades']['auto']+bonus(p,'passive')
def tick(p):
    now=time.time()*1000
    dt=min(86400,max(0,(now-p.get('last',now))/1000))
    p['energy']=min(max_energy(p),p['energy']+dt*recharge(p))
    p['balance']+=dt*passive(p)/3600
    p['last']=now

def telegram_user(init_data):
    if not TOKEN: raise ValueError('BOT_TOKEN not set')
    try:
        pairs=dict(parse_qsl(init_data,keep_blank_values=True))
        expected=pairs.pop('hash','')
        # Include 'signature' in bot-token HMAC validation; only 'hash' is excluded.
        auth=int(pairs.get('auth_date','0'))
        if not expected or abs(time.time()-auth)>86400: raise ValueError('Telegram authorization expired')
        check='\n'.join(f'{key}={value}' for key,value in sorted(pairs.items()))
        secret=hmac.new(b'WebAppData',TOKEN.encode(),hashlib.sha256).digest()
        signature=hmac.new(secret,check.encode(),hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected,signature): raise ValueError('Invalid Telegram signature')
        user=json.loads(pairs.get('user','null'))
        if not isinstance(user,dict) or not user.get('id'): raise ValueError('Open inside Telegram')
        return user
    except (KeyError,TypeError,ValueError,json.JSONDecodeError) as e:
        raise ValueError(f'Telegram authentication failed: {e}')

def get_player(req):
    session=req.headers.get('x-popov-session','')
    info=SESSIONS.get(session)
    if not info or info['expires']<time.time(): raise HTTPException(401,'Open the game through Telegram')
    uid=info['id']
    player=state['users'].get(uid)
    if player is None: raise HTTPException(401,'Session expired')
    tick(player)
    return uid,player

def fail(msg,status=400): raise HTTPException(status,msg)

async def telegram(method, payload):
    async with httpx.AsyncClient(timeout=38) as client:
        response=await client.post(f'https://api.telegram.org/bot{TOKEN}/{method}',json=payload)
        data=response.json()
        if not data.get('ok'): raise ValueError(data.get('description','Telegram API error'))
        return data['result']

async def polling():
    offset=0
    while True:
        try:
            updates=await telegram('getUpdates',{'offset':offset,'timeout':25,'allowed_updates':['message','pre_checkout_query']})
            for update in updates:
                offset=update['update_id']+1
                if 'pre_checkout_query' in update:
                    q=update['pre_checkout_query']
                    try:
                        item=json.loads(q['invoice_payload'])
                        ok=(str(q['from']['id'])==item['id'] and q['currency']=='XTR' and STARS.get(item['skin'])==q['total_amount'] and item['id'] in state['users'])
                    except (ValueError,KeyError,TypeError): ok=False
                    await telegram('answerPreCheckoutQuery',{'pre_checkout_query_id':q['id'],'ok':ok,**({} if ok else {'error_message':'Invalid invoice'})})
                msg=update.get('message',{})
                if msg.get('text','').startswith('/start'):
                    parts=msg.get('text','').split(maxsplit=1)
                    ref=parts[1][4:] if len(parts)>1 and parts[1].startswith('ref_') else ''
                    launch_url=PUBLIC_URL+('?ref='+ref if ref.isdigit() and len(ref)<30 else '')
                    if PUBLIC_URL:
                        await telegram('sendMessage',{'chat_id':msg['chat']['id'],'text':'👑 POPOV TAP — играй и собирай POPOV POINTS!','reply_markup':{'inline_keyboard':[[{'text':'🎮 Играть','web_app':{'url':launch_url}}]]}})
                    else:
                        await telegram('sendMessage',{'chat_id':msg['chat']['id'],'text':'👑 POPOV TAP: открой игру через кнопку меню.'})
                pay=msg.get('successful_payment')
                if pay:
                    try:
                        item=json.loads(pay['invoice_payload']);uid=item['id'];skin=item['skin'];charge=pay['telegram_payment_charge_id']
                        if (str(msg['from']['id'])==uid and uid in state['users'] and skin in STARS and STARS[skin]==pay['total_amount'] and pay['currency']=='XTR'):
                            with LOCK:
                                if charge not in state['charges']:
                                    state['charges'][charge]={'id':uid,'skin':skin,'at':time.time()}
                                    if skin not in state['users'][uid]['skins']: state['users'][uid]['skins'].append(skin)
                                    persist()
                            await telegram('sendMessage',{'chat_id':uid,'text':'✅ Скин получен! Открой POPOV TAP → Скины.'})
                    except Exception: log.exception('Payment handling failed')
        except asyncio.CancelledError: raise
        except Exception as e:
            log.warning('Telegram polling: %s',str(e)[:180]);await asyncio.sleep(5)

@asynccontextmanager
async def lifespan(app):
    initialize()
    log.info('Database connected; users: %d',len(state['users']))
    task=asyncio.create_task(polling()) if TOKEN else None
    yield
    if task: task.cancel()
    if conn: conn.close()

app=FastAPI(lifespan=lifespan)

@app.get('/')
async def home(): return FileResponse(Path(__file__).parent/'public'/'index.html',headers={'Cache-Control':'no-store, max-age=0'})
@app.get('/coin.png')
async def coin(): return FileResponse(Path(__file__).parent/'public'/'coin.png')
@app.get('/version')
async def version(): return {'build':'POPOV_FIX_V2', 'status':'online'}

@app.get('/health')
async def health(): return {'ok':True,'database':'connected','users':len(state['users'])}

@app.exception_handler(HTTPException)
async def http_error(_,exc): return JSONResponse({'error':str(exc.detail)},status_code=exc.status_code)

@app.post('/api/{action}')
async def api(action:str,request:Request):
    try: data=await request.json()
    except Exception: fail('Invalid JSON')
    if not isinstance(data,dict): fail('Invalid data')
    if action=='auth':
        try: user=telegram_user(str(data.get('initData','')))
        except ValueError as e: fail(str(e),401)
        uid=str(user['id'])
        with LOCK:
            newcomer=uid not in state['users']
            player=state['users'].setdefault(uid,initial())
            if newcomer:
                ref=str(data.get('ref',''))
                if ref.isdigit() and ref!=uid and ref in state['users']:
                    player['referred_by']=ref
                    state['users'][ref]['ref_count']=state['users'][ref].get('ref_count',0)+1
            player.setdefault('ref_count',0)
            name=' '.join(str(user.get(k,'')).strip() for k in ('first_name','last_name')).strip()[:70]
            photo=str(user.get('photo_url',''))
            player['profile']={'username':str(user.get('username',''))[:35], 'name':name or 'Игрок POPOV', 'photo_url':photo[:1000] if photo.startswith('https://') else ''}
            tick(player);persist()
            token=secrets.token_hex(32);SESSIONS[token]={'id':uid,'expires':time.time()+86400}
            return {'session':token,'player':player,'profile':user,'ref_count':player.get('ref_count',0)}
    with LOCK:
        uid,p=get_player(request)
        if action=='tap':
            count=data.get('count')
            if type(count)!=int or count<1 or count>40: fail('Invalid tap count')
            now=time.time();recent=RECENTS.get(uid,{'at':now,'count':0})
            if now-recent['at']>1: recent={'at':now,'count':0}
            recent['count']+=count;RECENTS[uid]=recent
            if recent['count']>20: fail('Too many taps')
            done=min(count,int(p['energy']))
            p['energy']-=done;p['totalTaps']+=done;p['balance']+=done*(1+p['upgrades']['tap']+bonus(p,'tap'))
        elif action=='upgrade':
            key=data.get('id')
            if key not in PRICE: fail('Unknown upgrade')
            lvl=p['upgrades'][key]
            if lvl>=100: fail('Max level')
            cost=round(PRICE[key]*(1.65**lvl))
            if p['balance']<cost: fail('Not enough points')
            p['balance']-=cost;p['upgrades'][key]+=1
        elif action=='skin-buy':
            key=data.get('id')
            if key not in SKIN_COST: fail('Unknown skin')
            if key in p['skins']: fail('Skin already owned')
            if p['balance']<SKIN_COST[key]: fail('Not enough points')
            p['balance']-=SKIN_COST[key];p['skins'].append(key);p['skin']=key
        elif action=='equip':
            if data.get('id') not in p['skins']: fail('Skin not owned')
            p['skin']=data['id']
        elif action=='claim':
            key=str(data.get('id',''))
            if key not in REWARDS or key in p['claimed'] or p['totalTaps']<int(key): fail('Reward unavailable')
            p['balance']+=REWARDS[key];p['claimed'].append(key)
        elif action=='leaderboard':
            ranked=sorted(({'uid':id,'score':int(max(0,x['balance'])),'username':x.get('profile',{}).get('username',''),'name':x.get('profile',{}).get('name','Игрок POPOV'),'photo_url':x.get('profile',{}).get('photo_url','')} for id,x in state['users'].items()),key=lambda x:(-x['score'],x['uid']))
            my_rank=next((i+1 for i,x in enumerate(ranked) if x['uid']==uid),0)
            top=[{'rank':i+1,'score':x['score'],'username':x['username'],'name':x['name'],'photo_url':x['photo_url'],'isMe':x['uid']==uid} for i,x in enumerate(ranked[:100])]
            persist();return {'top':top,'myRank':my_rank,'myScore':int(max(0,p['balance'])),'total':len(ranked)}
        elif action=='state': pass
        elif action=='invoice':
            skin=data.get('id')
            if skin not in STARS: fail('Unknown item')
            if skin in p['skins']: fail('Already owned')
            payload=json.dumps({'id':uid,'skin':skin,'nonce':secrets.token_hex(8)})
            try:
                url=await telegram('createInvoiceLink',{'title':'POPOV TAP — '+skin,'description':'Косметический скин POPOV TAP','payload':payload,'currency':'XTR','prices':[{'label':'Cosmetic skin','amount':STARS[skin]}]})
            except Exception as e: fail('Telegram invoice error: '+str(e),502)
            return {'url':url}
        else: fail('Not found',404)
        persist();return {'player':p}
