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
        _BUILTIN_MAP = {
            int: 'integer',
            float: 'number',
            bool: 'boolean',
            str: 'string',
            dict: 'object',
            list: 'array',
        }
        if type_value in _BUILTIN_MAP:
            return _BUILTIN_MAP[type_value]

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
    Внутренний рекурсивный конвертер. В отличие от python_type_to_json_schema_dict,
    str здесь ВСЕГДА становится {'type': 'string'} — сентинел "свободный текст
    без схемы" имеет смысл только на верхнем уровне вызова (см. ниже).
    """
    _BUILTIN: dict[Any, str] = {
        int: 'integer', float: 'number', bool: 'boolean',
        str: 'string',  dict: 'object',  list: 'array',
    }

    if isinstance(type_value, type) and type_value in _BUILTIN:
        return {'type': _BUILTIN[type_value]}

    # TypedDict класс — top-level или вложенный (list[SomeTypedDict], поле
    # другого TypedDict). get_type_hints уже был импортирован для этого,
    # но не использовался: TypedDict-классы не распознавались вообще и
    # тихо превращались в None → {'type': 'string'} у родителя.
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
        return {'type': 'string'}  # сложный union — не выразить точно, но и не None

    # Список-литерал из одного элемента, используемый КАК схема-по-примеру:
    # [str] значит "массив строк", [{...}] значит "массив объектов такой формы".
    # Раньше это не входило ни в одну ветку выше (это list-INSTANCE, не
    # list[X] generic alias, и не dict), и падало в самый низ на return None,
    # откуда родитель делал properties[key] = {'type': 'string'} — то есть
    # ключ, задуманный как массив, требовался API как строка.
    if isinstance(type_value, list):
        if not type_value:
            return {'type': 'array'}
        return {'type': 'array', 'items': _type_to_schema_dict(type_value[0])}

    if isinstance(type_value, dict) and type_value:
        properties = {k: _type_to_schema_dict(v) for k, v in type_value.items()}
        return {
            'type': 'object',
            'properties': properties,
            'required': list(type_value.keys()),
            'additionalProperties': False,
        }

    return {'type': 'string'}  # неизвестный вложенный тип — консервативный fallback


def python_type_to_json_schema_dict(type_value: Any) -> dict | None:
    """
    Converts a Python type / typing hint / dict-schema to a real JSON Schema dict
    suitable for passing as response_format to the OpenAI-compatible API.

    Returns None for plain str (no schema needed — model returns free text).
    Верхнеуровневый str/'date' -> None: значит "свободный текст без JSON
    Schema" — так и должно быть для простых текстовых вопросов через Q(prompt)
    без второго аргумента. Всё остальное делегируется в _type_to_schema_dict,
    где str внутри контейнера (list[str], TypedDict-поле, dict-литерал)
    корректно становится {'type': 'string'}, а не тем же самым None.

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


