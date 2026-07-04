# Copyright © 2024 UNISI Tech. All rights reserved.
from __future__ import annotations

from typing import get_type_hints, is_typeddict

import json, logging, os, re
from typing import Any, Literal, Union, get_args, get_origin

logger = logging.getLogger(__name__)

import diskcache
from openai import AsyncOpenAI as _AsyncOpenAI

from .common import Unishare

# Populated by setup_llmrag(). None until initialised.
_acompletion = None


# ---------------------------------------------------------------------------
# Query cache
# ---------------------------------------------------------------------------

class QueryCache:
    """
    Persistent LLM response cache backed by diskcache.
    diskcache uses SQLite under the hood: safe for concurrent access,
    supports TTL, and never breaks on special characters in LLM responses.
    """

    def __init__(self, directory: str, ttl: int | None = None) -> None:
        """
        Args:
            directory: path to the cache directory (created automatically).
            ttl: entry lifetime in seconds; None means no expiry.
        """         
        self._cache = diskcache.Cache(directory)
        self._ttl = ttl

    def get(self, key: str) -> str | None:
        return self._cache.get(key)

    def set(self, key: str, value: str) -> None:
        self._cache.set(key, value, expire=self._ttl)

    def close(self) -> None:
        self._cache.close()


# ---------------------------------------------------------------------------
# Type helper functions
# ---------------------------------------------------------------------------

_BUILTIN_TYPE_NAMES: dict[type, str] = {
    int: 'integer',
    float: 'number',
    bool: 'boolean',
    str: 'string',
    dict: 'object',
    list: 'array',
}


def python_type_to_json_schema(type_value: Any) -> str:
    """
    Converts a Python type or typing hint to a textual JSON schema description
    for use in system prompts.

    Examples:
        int              → 'integer'
        dict[str, int]   → 'object of string to integer structure.'
        list[str]        → 'array of string '
        {'name': str}    → 'object with {"name": "[Type: string]"} structure'
    """
    if isinstance(type_value, type):
        if type_value in _BUILTIN_TYPE_NAMES:
            return _BUILTIN_TYPE_NAMES[type_value]

        origin = get_origin(type_value)
        args = get_args(type_value)
        if origin is list:
            return f'array of {python_type_to_json_schema(args[0])} '
        if origin is dict:
            return (
                f'object of {python_type_to_json_schema(args[0])}'
                f' to {python_type_to_json_schema(args[1])} structure.'
            )
        return 'string'

    # Value instance, not a type
    match type_value:
        case str():
            return 'string'
        case int():
            return 'integer'
        case float():
            return 'number'
        case bool():
            return 'boolean'
        case dict():
            if type_value:
                pairs = ', '.join(
                    f'"{k}": "[Type: {python_type_to_json_schema(v)}]"'
                    for k, v in type_value.items()
                )
                return f'object with {{{pairs}}} structure'
            return 'object'
        case list():
            return 'array'
        case _:
            return 'string'


# Kept as a backward-compatible alias for code that imports jstype directly.
jstype = python_type_to_json_schema


