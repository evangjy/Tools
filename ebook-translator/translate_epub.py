import os, sys, time, re, zipfile, tempfile, hashlib, urllib.parse, hmac, base64, json
from pathlib import Path
import requests
from bs4 import BeautifulSoup, NavigableString

# Verified free-capacity providers (current official docs checked 2026-09-02):
# DeepL 500k/month; Google Cloud Translation 500k/month; Microsoft Translator 2M/month;
# Amazon Translate 2M/month for 12 months; Baidu Translation 2M/month for new users;
# Alibaba Cloud Machine Translation 1M/month.

PROVIDERS = {
    '1': 'DeepL', '2': 'Google Cloud Translation', '3': 'Microsoft Translator',
    '4': 'Amazon Translate', '5': 'Baidu Translate', '6': 'Alibaba Cloud MT'
}


def die(msg):
    print(f'[ERROR] {msg}')
    return 1


def prompt_secret(label):
    try:
        return input(label).strip()
    except (EOFError, KeyboardInterrupt):
        return ''


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'[INFO] Saved credentials to {path.name}')


def load_saved(base, provider):
    p = base / f'{provider.lower().replace(" ", "_")}_credentials.json'
    if p.exists():
        try: return json.loads(p.read_text(encoding='utf-8')), p
        except Exception: pass
    return {}, p


def choose_provider(base):
    print('\nTranslation provider:')
    for k,v in PROVIDERS.items(): print(f'  {k}. {v}')
    choice = input('Select [1-6]: ').strip()
    if choice not in PROVIDERS: raise ValueError('Invalid provider selection.')
    return choice, PROVIDERS[choice]


def credentials_for(choice, name, base):
    c, path = load_saved(base, name)
    # Environment variables override saved credentials.
    if name == 'DeepL':
        key = os.getenv('DEEPL_API_KEY', '').strip() or c.get('api_key','').strip()
        if not key: key = prompt_secret('DeepL API key: ')
        if not key: raise ValueError('No DeepL API key supplied.')
        c = {'api_key': key, 'endpoint': c.get('endpoint','https://api-free.deepl.com')}
    elif name == 'Google Cloud Translation':
        key = os.getenv('GOOGLE_TRANSLATE_API_KEY','').strip() or c.get('api_key','').strip()
        if not key: key = prompt_secret('Google Cloud Translation API key: ')
        if not key: raise ValueError('No Google API key supplied.')
        c={'api_key':key}
    elif name == 'Microsoft Translator':
        key=os.getenv('AZURE_TRANSLATOR_KEY','').strip() or c.get('key','').strip()
        region=os.getenv('AZURE_TRANSLATOR_REGION','').strip() or c.get('region','').strip()
        if not key: key=prompt_secret('Azure Translator key: ')
        if not region: region=prompt_secret('Azure Translator region (e.g. global, southeastasia): ')
        if not key or not region: raise ValueError('Microsoft Translator key/region required.')
        c={'key':key,'region':region}
    elif name == 'Amazon Translate':
        ak=os.getenv('AWS_ACCESS_KEY_ID','').strip() or c.get('access_key_id','').strip()
        sk=os.getenv('AWS_SECRET_ACCESS_KEY','').strip() or c.get('secret_access_key','').strip()
        region=os.getenv('AWS_DEFAULT_REGION','').strip() or c.get('region','').strip() or 'us-east-1'
        if not ak: ak=prompt_secret('AWS Access Key ID: ')
        if not sk: sk=prompt_secret('AWS Secret Access Key: ')
        if not ak or not sk: raise ValueError('AWS credentials required.')
        c={'access_key_id':ak,'secret_access_key':sk,'region':region}
    elif name == 'Baidu Translate':
        appid=os.getenv('BAIDU_TRANSLATE_APPID','').strip() or c.get('appid','').strip()
        secret=os.getenv('BAIDU_TRANSLATE_SECRET','').strip() or c.get('secret','').strip()
        if not appid: appid=prompt_secret('Baidu Translate APP ID: ')
        if not secret: secret=prompt_secret('Baidu Translate Secret Key: ')
        if not appid or not secret: raise ValueError('Baidu APP ID/Secret required.')
        c={'appid':appid,'secret':secret}
    elif name == 'Alibaba Cloud MT':
        ak=os.getenv('ALIBABA_ACCESS_KEY_ID','').strip() or c.get('access_key_id','').strip()
        sk=os.getenv('ALIBABA_ACCESS_KEY_SECRET','').strip() or c.get('access_key_secret','').strip()
        region=os.getenv('ALIBABA_REGION','').strip() or c.get('region','cn-hangzhou').strip()
        if not ak: ak=prompt_secret('Alibaba Cloud AccessKey ID: ')
        if not sk: sk=prompt_secret('Alibaba Cloud AccessKey Secret: ')
        if not ak or not sk: raise ValueError('Alibaba AccessKey ID/Secret required.')
        c={'access_key_id':ak,'access_key_secret':sk,'region':region}
    return c, path


