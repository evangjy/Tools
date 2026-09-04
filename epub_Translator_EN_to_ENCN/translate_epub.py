import os
import sys
import time
import re
import zipfile
import tempfile
import hashlib
import urllib.parse
import posixpath
import hmac
import base64
import json
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

import requests
from bs4 import BeautifulSoup, NavigableString


PROVIDERS = {
    '1': 'DeepL',
    '2': 'Google Cloud Translation',
    '3': 'Microsoft Translator',
    '4': 'Amazon Translate',
    '5': 'Baidu Translate',
    '6': 'Alibaba Cloud MT',
}

# Errors which mean the account/key is usable but its quota/rate limit is exhausted.
class QuotaError(RuntimeError):
    pass


class ProviderError(RuntimeError):
    pass


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
        try:
            return json.loads(p.read_text(encoding='utf-8')), p
        except Exception:
            pass
    return {}, p


def choose_provider():
    print('\nTranslation provider:')
    for k, v in PROVIDERS.items():
        print(f'  {k}. {v}')
    choice = input('Select [1-6]: ').strip()
    if choice not in PROVIDERS:
        raise ValueError('Invalid provider selection.')
    return choice, PROVIDERS[choice]


def credentials_for(choice, name, base, force_new=False):
    c, path = load_saved(base, name)

    # If the previous run detected quota exhaustion, force the user to enter
    # a new key instead of silently reusing the exhausted saved credential.
    if force_new:
        c = {}
        print(f'[INFO] Please enter NEW credentials for {name}.')

    if name == 'DeepL':
        key = '' if force_new else os.getenv('DEEPL_API_KEY', '').strip() or c.get('api_key', '').strip()
        if not key:
            key = prompt_secret('DeepL API key: ')
        if not key:
            raise ValueError('No DeepL API key supplied.')
        c = {'api_key': key, 'endpoint': c.get('endpoint', 'https://api-free.deepl.com')}

    elif name == 'Google Cloud Translation':
        key = '' if force_new else os.getenv('GOOGLE_TRANSLATE_API_KEY', '').strip() or c.get('api_key', '').strip()
        if not key:
            key = prompt_secret('Google Cloud Translation API key: ')
        if not key:
            raise ValueError('No Google API key supplied.')
        c = {'api_key': key}

    elif name == 'Microsoft Translator':
        key = '' if force_new else os.getenv('AZURE_TRANSLATOR_KEY', '').strip() or c.get('key', '').strip()
        region = '' if force_new else os.getenv('AZURE_TRANSLATOR_REGION', '').strip() or c.get('region', '').strip()
        if not key:
            key = prompt_secret('Azure Translator key: ')
        if not region:
            region = prompt_secret('Azure Translator region (e.g. global, southeastasia): ')
        if not key or not region:
            raise ValueError('Microsoft Translator key/region required.')
        c = {'key': key, 'region': region}

    elif name == 'Amazon Translate':
        ak = '' if force_new else os.getenv('AWS_ACCESS_KEY_ID', '').strip() or c.get('access_key_id', '').strip()
        sk = '' if force_new else os.getenv('AWS_SECRET_ACCESS_KEY', '').strip() or c.get('secret_access_key', '').strip()
        region = '' if force_new else os.getenv('AWS_DEFAULT_REGION', '').strip() or c.get('region', '').strip()
        region = region or 'us-east-1'
        if not ak:
            ak = prompt_secret('AWS Access Key ID: ')
        if not sk:
            sk = prompt_secret('AWS Secret Access Key: ')
        if not ak or not sk:
            raise ValueError('AWS credentials required.')
        c = {'access_key_id': ak, 'secret_access_key': sk, 'region': region}

    elif name == 'Baidu Translate':
        appid = '' if force_new else os.getenv('BAIDU_TRANSLATE_APPID', '').strip() or c.get('appid', '').strip()
        secret = '' if force_new else os.getenv('BAIDU_TRANSLATE_SECRET', '').strip() or c.get('secret', '').strip()
        if not appid:
            appid = prompt_secret('Baidu Translate APP ID: ')
        if not secret:
            secret = prompt_secret('Baidu Translate 密钥 (Secret Key): ')
        if not appid or not secret:
            raise ValueError('Baidu APP ID/Secret required.')
        c = {'appid': appid, 'secret': secret}

    elif name == 'Alibaba Cloud MT':
        ak = '' if force_new else os.getenv('ALIBABA_ACCESS_KEY_ID', '').strip() or c.get('access_key_id', '').strip()
        sk = '' if force_new else os.getenv('ALIBABA_ACCESS_KEY_SECRET', '').strip() or c.get('access_key_secret', '').strip()
        region = '' if force_new else os.getenv('ALIBABA_REGION', '').strip() or c.get('region', 'cn-hangzhou').strip()
        region = region or 'cn-hangzhou'
        if not ak:
            ak = prompt_secret('Alibaba Cloud AccessKey ID: ')
        if not sk:
            sk = prompt_secret('Alibaba Cloud AccessKey Secret: ')
        if not ak or not sk:
            raise ValueError('Alibaba AccessKey ID/Secret required.')
        c = {'access_key_id': ak, 'access_key_secret': sk, 'region': region}

    return c, path


