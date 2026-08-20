"""The model behind the chat: a self-hosted Ollama, or the Gemini API.

Both backends answer the same call - messages in, text out, optionally
constrained to a JSON schema - so nothing upstream knows or cares which is
configured. Config lives in module_config under 'AICHAT'.

The choice is not only about speed. Ollama keeps every question and every row
of port data on your own hardware; Gemini sends both to Google. That is the
trade, and it is why the provider is an explicit switch rather than a fallback.
"""

import json

import requests

from database import get_module_config

OLLAMA = 'ollama'
GEMINI = 'gemini'

GEMINI_HOST = 'https://generativelanguage.googleapis.com/v1beta'

DEFAULTS = {
    'enabled':           False,
    'provider':          OLLAMA,

    # -- self-hosted (Ollama) ------------------------------------------------
    'base_url':          'http://localhost:11434',
    # One capable model handles all three stages. Splitting only pays if a
    # small fast model is good enough for triage and narration but not SQL.
    'model':             'qwen2.5-coder:7b',
    'sql_model':         '',              # blank -> use `model`
    'keep_alive':        '30m',           # cold-loading per request is brutal

    # -- hosted (Gemini) -----------------------------------------------------
    'gemini_api_key':    '',
    'gemini_model':      'gemini-2.5-flash',

    # -- shared --------------------------------------------------------------
    'db_user':           '',              # read-only DB role for the AI path
    'db_password':       '',
    'timeout':           180,             # CPU inference is slow; not a typo
    'temperature':       0.0,
    'max_rows':          50000,           # source rows loaded into sqlite
    'max_result_rows':   2000,            # rows the SQL stage may return
    'max_history_chars': 12000,           # full history kept until this cap
}


def get_config():
    cfg = dict(DEFAULTS)
    cfg.update(get_module_config('AICHAT') or {})
    return cfg


def provider(cfg):
    return GEMINI if cfg.get('provider') == GEMINI else OLLAMA


def chat(messages, cfg=None, schema=None, model=None, num_predict=None):
    """One round trip. Returns the assistant's text.

    `schema` is a JSON Schema dict; both backends constrain decoding to it,
    which makes malformed JSON impossible. `num_predict` caps output tokens -
    uncapped, a small model rambles well past the answer.
    """
    cfg = cfg or get_config()
    if provider(cfg) == GEMINI:
        return _gemini_chat(messages, cfg, schema, num_predict)
    return _ollama_chat(messages, cfg, schema, model, num_predict)


def list_models(cfg=None):
    """Installed / available models, for the admin Test Connection button."""
    cfg = cfg or get_config()
    if provider(cfg) == GEMINI:
        key = (cfg.get('gemini_api_key') or '').strip()
        if not key:
            raise ValueError('No Gemini API key configured')
        r = requests.get(GEMINI_HOST + '/models', params={'key': key}, timeout=20)
        _raise_with_body(r)
        return sorted(
            m['name'].split('/')[-1] for m in r.json().get('models', [])
            if 'generateContent' in (m.get('supportedGenerationMethods') or []))
    r = requests.get(cfg['base_url'].rstrip('/') + '/api/tags', timeout=15)
    _raise_with_body(r)
    return [m['name'] for m in r.json().get('models', [])]


def _raise_with_body(response):
    """Both APIs put the useful part of a failure in the body, not the status."""
    if response.status_code >= 400:
        raise requests.HTTPError('HTTP %s: %s' % (response.status_code,
                                                  response.text[:400]))


# -- Ollama ------------------------------------------------------------------

def _ollama_chat(messages, cfg, schema, model, num_predict):
    # ponytail: no streaming yet - nothing consumes it. Add SSE with the UI;
    # only the narration stage benefits.
    options = {'temperature': cfg['temperature']}
    if num_predict:
        options['num_predict'] = num_predict
    payload = {
        'model':      model or cfg['model'],
        'messages':   messages,
        'stream':     False,
        'keep_alive': cfg['keep_alive'],
        'options':    options,
    }
    if schema:
        payload['format'] = schema
    r = requests.post(cfg['base_url'].rstrip('/') + '/api/chat',
                      json=payload, timeout=cfg['timeout'])
    _raise_with_body(r)
    return r.json()['message']['content']


# -- Gemini ------------------------------------------------------------------

_GEMINI_TYPES = {'string': 'STRING', 'number': 'NUMBER', 'integer': 'INTEGER',
                 'boolean': 'BOOLEAN', 'array': 'ARRAY', 'object': 'OBJECT'}


def to_gemini_schema(node):
    """JSON Schema -> the OpenAPI subset Gemini's responseSchema accepts.

    Types are uppercase there, and anything it does not recognise is rejected
    outright rather than ignored, so unknown keys are dropped.
    """
    if not isinstance(node, dict):
        return node
    out = {}
    for k, v in node.items():
        if k == 'type':
            out['type'] = _GEMINI_TYPES.get(v, v)
        elif k == 'properties':
            out['properties'] = {pk: to_gemini_schema(pv) for pk, pv in v.items()}
        elif k == 'items':
            out['items'] = to_gemini_schema(v)
        elif k in ('enum', 'required'):
            out[k] = v
    return out


def to_gemini_messages(messages):
    """(systemInstruction, contents).

    Gemini keeps the system prompt in its own field and calls the assistant
    role 'model'.
    """
    system = '\n\n'.join(m['content'] for m in messages if m.get('role') == 'system')
    contents = [{'role': 'model' if m.get('role') == 'assistant' else 'user',
                 'parts': [{'text': m.get('content', '')}]}
                for m in messages if m.get('role') != 'system']
    return system, contents


def _gemini_chat(messages, cfg, schema, num_predict):
    key = (cfg.get('gemini_api_key') or '').strip()
    if not key:
        raise ValueError('Gemini is selected but no API key is configured')
    model = (cfg.get('gemini_model') or DEFAULTS['gemini_model']).strip()

    system, contents = to_gemini_messages(messages)
    gen = {'temperature': cfg['temperature']}
    if num_predict:
        # The 2.5 models spend output tokens on reasoning before they answer,
        # and a budget sized for Ollama gets consumed entirely by that, leaving
        # an empty response. Give the hosted model room; it bills on what it
        # uses, not on the ceiling.
        gen['maxOutputTokens'] = max(1024, num_predict * 8)
    if schema:
        gen['responseMimeType'] = 'application/json'
        gen['responseSchema'] = to_gemini_schema(schema)

    payload = {'contents': contents, 'generationConfig': gen}
    if system:
        payload['systemInstruction'] = {'parts': [{'text': system}]}

    r = requests.post('%s/models/%s:generateContent' % (GEMINI_HOST, model),
                      params={'key': key}, json=payload, timeout=cfg['timeout'])
    _raise_with_body(r)
    return read_gemini_reply(r.json())


def read_gemini_reply(data):
    candidates = data.get('candidates') or []
    if not candidates:
        blocked = (data.get('promptFeedback') or {}).get('blockReason')
        raise ValueError('Gemini returned nothing' +
                         (' (blocked: %s)' % blocked if blocked else ''))
    top = candidates[0]
    parts = (top.get('content') or {}).get('parts') or []
    text = ''.join(p.get('text', '') for p in parts)
    if not text.strip():
        raise ValueError('Gemini returned an empty answer (finishReason %s)'
                         % top.get('finishReason', 'unknown'))
    return text