def _type_to_schema_dict(type_value: Any) -> dict:
    """
    Internal recursive converter. Unlike python_type_to_json_schema_dict,
    str here ALWAYS becomes {'type': 'string'} — the "free text, no schema"
    sentinel only makes sense at the top-level call (see below).
    """
    if isinstance(type_value, type) and type_value in _BUILTIN_TYPE_NAMES:
        return {'type': _BUILTIN_TYPE_NAMES[type_value]}

    # TypedDict class — top-level or nested (list[SomeTypedDict], a field
    # of another TypedDict). get_type_hints was already imported for this
    # purpose but never wired up: TypedDict classes weren't recognised at
    # all and silently turned into None → {'type': 'string'} in the parent.
    if isinstance(type_value, type) and is_typeddict(type_value):
        hints = get_type_hints(type_value)
        required_keys = getattr(type_value, '__required_keys__', frozenset(hints.keys()))
        properties = {k: _type_to_schema_dict(v) for k, v in hints.items()}
        return {
            'type': 'object',
            'properties': properties,
            'required': sorted(required_keys),
            'additionalProperties': False,
        }

    origin = get_origin(type_value)
    args   = get_args(type_value)

    if origin is list:
        schema: dict = {'type': 'array'}
        if args:
            schema['items'] = _type_to_schema_dict(args[0])
        return schema

    if origin is dict:
        if args and len(args) == 2:
            return {'type': 'object', 'additionalProperties': _type_to_schema_dict(args[1])}
        return {'type': 'object'}

    if origin is Union:
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return _type_to_schema_dict(non_none[0])
        return {'type': 'string'}  # complex union — can't express precisely, but not None either

    # A single-item list literal used AS an example schema:
    # [str] means "array of strings", [{...}] means "array of objects of this shape".
    # This used to fall through every branch above (it's a list INSTANCE, not
    # a list[X] generic alias, and not a dict) all the way to return None,
    # after which the parent set properties[key] = {'type': 'string'} — i.e.
    # a key meant to be an array was requested from the API as a string.
    if isinstance(type_value, list):
        if not type_value:
            return {'type': 'array'}
        return {'type': 'array', 'items': _type_to_schema_dict(type_value[0])}

    if isinstance(type_value, dict) and type_value:
        properties = {k: _type_to_schema_dict(v) for k, v in type_value.items()}
        return {
            'type': 'object',
            'properties': properties,
            'required': sorted(type_value.keys()),
            'additionalProperties': False,
        }

    return {'type': 'string'}  # unknown nested type — conservative fallback


def python_type_to_json_schema_dict(type_value: Any) -> dict | None:
    """
    Converts a Python type / typing hint / dict-schema to a real JSON Schema dict
    suitable for passing as response_format to the OpenAI-compatible API.

    Returns None for plain str (no schema needed — model returns free text).
    Top-level str/'date' -> None: means "free text, no JSON Schema" — which
    is exactly right for simple text questions via Q(prompt) with no second
    argument. Everything else is delegated to _type_to_schema_dict, where str
    inside a container (list[str], a TypedDict field, a dict-literal schema)
    correctly becomes {'type': 'string'} rather than the same None sentinel.

    Examples:
        int                    → {"type": "integer"}
        list[str]              → {"type": "array", "items": {"type": "string"}}
        [str]                  → {"type": "array", "items": {"type": "string"}}
        [{'a': str}]            → {"type": "array", "items": {"type": "object", ...}}
        dict(age=int, city=str)→ {"type": "object",
                                   "properties": {"age": {"type": "integer"},
                                                  "city": {"type": "string"}}}
        SomeTypedDict           → {"type": "object", "properties": {...}, ...}
    """
    if type_value is str or type_value == 'date':
        return None  # free-text answer — no schema
    return _type_to_schema_dict(type_value)


def _type_key(type_value: Any) -> str:
    """
    Stable, deterministic cache-key component for type_value.

    Using repr(type_value) directly is unreliable for dict-literal schemas:
    repr({'a': str, 'b': int}) and repr({'b': int, 'a': str}) differ even
    though the two describe the exact same type, which would cause spurious
    cache misses whenever a schema happens to be rebuilt with fields in a
    different order. It is also blind to TypedDict schema evolution: the
    repr of a TypedDict class is just its qualified name (e.g.
    "<class 'bible_core._SCHEMA'>"), unaffected by adding/removing fields —
    so an old cache entry from before a schema change would keep matching
    the new schema's cache key and be served as if still valid.

    _type_key sidesteps both problems by hashing the canonical JSON Schema
    (via _type_to_schema_dict, keys sorted) instead of the raw type object:
    field order stops mattering, and any actual change to the schema's
    shape changes the resulting string.
    """
    return json.dumps(_type_to_schema_dict(type_value), sort_keys=True, default=str)