def _response_error(provider, r):
    """Turn common quota/rate-limit responses into QuotaError."""
    text = r.text[:1200]
    low = text.lower()

    quota_words = (
        'quota', 'limit exceeded', 'quota exceeded', 'too many requests',
        'rate limit', 'ratelimit', 'monthly limit', 'character_limit',
        'daily limit', 'resource exhausted', 'throttl', 'insufficient quota',
        'limitexceeded', 'toomanyrequests', '54003', '54004', '54005',
    )
    status_quota = r.status_code in (429, 456)
    if status_quota or any(w in low for w in quota_words):
        raise QuotaError(f'{provider} quota/rate limit reached (HTTP {r.status_code}): {text}')
    raise ProviderError(f'{provider} API error (HTTP {r.status_code}): {text}')


def check_deepl(c):
    endpoint = c.get('endpoint', 'https://api-free.deepl.com').rstrip('/')
    r = requests.get(endpoint + '/v2/usage', headers={'Authorization': f'DeepL-Auth-Key {c["api_key"]}'}, timeout=30)
    if r.status_code >= 400:
        _response_error('DeepL', r)
    d = r.json()
    used = d.get('character_count', 0)
    limit = d.get('character_limit', 0)
    print(f'[INFO] DeepL connected: {used:,} / {limit:,} characters used')
    if limit and used >= limit:
        raise QuotaError(f'DeepL character quota exhausted: {used:,} / {limit:,}.')


def translate_deepl(texts, c):
    endpoint = c.get('endpoint', 'https://api-free.deepl.com').rstrip('/')
    r = requests.post(
        endpoint + '/v2/translate',
        headers={'Authorization': f'DeepL-Auth-Key {c["api_key"]}'},
        data=[('text', x) for x in texts] + [('target_lang', 'ZH')],
        timeout=90,
    )
    if r.status_code >= 400:
        _response_error('DeepL', r)
    try:
        result = r.json()['translations']
    except Exception as e:
        raise ProviderError(f'DeepL returned invalid JSON: {r.text[:800]}') from e
    if len(result) != len(texts):
        raise ProviderError(f'DeepL returned {len(result)} translations for {len(texts)} inputs.')
    return [x['text'] for x in result]


def check_google(c):
    r = requests.post(
        'https://translation.googleapis.com/language/translate/v2',
        params={'key': c['api_key']},
        json={'q': 'Hello', 'target': 'zh-CN', 'format': 'text'},
        timeout=30,
    )
    if r.status_code >= 400:
        _response_error('Google Cloud Translation', r)
    print('[INFO] Google Cloud Translation connected.')


def translate_google(texts, c):
    r = requests.post(
        'https://translation.googleapis.com/language/translate/v2',
        params={'key': c['api_key']},
        json={'q': texts, 'source': 'en', 'target': 'zh-CN', 'format': 'text'},
        timeout=90,
    )
    if r.status_code >= 400:
        _response_error('Google Cloud Translation', r)
    try:
        result = r.json()['data']['translations']
    except Exception as e:
        raise ProviderError(f'Google returned invalid JSON: {r.text[:800]}') from e
    if len(result) != len(texts):
        raise ProviderError(f'Google returned {len(result)} translations for {len(texts)} inputs.')
    return [x['translatedText'] for x in result]


def check_microsoft(c):
    h = {
        'Ocp-Apim-Subscription-Key': c['key'],
        'Ocp-Apim-Subscription-Region': c['region'],
        'Content-Type': 'application/json',
    }
    r = requests.post(
        'https://api.cognitive.microsofttranslator.com/translate',
        params={'api-version': '3.0', 'from': 'en', 'to': 'zh-Hans'},
        headers=h,
        json=[{'Text': 'Hello'}],
        timeout=30,
    )
    if r.status_code >= 400:
        _response_error('Microsoft Translator', r)
    print('[INFO] Microsoft Translator connected.')


