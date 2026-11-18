"""Model discovery on the explicitly configured provider origin."""
from urllib.parse import urlsplit, urlunsplit

from benchmark import completion_url


def catalog_urls(base_url):
    completion_url(base_url)  # Apply the same URL and credential validation as inference.
    parts = urlsplit(base_url.strip().rstrip('/'))
    path = parts.path.rstrip('/')
    if path.endswith('/models'):
        paths = [path]
    elif path.endswith('/chat/completions'):
        paths = [path.removesuffix('/chat/completions') + '/models']
    elif not path:
        paths = ['/v1/models', '/models']
    else:
        paths = [path + '/models']
    return [urlunsplit((parts.scheme, parts.netloc, path, '', '')) for path in paths]


def provider_error(payload):
    if not isinstance(payload, dict):
        return ''
    error = payload.get('error')
    if isinstance(error, dict):
        return str(error.get('message') or error.get('code') or '')
    if error:
        return str(error)
    if payload.get('code') not in (None, 0, '0', 200, '200'):
        return str(payload.get('detail') or payload.get('message') or payload['code'])
    return ''


def catalog_models(payload):
    error = provider_error(payload)
    if error:
        raise ValueError(error)
    items = payload.get('data', payload.get('models')) if isinstance(payload, dict) else payload
    if isinstance(items, dict):
        items = items.get('models', items.get('data'))
    if not isinstance(items, list):
        raise ValueError('响应中没有模型列表（data 或 models 数组）')
    result = []
    seen = set()
    for item in items[:10000]:
        name = item if isinstance(item, str) else (item.get('id') or item.get('model') or item.get('name')) if isinstance(item, dict) else None
        if isinstance(name, str) and 0 < len(name.strip()) <= 120 and name.strip() not in seen:
            seen.add(name.strip())
            result.append(name.strip())
    return result