def _validate_against_type(value: Any, type_value: Any) -> None:
    """
    Recursively checks that value matches the shape of type_value (the same
    representation accepted by python_type_to_json_schema_dict: builtin types,
    list[X], dict[K,V], Union/Optional, TypedDict, dict-literal {'field': type},
    list-literal [X] "array of this shape"). Raises ValueError with the exact
    path to the first mismatch found.

    Why this is needed on top of response_format: a provider without strict
    schema enforcement (strict=False — true for everyone except gpt-/o1/o3/o4,
    see _call_llm) can return syntactically valid JSON of the wrong shape, e.g.
    {"scenes": "some text describing the scenes"} instead of {"scenes": [...]}.
    json.loads() lets that through without a single error — the JSON parser
    doesn't care about the type, but the calling code does. Q() only caches
    content AFTER this check passes (see below), so a malformed response never
    sits in the cache forever under the same (type_value, prompt) key — which
    would otherwise make a retry from the caller's own retry loop just read
    the same cache entry and never reach the LLM again.
    """
    def _check(v: Any, t: Any, path: str) -> None:
        if t is str or t == 'date':
            if not isinstance(v, str):
                raise ValueError(f"{path}: expected string, got {type(v).__name__}")
            return
        if isinstance(t, type) and t in (int, float, bool):
            if not isinstance(v, t):
                raise ValueError(f"{path}: expected {t.__name__}, got {type(v).__name__}")
            return
        if isinstance(t, type) and is_typeddict(t):
            if not isinstance(v, dict):
                raise ValueError(f"{path}: expected object (TypedDict {t.__name__}), got {type(v).__name__}")
            hints = get_type_hints(t)
            required_keys = getattr(t, '__required_keys__', frozenset(hints.keys()))
            for k in required_keys:
                if k not in v:
                    raise ValueError(f"{path}: missing required field {k!r}")
            for k, sub_t in hints.items():
                if k in v:
                    _check(v[k], sub_t, f"{path}.{k}")
            return

        origin = get_origin(t)
        args   = get_args(t)

        if origin is list:
            if not isinstance(v, list):
                raise ValueError(f"{path}: expected list, got {type(v).__name__}")
            if args:
                for i, item in enumerate(v):
                    _check(item, args[0], f"{path}[{i}]")
            return

        if origin is dict:
            if not isinstance(v, dict):
                raise ValueError(f"{path}: expected dict, got {type(v).__name__}")
            if args and len(args) == 2:
                for k, val in v.items():
                    _check(val, args[1], f"{path}.{k}")
            return

        if origin is Union:
            non_none = [a for a in args if a is not type(None)]
            if v is None and type(None) in args:
                return
            if len(non_none) == 1:
                _check(v, non_none[0], path)
                return
            return  # complex union — not strictly checked

        if isinstance(t, list):
            if not isinstance(v, list):
                raise ValueError(f"{path}: expected list (array), got {type(v).__name__}")
            if t:
                for i, item in enumerate(v):
                    _check(item, t[0], f"{path}[{i}]")
            return

        if isinstance(t, dict) and t:
            if not isinstance(v, dict):
                raise ValueError(f"{path}: expected object, got {type(v).__name__}")
            for k, sub_t in t.items():
                if k not in v:
                    raise ValueError(f"{path}: missing required field {k!r}")
                _check(v[k], sub_t, f"{path}.{k}")
            return
        # unknown type descriptor — not strictly checked

    _check(value, type_value, "$")


# ---------------------------------------------------------------------------
# Strip non-standard comments from JSON
# ---------------------------------------------------------------------------