def translate_microsoft(texts, c):
    h = {
        'Ocp-Apim-Subscription-Key': c['key'],
        'Ocp-Apim-Subscription-Region': c['region'],
        'Content-Type': 'application/json',
    }
    r = requests.post(
        'https://api.cognitive.microsofttranslator.com/translate',
        params={'api-version': '3.0', 'from': 'en', 'to': 'zh-Hans'},
        headers=h,
        json=[{'Text': x} for x in texts],
        timeout=90,
    )
    if r.status_code >= 400:
        _response_error('Microsoft Translator', r)
    result = r.json()
    if len(result) != len(texts):
        raise ProviderError(f'Microsoft returned {len(result)} translations for {len(texts)} inputs.')
    return [x['translations'][0]['text'] for x in result]


def aws_sign_v4(method, url, region, service, access_key, secret_key, payload, headers_extra=None):
    t = time.gmtime()
    amzdate = time.strftime('%Y%m%dT%H%M%SZ', t)
    datestamp = amzdate[:8]
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc
    payload_hash = hashlib.sha256(payload.encode()).hexdigest()
    headers = {
        'content-type': 'application/x-amz-json-1.1',
        'host': host,
        'x-amz-date': amzdate,
        'x-amz-target': 'AWSShineFrontendService_20170701.TranslateText',
    }
    if headers_extra:
        headers.update(headers_extra)
    canonical_headers = ''.join(f'{k.lower()}:{str(headers[k]).strip()}\n' for k in sorted(headers))
    signed = ';'.join(k.lower() for k in sorted(headers))
    canonical = f'{method}\n{parsed.path or "/"}\n{parsed.query}\n{canonical_headers}\n{signed}\n{payload_hash}'
    scope = f'{datestamp}/{region}/{service}/aws4_request'
    string_to_sign = f'AWS4-HMAC-SHA256\n{amzdate}\n{scope}\n{hashlib.sha256(canonical.encode()).hexdigest()}'

    def H(k, m):
        return hmac.new(k, m.encode(), hashlib.sha256).digest()

    kDate = H(('AWS4' + secret_key).encode(), datestamp)
    kRegion = H(kDate, region)
    kService = H(kRegion, service)
    kSigning = H(kService, 'aws4_request')
    sig = hmac.new(kSigning, string_to_sign.encode(), hashlib.sha256).hexdigest()
    headers['Authorization'] = f'AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed}, Signature={sig}'
    return headers


def check_amazon(c):
    url = f'https://translate.{c["region"]}.amazonaws.com/'
    payload = json.dumps({'Text': 'Hello', 'SourceLanguageCode': 'en', 'TargetLanguageCode': 'zh'})
    h = aws_sign_v4('POST', url, c['region'], 'translate', c['access_key_id'], c['secret_access_key'], payload)
    r = requests.post(url, headers=h, data=payload, timeout=30)
    if r.status_code >= 400:
        _response_error('Amazon Translate', r)
    print('[INFO] Amazon Translate connected.')


def translate_amazon(texts, c):
    out = []
    url = f'https://translate.{c["region"]}.amazonaws.com/'
    for x in texts:
        payload = json.dumps({'Text': x, 'SourceLanguageCode': 'en', 'TargetLanguageCode': 'zh'})
        h = aws_sign_v4('POST', url, c['region'], 'translate', c['access_key_id'], c['secret_access_key'], payload)
        r = requests.post(url, headers=h, data=payload, timeout=90)
        if r.status_code >= 400:
            _response_error('Amazon Translate', r)
        try:
            out.append(r.json()['TranslatedText'])
        except Exception as e:
            raise ProviderError(f'Amazon Translate returned invalid JSON: {r.text[:800]}') from e
    return out


def check_baidu(c):
    """Validate Baidu General Text Translation credentials using official sign auth.

    IMPORTANT:
      - c["appid"] is Baidu APPID
      - c["secret"] is the platform-issued 密钥 / Secret Key
      - There is NO OAuth token request here.
    """
    test_text = "Hello"
    salt = str(int(time.time() * 1000))
    sign = hashlib.md5(
        (c["appid"] + test_text + salt + c["secret"]).encode("utf-8")
    ).hexdigest()

    r = requests.post(
        "https://fanyi-api.baidu.com/api/trans/vip/translate",
        data={
            "q": test_text,
            "from": "en",
            "to": "zh",
            "appid": c["appid"],
            "salt": salt,
            "sign": sign,
        },
        timeout=30,
    )
    if r.status_code >= 400:
        _response_error("Baidu Translate", r)

    try:
        d = r.json()
    except Exception as e:
        raise ProviderError(
            f"Baidu Translate returned invalid JSON: {r.text[:800]}"
        ) from e

    if "error_code" in d:
        code = str(d.get("error_code"))
        msg = str(d.get("error_msg", "unknown error"))
        if code in {"54003", "54004", "54005"}:
            raise QuotaError(f"Baidu Translate error {code}: {msg}")
        if code == "52003":
            raise ProviderError(
                f"Baidu APPID unauthorized or service not enabled (52003): {msg}"
            )
        if code == "54001":
            raise ProviderError(
                "Baidu sign authentication failed (54001). "
                "The script is using APPID + 密钥 with the official MD5 formula; "
                f"server response: {msg}"
            )
        raise ProviderError(f"Baidu Translate error {code}: {msg}")

    if "trans_result" not in d:
        raise ProviderError(f"Baidu Translate unexpected response: {json.dumps(d, ensure_ascii=False)}")

    print("[INFO] Baidu Translate connected (APPID + 密钥 / sign auth).")