def check_deepl(c):
    endpoint=c.get('endpoint','https://api-free.deepl.com').rstrip('/')
    r=requests.get(endpoint+'/v2/usage',headers={'Authorization':f'DeepL-Auth-Key {c["api_key"]}'},timeout=30)
    if r.status_code>=400: raise RuntimeError(f'HTTP {r.status_code}: {r.text[:500]}')
    d=r.json(); print(f'[INFO] DeepL connected: {d.get("character_count",0):,} / {d.get("character_limit",0):,} characters used')


def translate_deepl(texts,c):
    endpoint=c.get('endpoint','https://api-free.deepl.com').rstrip('/')
    r=requests.post(endpoint+'/v2/translate',headers={'Authorization':f'DeepL-Auth-Key {c["api_key"]}'},data=[('text',x) for x in texts]+[('target_lang','ZH')],timeout=90)
    if r.status_code>=400: raise RuntimeError(f'HTTP {r.status_code}: {r.text[:800]}')
    return [x['text'] for x in r.json()['translations']]


def check_google(c):
    r=requests.post('https://translation.googleapis.com/language/translate/v2',params={'key':c['api_key']},json={'q':'Hello','target':'zh-CN','format':'text'},timeout=30)
    if r.status_code>=400: raise RuntimeError(f'HTTP {r.status_code}: {r.text[:800]}')
    print('[INFO] Google Cloud Translation connected.')


def translate_google(texts,c):
    r=requests.post('https://translation.googleapis.com/language/translate/v2',params={'key':c['api_key']},json={'q':texts,'source':'en','target':'zh-CN','format':'text'},timeout=90)
    if r.status_code>=400: raise RuntimeError(f'HTTP {r.status_code}: {r.text[:800]}')
    return [x['translatedText'] for x in r.json()['data']['translations']]


def check_microsoft(c):
    h={'Ocp-Apim-Subscription-Key':c['key'],'Ocp-Apim-Subscription-Region':c['region'],'Content-Type':'application/json'}
    r=requests.post('https://api.cognitive.microsofttranslator.com/translate',params={'api-version':'3.0','from':'en','to':'zh-Hans'},headers=h,json=[{'Text':'Hello'}],timeout=30)
    if r.status_code>=400: raise RuntimeError(f'HTTP {r.status_code}: {r.text[:800]}')
    print('[INFO] Microsoft Translator connected.')


def translate_microsoft(texts,c):
    h={'Ocp-Apim-Subscription-Key':c['key'],'Ocp-Apim-Subscription-Region':c['region'],'Content-Type':'application/json'}
    r=requests.post('https://api.cognitive.microsofttranslator.com/translate',params={'api-version':'3.0','from':'en','to':'zh-Hans'},headers=h,json=[{'Text':x} for x in texts],timeout=90)
    if r.status_code>=400: raise RuntimeError(f'HTTP {r.status_code}: {r.text[:800]}')
    return [x['translations'][0]['text'] for x in r.json()]