def remove_json_comments(json_str: str) -> str:
    """
    Removes // and /* */ comments from a JSON string returned by the LLM.

    This is a string-aware scanner, not a plain regex substitution: a naive
    r'//.*' regex would also strip everything after // found INSIDE a string
    value, e.g. {"website": "https://example.com/page"} would be mangled
    into {"website": "https: — breaking otherwise perfectly valid JSON that
    simply happens to contain a URL. This scanner tracks whether it is
    currently inside a string literal (respecting \\" escapes) and only
    treats // or /* as a comment start when outside of one.
    """
    result: list[str] = []
    i = 0
    n = len(json_str)
    in_string = False
    while i < n:
        c = json_str[i]
        if in_string:
            result.append(c)
            if c == '\\' and i + 1 < n:
                # Escaped character — copy the next char verbatim too, so an
                # escaped quote \" does not end the string early.
                result.append(json_str[i + 1])
                i += 2
                continue
            if c == '"':
                in_string = False
            i += 1
            continue

        if c == '"':
            in_string = True
            result.append(c)
            i += 1
            continue

        if c == '/' and i + 1 < n and json_str[i + 1] == '/':
            j = json_str.find('\n', i)
            i = j if j != -1 else n
            continue

        if c == '/' and i + 1 < n and json_str[i + 1] == '*':
            j = json_str.find('*/', i + 2)
            i = j + 2 if j != -1 else n
            continue

        result.append(c)
        i += 1
    return ''.join(result)


# Kept as a backward-compatible alias.
remove_comments = remove_json_comments


# ---------------------------------------------------------------------------
# Core: prompt building and LLM invocation
# ---------------------------------------------------------------------------

def _safe_format(prompt: str, format_vars: dict) -> str:
    """
    Substitutes only {key} placeholders whose name is a known key in
    format_vars, leaving any other brace content untouched.

    Unlike str.format_map(), which raises KeyError on ANY unmatched {...}
    and aborts the whole substitution — including otherwise-valid
    placeholders — if the prompt happens to contain literal JSON braces,
    e.g. 'Answer as {"key": "value"}. Name: {name}' would previously fail
    to substitute {name} at all, silently, because format_map choked on
    {"key": "value"} first.
    """
    if not format_vars:
        return prompt
    pattern = re.compile(r'\{(' + '|'.join(re.escape(k) for k in format_vars) + r')\}')
    return pattern.sub(lambda m: str(format_vars[m.group(1)]), prompt)


def _build_prompt(
    prompt: str,
    type_value: Any,
    *,
    extend: bool,
    identity: str,
    format_vars: dict,
) -> str:
    """
    Formats the prompt string and optionally prepends a system prefix.

    Args:
        prompt:      raw prompt string with optional {placeholders}.
        type_value:  expected response type; affects the format instruction.
        extend:      if True, prepend a system prefix with a format instruction.
        identity:    assistant role/persona used in the system prefix.
        format_vars: variables substituted for {key} placeholders whose key
                      name is present in format_vars.

    Returns:
        The fully-built prompt string.

    Note:
        Unlike the original, does NOT read the caller frame via inspect.
        All variables are passed explicitly through **kwargs in Q().
    """
    if format_vars:
        prompt = _safe_format(prompt, format_vars)

    if extend and type_value is not None:
        if type_value == 'date':
            # date has no JSON Schema equivalent — keep the text hint
            prompt = f' Output STRONGLY in format dd/mm/yyyy string. DO NOT OUTPUT ANY COMMENTARY.' + prompt
        elif type_value is not str:
            # Schema is enforced via response_format at the API level.
            # Only tell the model NOT to add commentary; no need to describe the schema.
            prompt = ' DO NOT OUTPUT ANY COMMENTARY. Output only the requested data.' + prompt
        prompt = identity + prompt

    return prompt