def translate_baidu(texts, c):
    """Translate complete paragraph strings with Baidu General Text Translation.

    One input item = one complete EPUB block. We deliberately do not split a
    paragraph into smaller text nodes, because doing so destroys bilingual
    layout. Baidu documents 6000 characters as the hard q limit and recommends
    keeping a request around 2000 characters for quality/speed.
    """
    out = []

    for text in texts:
        if not text or not text.strip():
            out.append("")
            continue

        if len(text) > 6000:
            raise ProviderError(
                "Baidu Translate cannot translate this EPUB paragraph as one unit: "
                f"it contains {len(text)} characters, above the 6000-character limit. "
                "The script refuses to split it because splitting would violate the "
                "required one-English-paragraph -> one-Chinese-paragraph layout."
            )

        salt = str(int(time.time() * 1000))
        sign = hashlib.md5(
            (c["appid"] + text + salt + c["secret"]).encode("utf-8")
        ).hexdigest()

        r = requests.post(
            "https://fanyi-api.baidu.com/api/trans/vip/translate",
            data={
                "q": text,
                "from": "en",
                "to": "zh",
                "appid": c["appid"],
                "salt": salt,
                "sign": sign,
            },
            timeout=90,
        )

        if r.status_code >= 400:
            _response_error("Baidu Translate", r)

        try:
            d = r.json()
        except Exception as e:
            raise ProviderError(
                f"Baidu Translate returned invalid JSON: {r.text[:800]}"
            ) from e

        if "error_code" in d:
            code = str(d.get("error_code"))
            msg = str(d.get("error_msg", "unknown error"))

            if code in {"54003", "54004", "54005"}:
                raise QuotaError(f"Baidu Translate error {code}: {msg}")

            if code == "54001":
                raise ProviderError(
                    f"Baidu sign authentication failed (54001): {msg}. "
                    "Check that the APPID and the platform-issued 密钥 belong "
                    "to the same Baidu translation application."
                )

            if code == "52003":
                raise ProviderError(
                    f"Baidu APPID is unauthorized or the service is not enabled (52003): {msg}"
                )

            if code in {"58000", "58001", "58002", "58003", "90107"}:
                raise ProviderError(f"Baidu Translate error {code}: {msg}")

            raise ProviderError(f"Baidu Translate error {code}: {msg}")

        result = d.get("trans_result")
        if not isinstance(result, list):
            raise ProviderError(
                f"Baidu Translate returned no trans_result: {json.dumps(d, ensure_ascii=False)[:1000]}"
            )

        # Baidu may return multiple src/dst records for a paragraph. Join them
        # with newlines; this remains ONE Chinese paragraph/block in the EPUB.
        out.append("\n".join(str(x.get("dst", "")) for x in result).strip())

    return out

def percent_encode(v):
    return urllib.parse.quote(str(v), safe='-_.~')