def aws_sign_v4(method, url, region, service, access_key, secret_key, payload, headers_extra=None):
    t=time.gmtime(); amzdate=time.strftime('%Y%m%dT%H%M%SZ',t); datestamp=amzdate[:8]
    parsed=urllib.parse.urlparse(url); host=parsed.netloc
    payload_hash=hashlib.sha256(payload.encode()).hexdigest()
    headers={'content-type':'application/x-amz-json-1.1','host':host,'x-amz-date':amzdate,'x-amz-target':'AWSShineFrontendService_20170701.TranslateText'}
    if headers_extra: headers.update(headers_extra)
    canonical_headers=''.join(f'{k.lower()}:{str(headers[k]).strip()}\n' for k in sorted(headers))
    signed=';'.join(k.lower() for k in sorted(headers))
    canonical=f'{method}\n{parsed.path or "/"}\n{parsed.query}\n{canonical_headers}\n{signed}\n{payload_hash}'
    scope=f'{datestamp}/{region}/{service}/aws4_request'
    string_to_sign=f'AWS4-HMAC-SHA256\n{amzdate}\n{scope}\n{hashlib.sha256(canonical.encode()).hexdigest()}'
    def H(k,m): return hmac.new(k,m.encode(),hashlib.sha256).digest()
    kDate=H(('AWS4'+secret_key).encode(),datestamp); kRegion=H(kDate,region); kService=H(kRegion,service); kSigning=H(kService,'aws4_request')
    sig=hmac.new(kSigning,string_to_sign.encode(),hashlib.sha256).hexdigest()
    headers['Authorization']=f'AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed}, Signature={sig}'
    return headers


def check_amazon(c):
    url=f'https://translate.{c["region"]}.amazonaws.com/'
    payload=json.dumps({'Text':'Hello','SourceLanguageCode':'en','TargetLanguageCode':'zh'})
    h=aws_sign_v4('POST',url,c['region'],'translate',c['access_key_id'],c['secret_access_key'],payload)
    r=requests.post(url,headers=h,data=payload,timeout=30)
    if r.status_code>=400: raise RuntimeError(f'HTTP {r.status_code}: {r.text[:800]}')
    print('[INFO] Amazon Translate connected.')


def translate_amazon(texts,c):
    out=[]; url=f'https://translate.{c["region"]}.amazonaws.com/'
    for x in texts:
        payload=json.dumps({'Text':x,'SourceLanguageCode':'en','TargetLanguageCode':'zh'})
        h=aws_sign_v4('POST',url,c['region'],'translate',c['access_key_id'],c['secret_access_key'],payload)
        r=requests.post(url,headers=h,data=payload,timeout=90)
        if r.status_code>=400: raise RuntimeError(f'HTTP {r.status_code}: {r.text[:800]}')
        out.append(r.json()['TranslatedText'])
    return out


def baidu_token(c):
    r=requests.post('https://aip.baidubce.com/oauth/2.0/token',params={'grant_type':'client_credentials','client_id':c['appid'],'client_secret':c['secret']},timeout=30)
    if r.status_code>=400: raise RuntimeError(f'HTTP {r.status_code}: {r.text[:500]}')
    d=r.json()
    if 'access_token' not in d: raise RuntimeError(str(d))
    return d['access_token']


def check_baidu(c):
    c['_token']=baidu_token(c); print('[INFO] Baidu Translate connected.')


def translate_baidu(texts,c):
    token=c.get('_token') or baidu_token(c); out=[]
    for text in texts:
        salt=str(int(time.time()*1000)); sign=hashlib.md5((c['appid']+text+salt+c['secret']).encode()).hexdigest()
        r=requests.post('https://fanyi-api.baidu.com/api/trans/vip/translate',params={'q':text,'from':'en','to':'zh','appid':c['appid'],'salt':salt,'sign':sign},timeout=90)
        if r.status_code>=400: raise RuntimeError(f'HTTP {r.status_code}: {r.text[:800]}')
        d=r.json()
        if 'trans_result' not in d: raise RuntimeError(str(d))
        out.append('\n'.join(x['dst'] for x in d['trans_result']))
    return out