def _validate_against_type(value: Any, type_value: Any) -> None:
    """
    Рекурсивно проверяет, что value соответствует форме type_value (том же
    представлении, которое принимает python_type_to_json_schema_dict: builtin
    типы, list[X], dict[K,V], Union/Optional, TypedDict, dict-литерал
    {'field': type}, list-литерал [X] "массив такой формы"). Бросает ValueError
    с точным путём до несовпадения при первом расхождении.

    Зачем нужен отдельно от response_format: провайдер без строгой схемы
    (strict=False — так у всех, кроме gpt-/o1/o3/o4, см. _call_llm) может
    вернуть синтаксически валидный JSON неправильной формы, например
    {"scenes": "текст со сценами"} вместо {"scenes": [...]}. json.loads()
    такое пропустит без единой ошибки — тип для JSON-парсера не важен, а вот
    вызывающему коду важен. Q() кеширует контент ТОЛЬКО после этой проверки
    (см. ниже), чтобы малоформатный ответ не осел в кеше навсегда под тем же
    (type_value, prompt) — тогда как повторный вызов Q() из retry-цикла
    вызывающего кода будет просто читать тот же кеш и никогда не дойдёт
    до нового обращения к LLM.
    """
    def _check(v: Any, t: Any, path: str) -> None:
        if t is str or t == 'date':
            if not isinstance(v, str):
                raise ValueError(f"{path}: ожидалась строка, получено {type(v).__name__}")
            return
        if isinstance(t, type) and t in (int, float, bool):
            if not isinstance(v, t):
                raise ValueError(f"{path}: ожидался {t.__name__}, получено {type(v).__name__}")
            return
        if isinstance(t, type) and is_typeddict(t):
            if not isinstance(v, dict):
                raise ValueError(f"{path}: ожидался объект (TypedDict {t.__name__}), получено {type(v).__name__}")
            hints = get_type_hints(t)
            required_keys = getattr(t, '__required_keys__', frozenset(hints.keys()))
            for k in required_keys:
                if k not in v:
                    raise ValueError(f"{path}: отсутствует обязательное поле {k!r}")
            for k, sub_t in hints.items():
                if k in v:
                    _check(v[k], sub_t, f"{path}.{k}")
            return

        origin = get_origin(t)
        args   = get_args(t)

        if origin is list:
            if not isinstance(v, list):
                raise ValueError(f"{path}: ожидался list, получено {type(v).__name__}")
            if args:
                for i, item in enumerate(v):
                    _check(item, args[0], f"{path}[{i}]")
            return

        if origin is dict:
            if not isinstance(v, dict):
                raise ValueError(f"{path}: ожидался dict, получено {type(v).__name__}")
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
            return  # сложный union — не проверяем строго

        if isinstance(t, list):
            if not isinstance(v, list):
                raise ValueError(f"{path}: ожидался list (array), получено {type(v).__name__}")
            if t:
                for i, item in enumerate(v):
                    _check(item, t[0], f"{path}[{i}]")
            return

        if isinstance(t, dict) and t:
            if not isinstance(v, dict):
                raise ValueError(f"{path}: ожидался object, получено {type(v).__name__}")
            for k, sub_t in t.items():
                if k not in v:
                    raise ValueError(f"{path}: отсутствует обязательное поле {k!r}")
                _check(v[k], sub_t, f"{path}.{k}")
            return
        # неизвестный тип-описатель — не проверяем строго

    _check(value, type_value, "$")


# ---------------------------------------------------------------------------
# Strip non-standard comments from JSON
# ---------------------------------------------------------------------------

_RE_SINGLE_COMMENT = re.compile(r'//.*')
_RE_MULTI_COMMENT = re.compile(r'/\*.*?\*/', re.DOTALL)


def remove_json_comments(json_str: str) -> str:
    """Removes // and /* */ comments from a JSON string returned by the LLM."""
    json_str = _RE_SINGLE_COMMENT.sub('', json_str)
    return _RE_MULTI_COMMENT.sub('', json_str)


# Kept as a backward-compatible alias.
remove_comments = remove_json_comments


# ---------------------------------------------------------------------------
# Core: prompt building and LLM invocation
# ---------------------------------------------------------------------------

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
        format_vars: variables for str.format_map().

    Returns:
        The fully-built prompt string.

    Note:
        Unlike the original, does NOT read the caller frame via inspect.
        All variables are passed explicitly through **kwargs in Q().
    """
    if '{' in prompt and format_vars:
        try:
            prompt = prompt.format_map(format_vars)
        except KeyError as e:
            logger.warning("Q(): missing format variable %s", e)

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
    cache_key = f'{type_value!r}:{final_prompt}'

    if cache is not None:
        if (cached := cache.get(cache_key)) is not None:
            try:
                # Обычный путь: контент уже прошёл _parse_response при записи
                # (см. ниже), здесь просто повторный парсинг того же валидного JSON.
                return _parse_response(cached, type_value)
            except ValueError:
                # Кеш содержит запись, не проходящую валидацию — например,
                # записанную ДО этого фикса, когда malformed-ответы ещё
                # кешировались, или любую другую испорченную запись. Без этого
                # except читатель кеша навечно застревает: cache.get() всегда
                # возвращает ту же битую строку, Q() всегда бросает то же
                # исключение, и обращение к LLM НИКОГДА не происходит — даже
                # если вызывающий код честно организовал retry вокруг Q().
                # Трактуем это как промах кеша и идём в LLM заново.
                pass

    content = await _call_llm(final_prompt, type_value)
    result = _parse_response(content, type_value)  # бросает исключение на malformed — до cache.set не дойдёт

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