def aliyun_rpc(c, action, params):
    endpoint = 'https://mt.cn-hangzhou.aliyuncs.com/'
    common = {
        'AccessKeyId': c['access_key_id'],
        'Action': action,
        'Format': 'JSON',
        'RegionId': c['region'],
        'SignatureMethod': 'HMAC-SHA1',
        # Must be unique per request or Aliyun rejects it as a replay
        # ("InvalidSignatureNonce.Used"). PID is appended for extra safety
        # when calls happen faster than the clock resolution.
        'SignatureNonce': f'{time.time_ns()}{os.getpid()}',
        'SignatureVersion': '1.0',
        # REQUIRED public parameter. Without it the RPC signature is
        # incomplete and Aliyun's gateway rejects the request before it ever
        # reaches the Machine Translation backend -- which is exactly why
        # nothing showed up in the MT console's call statistics.
        'Timestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'Version': '2018-10-12',
    }
    common.update(params)

    # Sign over the sorted, percent-encoded parameter set (RPC signature
    # mechanism). Signing is identical whether the params are sent via the
    # query string or a POST body.
    qs = '&'.join(f'{percent_encode(k)}={percent_encode(common[k])}' for k in sorted(common))
    signstr = 'POST&%2F&' + percent_encode(qs)
    key = c['access_key_secret'] + '&'
    sig = base64.b64encode(hmac.new(key.encode(), signstr.encode(), hashlib.sha1).digest()).decode()
    common['Signature'] = sig

    # POST (not GET) so long paragraphs never risk silent truncation at a
    # URL-length limit on some intermediate proxy/gateway.
    r = requests.post(
        endpoint,
        data=common,
        headers={'Content-Type': 'application/x-www-form-urlencoded; charset=utf-8'},
        timeout=90,
    )
    if r.status_code >= 400:
        _response_error('Alibaba Cloud MT', r)

    try:
        d = r.json()
    except ValueError as e:
        raise ProviderError(
            f'Alibaba Cloud MT returned a non-JSON response (HTTP {r.status_code}): '
            f'{r.text[:800]}'
        ) from e

    # Code can come back as either an int (200) or a string ("200") depending
    # on the response path, so compare as strings instead of `in (None, '200')`.
    code = d.get('Code')
    if code is not None and str(code) != '200':
        text = json.dumps(d, ensure_ascii=False)
        if any(w in text.lower() for w in ('quota', 'limit', 'throttl', 'too many')):
            raise QuotaError(f'Alibaba Cloud MT quota/throttling error: {text[:800]}')
        raise ProviderError(f'Alibaba Cloud MT error: {text[:800]}')
    return d


def _aliyun_looks_untranslated(source, translated):
    """Detect Aliyun MT echoing the English source back unchanged instead of
    translating it (e.g. because the request was mis-routed/rejected
    internally but still returned Code=200). This is what previously let a
    fully-English "bilingual" EPUB get published with no visible error and
    no entry in the MT console's usage log."""
    s = re.sub(r'\s+', ' ', source or '').strip().lower()
    t = re.sub(r'\s+', ' ', translated or '').strip().lower()
    if not s or s != t:
        return False
    # Allow trivially "untranslated" text (numbers, single words, names) to
    # pass; only flag real runs of English prose matching verbatim.
    return bool(re.search(r'[A-Za-z]{3,}\s+[A-Za-z]{3,}', s))


def check_aliyun(c):
    sample = 'This is a short sentence used only to verify the connection.'
    d = aliyun_rpc(c, 'TranslateGeneral', {
        'FormatType': 'text',
        'Scene': 'general',
        'SourceLanguage': 'en',
        'TargetLanguage': 'zh',
        'SourceText': sample,
    })
    data = d.get('Data') or {}
    val = data.get('Translated') or data.get('TranslatedText')
    if not val:
        raise ProviderError(f'Alibaba Cloud MT returned no translation: {d}')
    if _aliyun_looks_untranslated(sample, val):
        raise ProviderError(
            'Alibaba Cloud MT accepted the request (HTTP 200, Code 200) but '
            f'returned the English text unchanged instead of Chinese: {d}. '
            'This usually means the Machine Translation service itself is '
            'not enabled/subscribed on this account, or the AccessKey/RAM '
            'user lacks the alimt:TranslateGeneral permission -- check the '
            'Machine Translation console (机器翻译) "开通服务" status.'
        )
    print('[INFO] Alibaba Cloud MT connected.')


def translate_aliyun(texts, c):
    out = []
    for x in texts:
        d = aliyun_rpc(c, 'TranslateGeneral', {
            'FormatType': 'text',
            'Scene': 'general',
            'SourceLanguage': 'en',
            'TargetLanguage': 'zh',
            'SourceText': x,
        })
        data = d.get('Data') or {}
        val = data.get('Translated') or data.get('TranslatedText')
        if val is None:
            raise ProviderError(str(d))
        if _aliyun_looks_untranslated(x, val):
            raise ProviderError(
                'Alibaba Cloud MT returned the English text unchanged instead '
                f'of translating it: {json.dumps(d, ensure_ascii=False)[:500]}'
            )
        out.append(val)
    return out


def provider_funcs(name):
    return {
        'DeepL': (check_deepl, translate_deepl),
        'Google Cloud Translation': (check_google, translate_google),
        'Microsoft Translator': (check_microsoft, translate_microsoft),
        'Amazon Translate': (check_amazon, translate_amazon),
        'Baidu Translate': (check_baidu, translate_baidu),
        'Alibaba Cloud MT': (check_aliyun, translate_aliyun),
    }[name]


def validate_epub(path):
    if not path.exists():
        raise ValueError(f'Input file does not exist: {path}')
    if path.suffix.lower() != '.epub':
        raise ValueError(f'Input must be an .epub file, not: {path.name}')
    if not zipfile.is_zipfile(path):
        raise ValueError(f'Not a valid EPUB/ZIP archive: {path}')
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        if 'META-INF/container.xml' not in names:
            raise ValueError('Invalid EPUB: META-INF/container.xml is missing. Use the original .epub.')
        if 'mimetype' not in names:
            raise ValueError('Invalid EPUB: mimetype is missing.')