def percent_encode(v): return urllib.parse.quote(str(v),safe='-_.~')

def aliyun_rpc(c, action, params):
    endpoint='https://mt.cn-hangzhou.aliyuncs.com/'
    common={'AccessKeyId':c['access_key_id'],'Action':action,'Format':'JSON','RegionId':c['region'],'SignatureMethod':'HMAC-SHA1','SignatureNonce':str(time.time_ns()),'SignatureVersion':'1.0','Version':'2018-10-12'}
    common.update(params); qs='&'.join(f'{percent_encode(k)}={percent_encode(common[k])}' for k in sorted(common))
    canonical='/'.join([])
    signstr='GET&%2F&'+percent_encode(qs)
    key=c['access_key_secret']+'&'
    sig=base64.b64encode(hmac.new(key.encode(),signstr.encode(),hashlib.sha1).digest()).decode()
    common['Signature']=sig
    r=requests.get(endpoint,params=common,timeout=90)
    if r.status_code>=400: raise RuntimeError(f'HTTP {r.status_code}: {r.text[:800]}')
    d=r.json()
    if 'Code' in d and d.get('Code') not in (None,'200'): raise RuntimeError(str(d))
    return d


def check_aliyun(c):
    d=aliyun_rpc(c,'TranslateGeneral',{'FormatType':'text','SourceLanguage':'en','TargetLanguage':'zh','SourceText':'Hello'})
    if 'Data' not in d: raise RuntimeError(str(d))
    print('[INFO] Alibaba Cloud MT connected.')


def translate_aliyun(texts,c):
    out=[]
    for x in texts:
        d=aliyun_rpc(c,'TranslateGeneral',{'FormatType':'text','SourceLanguage':'en','TargetLanguage':'zh','SourceText':x})
        data=d.get('Data') or {}; val=data.get('Translated') or data.get('TranslatedText')
        if val is None: raise RuntimeError(str(d))
        out.append(val)
    return out


def provider_funcs(name):
    return {'DeepL':(check_deepl,translate_deepl),'Google Cloud Translation':(check_google,translate_google),'Microsoft Translator':(check_microsoft,translate_microsoft),'Amazon Translate':(check_amazon,translate_amazon),'Baidu Translate':(check_baidu,translate_baidu),'Alibaba Cloud MT':(check_aliyun,translate_aliyun)}[name]


def validate_epub(path):
    if not path.exists(): raise ValueError(f'Input file does not exist: {path}')
    if path.suffix.lower()!='.epub': raise ValueError(f'Input must be an .epub file, not: {path.name}')
    if not zipfile.is_zipfile(path): raise ValueError(f'Not a valid EPUB/ZIP archive: {path}')
    with zipfile.ZipFile(path) as z:
        if 'META-INF/container.xml' not in z.namelist(): raise ValueError('Invalid EPUB: META-INF/container.xml is missing. Use the original .epub.')


def is_translatable_text(node):
    if not isinstance(node,NavigableString): return False
    p=node.parent
    if not p or p.name in {'script','style','code','pre','svg','math'}: return False
    s=re.sub(r'\s+',' ',str(node)).strip()
    return bool(s and re.search(r'[A-Za-z]{2,}',s))