async def _call_llm(prompt: str, type_value: Any = str) -> str:
    """
    Invokes the LLM via AsyncOpenAI (OpenAI-compatible endpoint).

    Passes a proper JSON Schema via response_format when type_value is
    not str, so the model is constrained at the API level — no need to
    describe the schema in the prompt text.

    No caching here — see Q(). Caching a raw response before it has been
    parsed and shape-validated would let a malformed answer (syntactically
    valid JSON of the wrong shape, or free text instead of JSON) get stuck
    in the cache under (type_value, prompt) forever, making every future
    call — including retries from the caller's own retry loop — replay the
    same bad answer instead of ever reaching the LLM again.
    """
    if _acompletion is None:
        raise RuntimeError('LLM not initialised — call setup_llmrag() first')

    schema = python_type_to_json_schema_dict(type_value)
    kwargs: dict = dict(
        model=Unishare.llm_model,
        messages=[{'role': 'user', 'content': prompt}],
        temperature=getattr(Unishare, 'llm_temperature', 0.0),
    )
    if schema is not None:
        # Native JSON Schema enforcement — much more reliable than prompt hints
        # strict=True gives 100% schema adherence on OpenAI; Gemini ignores it.
        # Other providers (Groq, Mistral, local) may reject it, so we only
        # enable it when the model string suggests an OpenAI-hosted model.
        _model = Unishare.llm_model or ''
        use_strict = _model.startswith(('gpt-', 'o1', 'o3', 'o4'))
        kwargs['response_format'] = {
            'type': 'json_schema',
            'json_schema': {
                'name': 'response',
                'schema': schema,
                'strict': use_strict,
            },
        }
    if extra := getattr(Unishare, 'llm_extra_body', None):
        kwargs['extra_body'] = extra

    response = await _acompletion(**kwargs)
    return response.choices[0].message.content


def _parse_response(content: str, type_value: Any) -> Any:
    """
    Parses and validates the LLM response content.

    If type_value is str or 'date', returns the string as-is.
    Otherwise attempts JSON parsing, then validates the parsed value against
    type_value via _validate_against_type — catching not just invalid JSON
    but syntactically valid JSON of the wrong shape (e.g. a string where an
    array was expected), which json.loads() alone would not reject.
    """
    # Strip code-block markers that LLMs sometimes include
    cleaned = content.strip().strip('`')
    if cleaned.startswith('json'):
        cleaned = cleaned[4:]

    if type_value in (str, 'date'):
        return cleaned

    clean_json = remove_json_comments(cleaned)
    try:
        parsed = json.loads(clean_json)
    except json.JSONDecodeError:
        raise ValueError(f'Invalid JSON from LLM:\n{cleaned}')

    _validate_against_type(parsed, type_value)
    return parsed


# ---------------------------------------------------------------------------
# Public API: Q() and Qx()
# ---------------------------------------------------------------------------

_DEFAULT_IDENTITY = 'You are an intelligent and extremely smart assistant.'