BLOCK_TAGS = {
    "p", "div", "section", "article", "blockquote",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "li", "dt", "dd", "figcaption", "caption"
}

NESTED_BLOCK_TAGS = BLOCK_TAGS - {"p"}


def _normal_text(s):
    return re.sub(r"[ \t\r\n\f]+", " ", str(s)).strip()


def _is_english_block(el):
    """Return True only for a visible block that should be translated.

    Crucially, the whole block's text is translated as ONE unit. Inline tags
    such as em/strong/a/span are never treated as separate translation units.
    """
    if el.name not in BLOCK_TAGS:
        return False

    # Do not touch navigation, scripts, styles, SVG/MathML, metadata, etc.
    if el.find_parent(["head", "script", "style", "svg", "math", "nav",
                       "noscript", "template", "textarea"]):
        return False

    if el.get("data-no-translate") is not None:
        return False

    # If a block contains another block, it is a container rather than a
    # paragraph unit. Its child blocks will be processed instead.
    if el.find(list(NESTED_BLOCK_TAGS)):
        return False

    # A block containing only an image should never be sent to translation.
    if el.find("img") and not _normal_text(el.get_text(" ", strip=True)):
        return False

    s = _normal_text(el.get_text(" ", strip=True))
    return bool(s and re.search(r"[A-Za-z]{2,}", s))


def _existing_bilingual_after(el):
    """Prevent duplicate Chinese paragraphs when rerunning on an output EPUB."""
    nxt = el.find_next_sibling()
    if nxt and getattr(nxt, "name", None) == "p":
        classes = nxt.get("class") or []
        if "bilingual-zh" in classes:
            return True
    return False


def process_xhtml(raw, translate_fn):
    """Translate complete HTML/XHTML blocks without splitting inline markup.

    Layout rule:
        English block
        Chinese block
        English next block
        Chinese next block

    The original English block is left structurally intact. A new <p
    class="bilingual-zh"> is inserted immediately after it.

    Images, CSS links, attributes, inline <em>/<strong>/<a>/<span>, and all
    non-text resources remain untouched.
    """
    soup = BeautifulSoup(raw, "html.parser")

    body = soup.find("body")
    if body is None:
        return raw, 0, 0

    # Snapshot the block elements BEFORE inserting any Chinese elements.
    blocks = [el for el in body.find_all(list(BLOCK_TAGS)) if _is_english_block(el)]
    if not blocks:
        return raw, 0, 0

    texts = [_normal_text(el.get_text(" ", strip=True)) for el in blocks]
    translations = []

    # Each block remains one translation item. We batch several whole
    # paragraphs only when the provider supports multiple inputs.
    for i in range(0, len(texts), 20):
        batch = texts[i:i + 20]
        got = translate_fn(batch)

        if len(got) != len(batch):
            raise ProviderError(
                f"Provider returned {len(got)} translations for {len(batch)} paragraphs."
            )

        if any(x is None or not str(x).strip() for x in got):
            raise ProviderError("Provider returned an empty translation for a paragraph.")

        translations.extend(str(x).strip() for x in got)
        print(
            f"      translated "
            f"{min(i + len(batch), len(texts))}/{len(texts)} paragraphs"
        )

    # Do not modify the document until ALL translations for this XHTML have
    # succeeded.
    for el, zh in zip(blocks, translations):
        if _existing_bilingual_after(el):
            continue

        p = soup.new_tag("p")
        p["class"] = ["bilingual-zh"]
        p.string = zh

        # Insert immediately after the complete English block.
        el.insert_after(p)

    head = soup.find("head")
    if head and not soup.find("style", attrs={"id": "bilingual-epub-style"}):
        st = soup.new_tag("style", id="bilingual-epub-style")
        st.string = (
            ".bilingual-zh{display:block;margin:.35em 0 1em 0;}"
            ".bilingual-zh + *{margin-top:0;}"
        )
        head.append(st)

    return str(soup).encode("utf-8"), len(blocks), sum(len(x) for x in translations)

def output_name(inp):
    return inp.with_name(inp.stem + '_EN-ZH.epub')


def _legacy_xhtml_filter(names):
    """Old extension-guessing heuristic. Kept only as a last-resort fallback
    for EPUBs whose container.xml/OPF can't be parsed for some reason."""
    return [
        n for n in names
        if n.lower().endswith(('.xhtml', '.html', '.htm'))
        and '/nav' not in n.lower()
        and not n.lower().endswith(('toc.xhtml', 'toc.html', 'toc.htm'))
    ]


