"""Thin Ollama client. Config lives in module_config under 'AICHAT'."""

import requests
from database import get_module_config

DEFAULTS = {
    'enabled':           False,
    'base_url':          'http://localhost:11434',
    'model':             'llama3.2:3b',   # routing + narration
    'sql_model':         '',              # blank -> use `model`
    'keep_alive':        '30m',           # cold-loading an 18GB model per request is brutal
    'timeout':           180,             # CPU inference is slow; this is not a typo
    'temperature':       0.0,
    'max_rows':          50000,           # source rows loaded into sqlite
    'max_result_rows':   5000,            # rows returned to the caller
    'max_history_chars': 12000,           # full history is kept until this cap, then oldest drops
}


def get_config():
    cfg = dict(DEFAULTS)
    cfg.update(get_module_config('AICHAT') or {})
    return cfg


def _url(cfg, path):
    return cfg['base_url'].rstrip('/') + path


def chat(messages, cfg=None, schema=None, model=None):
    """One non-streaming /api/chat round trip. Returns the message content.

    `schema` is a JSON Schema dict -> Ollama constrains decoding to it, which
    makes malformed JSON impossible and is markedly faster than letting the
    model free-form its way to the same shape.
    """
    # ponytail: no streaming yet — nothing consumes it while testing via curl.
    # Add SSE when the chat UI lands; only the narration stage benefits.
    cfg = cfg or get_config()
    payload = {
        'model':      model or cfg['model'],
        'messages':   messages,
        'stream':     False,
        'keep_alive': cfg['keep_alive'],
        'options':    {'temperature': cfg['temperature']},
    }
    if schema:
        payload['format'] = schema
    r = requests.post(_url(cfg, '/api/chat'), json=payload, timeout=cfg['timeout'])
    r.raise_for_status()
    return r.json()['message']['content']


def list_models(cfg=None):
    """Installed models, for the admin Test Connection button."""
    cfg = cfg or get_config()
    r = requests.get(_url(cfg, '/api/tags'), timeout=15)
    r.raise_for_status()
    return [m['name'] for m in r.json().get('models', [])]