async def Q(
    str_prompt: str,
    type_value: Any = str,
    blank: bool = True,
    extend: bool = True,
    format: bool = True,  # noqa: A002  (kept for compatibility)
    **format_vars,
) -> Any:
    """
    Args:
        str_prompt:  query string with optional {placeholders}.
        type_value:  expected response type (str, int, list[str], dict, etc.).
                     Used to build the format instruction and validate the result.
        blank:       reserved for compatibility (unused).
        extend:      if True, prepend a system prompt with a format instruction.
        format:      if True and the string contains { }, substitute variables
                     from **format_vars.
        **format_vars:
                     named variables for substitution into str_prompt.
                     Unlike the original, ONLY explicitly passed values;
                     no reading of local variables from the calling frame.

    Example::

        # Explicit variable passing (recommended)
        result = await Q("Capital of {country}?", str, country=country)

        # No substitution
        result = await Q("What is Python?")

        # Structured response
        info = await Q("Details about {name}", dict(age=int, city=str), name=name)

    Note:
        format_vars['identity'] overrides the assistant role when extend=True.
        Caching (Unishare.llm_cache, if configured) happens HERE, after
        _parse_response succeeds — not inside _call_llm. A malformed answer
        (invalid JSON, or valid JSON of the wrong shape) is never cached, so
        the next call — including a retry from the caller's own retry loop —
        reaches the LLM again instead of replaying the same bad answer.
    """
    identity = format_vars.pop('identity', _DEFAULT_IDENTITY)

    effective_format_vars = format_vars if format else {}

    final_prompt = _build_prompt(
        str_prompt,
        type_value,
        extend=extend,
        identity=identity,
        format_vars=effective_format_vars,
    )

    cache: QueryCache | None = Unishare.llm_cache
    cache_key = f'{_type_key(type_value)}:{final_prompt}'

    if cache is not None:
        if (cached := cache.get(cache_key)) is not None:
            try:
                # Normal path: this content already passed _parse_response
                # when it was written (see below), so this is just re-parsing
                # the same valid JSON again.
                return _parse_response(cached, type_value)
            except ValueError:
                # The cache holds an entry that fails validation — e.g. one
                # written BEFORE this fix, back when malformed responses were
                # still cached, or any other corrupted entry. Without this
                # except, a cache reader gets stuck forever: cache.get()
                # always returns the same bad string, Q() always raises the
                # same exception, and the LLM is NEVER called again — even
                # if the caller wrapped Q() in a proper retry loop of its own.
                # Treat this as a cache miss and go to the LLM fresh.
                pass

    content = await _call_llm(final_prompt, type_value)
    result = _parse_response(content, type_value)  # raises on malformed — never reaches cache.set below

    if cache is not None:
        cache.set(cache_key, content)

    return result


async def Qx(str_prompt: str, type_value: Any = str) -> Any:
    """
    Calls the LLM without any formatting or system prompt.
    Useful for raw queries where the prompt is already fully formed.
    """
    return await Q(str_prompt, type_value, format=False, extend=False)


# ---------------------------------------------------------------------------
# LLM provider initialisation
# ---------------------------------------------------------------------------

# Providers that require an environment variable holding the API key.
# litellm looks them up automatically by their standard names.
_PROVIDER_ENV_KEYS: dict[str, str] = {
    'openai':   'OPENAI_API_KEY',
    'groq':     'GROQ_API_KEY',
    'google':   'GOOGLE_API_KEY',
    'gemini':   'GOOGLE_API_KEY',
    'mistral':  'MISTRAL_API_KEY',
    'xai':      'XAI_API_KEY',
}

# Google Gemini: disable all safety filters
_GEMINI_SAFETY_SETTINGS = [
    {'category': 'HARM_CATEGORY_DANGEROUS_CONTENT', 'threshold': 'BLOCK_NONE'},
    {'category': 'HARM_CATEGORY_HARASSMENT', 'threshold': 'BLOCK_NONE'},
    {'category': 'HARM_CATEGORY_HATE_SPEECH', 'threshold': 'BLOCK_NONE'},
    {'category': 'HARM_CATEGORY_SEXUALLY_EXPLICIT', 'threshold': 'BLOCK_NONE'},
]