def epub_content_documents(zin, names):
    """Return the ZIP entry names of the EPUB's real content documents, in
    spine order, by reading META-INF/container.xml -> the OPF manifest/spine
    -- NOT by guessing from file extensions.

    Many EPUBs (older titles, some publisher toolchains) ship XHTML content
    under non-standard extensions such as .xml while still declaring
    media-type="application/xhtml+xml" in the manifest. A pure
    extension-based filter silently matches zero files for those books,
    which produces a "successful" run that translates nothing and shows no
    error at all -- exactly the "XHTML documents: 0" symptom.
    """
    try:
        container = ET.fromstring(zin.read('META-INF/container.xml'))
        ns = {'c': 'urn:oasis:names:tc:opendocument:xmlns:container'}
        rootfile = container.find('.//c:rootfile', ns)
        opf_path = rootfile.get('full-path') if rootfile is not None else None
        if not opf_path or opf_path not in names:
            return _legacy_xhtml_filter(names)

        opf_dir = posixpath.dirname(opf_path)
        opf = ET.fromstring(zin.read(opf_path))

        def local(tag):
            return tag.rsplit('}', 1)[-1]

        manifest = {}
        for item in opf.iter():
            if local(item.tag) != 'item':
                continue
            item_id = item.get('id')
            href = item.get('href')
            if not item_id or not href:
                continue
            manifest[item_id] = (
                urllib.parse.unquote(href),
                (item.get('media-type') or '').strip().lower(),
                (item.get('properties') or '').strip().lower(),
            )

        spine_idrefs = [
            itemref.get('idref')
            for itemref in opf.iter()
            if local(itemref.tag) == 'itemref' and itemref.get('idref')
        ]

        text_media_types = ('application/xhtml+xml', 'application/x-dtbook+xml', 'text/html')
        docs = []
        for idref in spine_idrefs:
            entry = manifest.get(idref)
            if not entry:
                continue
            href, media_type, properties = entry
            if 'nav' in properties.split():
                continue  # EPUB3 navigation doc, not narrative content
            if media_type not in text_media_types:
                continue
            full = posixpath.normpath(posixpath.join(opf_dir, href)) if opf_dir else href
            full = full[2:] if full.startswith('./') else full
            if full in names:
                docs.append(full)
            else:
                # Try a case-insensitive match as a last resort (some tools
                # write inconsistent casing between the manifest and the ZIP).
                match = next((n for n in names if n.lower() == full.lower()), None)
                if match:
                    docs.append(match)

        return docs or _legacy_xhtml_filter(names)
    except Exception:
        return _legacy_xhtml_filter(names)


def run_translation(inp, out, trans, creds):
    """Build a completely separate temporary EPUB and atomically publish it.

    Existing output is NEVER deleted before successful completion. Therefore an
    API/quota/network failure cannot produce or expose a partial new EPUB.
    """
    validate_epub(inp)
    if out == inp:
        raise ValueError('Output path must differ from input path.')

    print(f'[INFO] Input : {inp}')
    print(f'[INFO] Output: {out}')

    with zipfile.ZipFile(inp, 'r') as zin:
        names = zin.namelist()
        xhtmls = epub_content_documents(zin, names)
        print(f'[INFO] XHTML documents: {len(xhtmls)}')
        if not xhtmls:
            raise ProviderError(
                'No translatable content documents were found in this EPUB '
                '(checked the OPF manifest/spine and, as a fallback, common '
                '.xhtml/.html/.htm extensions). Nothing would be translated, '
                'so no output was written. This EPUB may use an unusual '
                'internal structure -- please share it so the detection '
                'logic can be extended.'
            )
        out.parent.mkdir(parents=True, exist_ok=True)

        fd, tmpname = tempfile.mkstemp(
            prefix=out.stem + '.', suffix='.tmp.epub', dir=str(out.parent)
        )
        os.close(fd)

        try:
            with zipfile.ZipFile(tmpname, 'w') as zout:
                # EPUB requires mimetype to be the first ZIP entry and STORED.
                if 'mimetype' in names:
                    zout.writestr('mimetype', zin.read('mimetype'), compress_type=zipfile.ZIP_STORED)

                for namex in names:
                    if namex == 'mimetype':
                        continue
                    data = zin.read(namex)  # every non-XHTML resource is copied byte-for-byte
                    if namex in xhtmls:
                        pos = xhtmls.index(namex) + 1
                        print(f'[INFO] [{pos}/{len(xhtmls)}] {namex}')
                        data, _, _ = process_xhtml(data, lambda batch: trans(batch, creds))
                    zout.writestr(namex, data)

            # Structural validation before publishing the file.
            with zipfile.ZipFile(tmpname, 'r') as test:
                test_names = test.namelist()
                if not test_names or test_names[0] != 'mimetype':
                    raise ProviderError('Generated EPUB failed validation: mimetype is not the first ZIP entry.')
                if test.getinfo('mimetype').compress_type != zipfile.ZIP_STORED:
                    raise ProviderError('Generated EPUB failed validation: mimetype is compressed.')
                if 'META-INF/container.xml' not in test_names:
                    raise ProviderError('Generated EPUB failed validation: META-INF/container.xml missing.')
                if test.read('mimetype').decode('utf-8', errors='replace').strip() != 'application/epub+zip':
                    raise ProviderError('Generated EPUB failed validation: invalid mimetype content.')
                # Every original ZIP entry must still exist. We only permit
                # XHTML byte changes and the intentionally added bilingual CSS
                # inside those XHTML documents.
                missing = [n for n in names if n not in test_names]
                if missing:
                    raise ProviderError(
                        'Generated EPUB lost original resources: ' + ', '.join(missing[:10])
                    )

            # Publish only after every document and validation step succeeded.
            os.replace(tmpname, out)
            tmpname = None
        finally:
            if tmpname and os.path.exists(tmpname):
                os.unlink(tmpname)