def process_xhtml(raw, translate_fn):
    soup=BeautifulSoup(raw,'html.parser')
    nodes=[n for n in soup.find_all(string=True) if is_translatable_text(n)]
    if not nodes: return raw,0,0
    texts=[re.sub(r'\s+',' ',str(n)).strip() for n in nodes]
    translations=[]
    # Safe batch sizes for all providers. 20 nodes avoids oversized requests.
    for i in range(0,len(texts),20):
        batch=texts[i:i+20]
        got=translate_fn(batch)
        if len(got)!=len(batch): raise RuntimeError('Provider returned an unexpected number of translations.')
        translations.extend(got)
        print(f'      translated {min(i+len(batch),len(texts))}/{len(texts)} text nodes')
    soup=BeautifulSoup(raw,'html.parser'); nodes2=[n for n in soup.find_all(string=True) if is_translatable_text(n)]
    for node,zh in zip(nodes2,translations):
        parent=node.parent
        if not parent or not zh.strip(): continue
        replacement=NavigableString(str(node))
        node.replace_with(replacement)
        br=soup.new_tag('br'); span=soup.new_tag('span',attrs={'class':'bilingual-zh'}); span.string=zh.strip()
        replacement.insert_after(br); br.insert_after(span)
    head=soup.find('head')
    if head and not soup.find('style',attrs={'id':'bilingual-epub-style'}):
        st=soup.new_tag('style',id='bilingual-epub-style'); st.string='.bilingual-zh{display:block;margin:.35em 0 1em 0;}' ; head.append(st)
    return str(soup).encode('utf-8'),len(nodes),sum(len(x) for x in translations)


def output_name(inp): return inp.with_name(inp.stem+'_EN-ZH.epub')


def main():
    print('='*68); print('  English -> English + Chinese EPUB Translator'); print('  Multi-provider edition: free-capacity APIs'); print('='*68)
    base=Path(__file__).resolve().parent
    try: choice,name=choose_provider(base); creds,path=credentials_for(choice,name,base)
    except Exception as e: return die(str(e))
    check,trans=provider_funcs(name)
    try:
        check(creds)
    except Exception as e:
        print(f'[ERROR] {name} authentication/connection failed: {e}')
        print('[INFO] Credentials were NOT saved. Fix them and run again.')
        return 1
    # Save only after validation succeeds.
    save_json(path, {k:v for k,v in creds.items() if not k.startswith('_')})
    args=sys.argv[1:]
    if args: inp=Path(args[0]).expanduser().resolve()
    else: inp=Path(input('EPUB path (drag the ORIGINAL English .epub here): ').strip().strip('"')).expanduser().resolve()
    try: validate_epub(inp)
    except Exception as e: return die(str(e))
    out=Path(args[1]).expanduser().resolve() if len(args)>1 else output_name(inp)
    if out==inp: return die('Output path must differ from input path.')
    print(f'[INFO] Input : {inp}'); print(f'[INFO] Output: {out}'); print(f'[INFO] API   : {name}'); print('[INFO] GPU   : not used')
    try:
        with zipfile.ZipFile(inp,'r') as zin:
            names=zin.namelist(); xhtmls=[n for n in names if n.lower().endswith(('.xhtml','.html','.htm'))]
            print(f'[INFO] XHTML documents: {len(xhtmls)}')
            out.parent.mkdir(parents=True,exist_ok=True)
            fd,tmpname=tempfile.mkstemp(prefix=out.stem+'.',suffix='.tmp.epub',dir=str(out.parent)); os.close(fd)
            try:
                with zipfile.ZipFile(tmpname,'w') as zout:
                    if 'mimetype' in names: zout.writestr('mimetype',zin.read('mimetype'),compress_type=zipfile.ZIP_STORED)
                    for idx,namex in enumerate(names):
                        if namex=='mimetype': continue
                        data=zin.read(namex)
                        if namex in xhtmls:
                            print(f'[INFO] [{xhtmls.index(namex)+1}/{len(xhtmls)}] {namex}')
                            data,_,_=process_xhtml(data,lambda batch: trans(batch,creds))
                        zout.writestr(namex,data)
                with zipfile.ZipFile(tmpname) as test:
                    if 'META-INF/container.xml' not in test.namelist(): raise RuntimeError('Generated EPUB failed validation: META-INF/container.xml missing.')
                if out.exists(): out.unlink()
                shutil.move(tmpname,out)
            finally:
                if os.path.exists(tmpname): os.unlink(tmpname)
    except Exception as e:
        return die(str(e))
    print('='*68); print('[SUCCESS] Translation completed.'); print(f'[SUCCESS] Output: {out}'); print('='*68)
    return 0

if __name__=='__main__': raise SystemExit(main())