def setup_llmrag() -> None:
    """
    Initialises the LLM provider from config.llm.

    Reads configuration from the config module (must be on PYTHONPATH or
    in the application's working directory).

    Supported config.llm formats:
        ['host', address]                      - local model, no API key
        ['host', address, api_key_env, model]  - local model with API key
        [provider, model]                      - cloud provider
        [provider, model, address]             - cloud provider with custom base URL

    Supported providers: host, openai, groq, google, gemini, mistral, xai.
    Any OpenAI-compatible endpoint (LM Studio, Ollama) is specified as 'host'.

    After initialisation, Unishare.llm_model holds the litellm model string
    (e.g. 'groq/llama3-8b-8192') and all calls go through litellm.acompletion.
    """
    import config  # module is loaded before config analysis

    if not getattr(config, 'llm', None):
        return

    temperature: float = getattr(config, 'temperature', 0.0)

    # --- Parse config.llm ---
    provider = model = address = api_key_env = ''
    match config.llm:
        case ['host', address]:
            provider = 'host'
        case ['host', address, api_key_env, model]:
            provider = 'host'
        case [p, m, address]:
            provider, model = p, m
        case [p, m]:
            provider, model = p, m
            address = None
        case _:
            logger.error('Invalid config.llm format: %s', config.llm)
            return

    provider = provider.lower()

    # --- Reasoning parameters (e.g. for OpenAI o1/o3 models) ---
    extra_body: dict = {}
    if reasoning := getattr(config, 'reasoning', None):
        extra_body['reasoning'] = {'effort': reasoning, 'enabled': True}

    # --- Provider → AsyncOpenAI client ---
    _PROVIDER_BASE_URL: dict[str, str] = {
        'google': 'https://generativelanguage.googleapis.com/v1beta/openai/',
        'gemini': 'https://generativelanguage.googleapis.com/v1beta/openai/',
        'openai': 'https://api.openai.com/v1/',
        'groq':   'https://api.groq.com/openai/v1/',
        'mistral':'https://api.mistral.ai/v1/',
        'xai':    'https://api.x.ai/v1/',
    }

    if provider == 'host':
        api_key = (os.environ.get(api_key_env) if api_key_env else None) or 'llm-studio'
        base_url = address
        model_id = model or 'local-model'

    elif provider in _PROVIDER_ENV_KEYS:
        api_key = os.environ.get(_PROVIDER_ENV_KEYS[provider], 'no-key')
        base_url = address or _PROVIDER_BASE_URL.get(provider, '')
        model_id = model

        # Note: safety_settings is a native Gemini API parameter and is NOT
        # accepted by the OpenAI-compatible endpoint (/v1beta/openai/).
        # To control safety filters use the native google-genai SDK instead.
    else:
        logger.error('Unknown LLM provider: %s', provider)
        return

    global _acompletion
    _client = _AsyncOpenAI(api_key=api_key, base_url=base_url)
    _acompletion = _client.chat.completions.create

    # Store the model identifier and parameters in Unishare
    Unishare.llm_model = model_id  # plain model name, e.g. 'gemini-2.5-pro-preview'
    Unishare.llm_temperature = temperature
    Unishare.llm_extra_body = extra_body or None

    logger.info('LLM initialised: %s (temperature=%.2f)', model_id, temperature)

    # --- Cache ---
    if hasattr(config, 'llm_cache'):
        ttl: int | None = getattr(config, 'llm_cache_ttl', None)
        Unishare.llm_cache = QueryCache(config.llm_cache, ttl=ttl)


# ---------------------------------------------------------------------------
# High-level helper: extract a property from context
# ---------------------------------------------------------------------------

async def get_property(
    name: str,
    context: str = '',
    type: Any = str,      # noqa: A002  (kept for compatibility)
    options: list[str] | None = None,
) -> Any | None:
    """
    Extracts a specific property from a text context using the LLM.

    Args:
        name:     property name (e.g. 'Date of birth').
        context:  text context to extract the value from.
        type:     expected type of the result.
        options:  list of allowed values, if applicable.

    Returns:
        The extracted value of the requested type, or None on error.
    """
    # Auto-detect date fields by property name
    effective_type = type
    if type is str and re.search(r'date', name, re.IGNORECASE):
        effective_type = 'date'

    limits = (
        f', which possible options are {",".join(options)},'
        if options
        else ''
    )
    prompt = (
        f'Output ONLY explicit value{limits} based on the context. '
        f'Example: Context: Animal: Byrd. Query: Has beak: True. '
        f'Context: {context}. Query: {name}:'
    )
    try:
        # Explicit variable passing instead of the inspect hack
        return await Q(prompt, effective_type, format=False)
    except Exception as exc:
        Unishare.message_logger(exc)
        return None