def main():
    print('=' * 68)
    print('  English -> English + Chinese EPUB Translator')
    print('  Multi-provider edition: quota-safe / paragraph-preserving / atomic-output')
    print('=' * 68)

    base = Path(__file__).resolve().parent

    args = sys.argv[1:]
    if args:
        inp = Path(args[0]).expanduser().resolve()
    else:
        inp = Path(input('EPUB path (drag the ORIGINAL English .epub here): ').strip().strip('"')).expanduser().resolve()

    try:
        validate_epub(inp)
    except Exception as e:
        return die(str(e))

    out = Path(args[1]).expanduser().resolve() if len(args) > 1 else output_name(inp)
    if out == inp:
        return die('Output path must differ from input path.')

    # Provider/key selection loop. If a quota error is detected, the saved key
    # is deleted and the user is immediately returned to credential/provider
    # selection instead of getting trapped with the same exhausted key.
    force_new = False
    while True:
        try:
            choice, name = choose_provider()
            creds, cred_path = credentials_for(choice, name, base, force_new=force_new)
            force_new = False
            check, trans = provider_funcs(name)
            print(f'[INFO] Checking {name} credentials/quota...')
            check(creds)

            # Save only credentials that have passed validation.
            save_json(cred_path, {k: v for k, v in creds.items() if not k.startswith('_')})
            break

        except QuotaError as e:
            print(f'[QUOTA] {e}')
            print('[INFO] This credential has hit its quota/rate limit.')
            print('[INFO] It will NOT be reused automatically on the next run.')

            # Delete the exhausted provider's saved credential so the next run
            # cannot silently select the same dead API key.
            try:
                if 'cred_path' in locals() and cred_path.exists():
                    cred_path.unlink()
                    print(f'[INFO] Removed exhausted saved credential: {cred_path.name}')
            except Exception as delete_err:
                print(f'[WARN] Could not remove saved credential automatically: {delete_err}')

            answer = input('\n[1] Choose another provider/key now\n[2] Exit and choose a new key next run\nSelect [1-2]: ').strip()
            if answer == '1':
                force_new = True
                continue
            return 1

        except Exception as e:
            print(f'[ERROR] {name if "name" in locals() else "Provider"} authentication/connection failed: {e}')
            print('[INFO] Credentials were NOT saved. The EPUB has NOT been written.')
            return 1

    print(f'[INFO] Input : {inp}')
    print(f'[INFO] Output: {out}')
    print(f'[INFO] API   : {name}')
    print('[INFO] GPU   : not used')

    try:
        run_translation(inp, out, trans, creds)
    except QuotaError as e:
        print(f'[QUOTA] Translation stopped before publishing output: {e}')
        print('[INFO] No partial EPUB was published.')
        try:
            if cred_path.exists():
                cred_path.unlink()
                print(f'[INFO] Removed exhausted saved credential: {cred_path.name}')
        except Exception as delete_err:
            print(f'[WARN] Could not remove saved credential automatically: {delete_err}')
        print('[INFO] Run again and select another provider or enter a new key.')
        return 1
    except Exception as e:
        print(f'[ERROR] Translation failed before publishing output: {e}')
        print('[INFO] No partial EPUB was published. Any existing output file was preserved.')
        return 1

    print('=' * 68)
    print('[SUCCESS] Translation completed.')
    print(f'[SUCCESS] Output: {out}')
    print('=' * 68)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
