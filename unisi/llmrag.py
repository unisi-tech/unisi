# Copyright © 2024 UNISI Tech. All rights reserved.
from __future__ import annotations

import json, logging, os, re
from typing import Any, Literal, Union, get_args, get_origin

logger = logging.getLogger(__name__)
logging.getLogger("LiteLLM").setLevel(logging.ERROR)

import litellm, diskcache
from litellm import acompletion

from .common import Unishare


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


def is_type(variable: Any, expected_type: Any) -> bool:
    """
    Checks whether a variable matches the expected type or typing hint.

    Supports: bare types, Union/Optional, List[T], Set[T],
    Dict[K,V], Tuple, Literal, Any, and dict schemas like {'field': type}.
    """
    # Explicit dict schema: {'name': str, 'age': int}
    if isinstance(expected_type, dict):
        if not isinstance(variable, dict):
            return False
        for key, sub_type in expected_type.items():
            if key in variable and not is_type(variable[key], sub_type):
                return False
        return True

    origin = get_origin(expected_type)
    args = get_args(expected_type)

    if origin is None:
        if expected_type is Any:
            return True
        if expected_type is None:
            return variable is None
        if isinstance(expected_type, (type, tuple)):
            return isinstance(variable, expected_type)
        try:
            return isinstance(variable, expected_type)
        except TypeError:
            return False

    if origin is Union:
        return any(is_type(variable, arg) for arg in args)

    if origin in (list, set):
        container = list if origin is list else set
        if not isinstance(variable, container):
            return False
        if not args:
            return True
        return all(is_type(item, args[0]) for item in variable)

    if origin is tuple:
        if not isinstance(variable, tuple):
            return False
        if not args:
            return True
        if len(args) == 2 and args[1] is Ellipsis:
            return all(is_type(item, args[0]) for item in variable)
        return len(variable) == len(args) and all(
            is_type(item, t) for item, t in zip(variable, args)
        )

    if origin is dict:
        if not isinstance(variable, dict):
            return False
        if not args:
            return True
        key_type, val_type = args
        return all(
            is_type(k, key_type) and is_type(v, val_type)
            for k, v in variable.items()
        )

    if origin is Literal:
        return any(variable == lit for lit in args)

    try:
        return isinstance(variable, origin)
    except TypeError:
        return False


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
        jschema = python_type_to_json_schema(type_value)
        if type_value == 'date':
            fmt_instruction = ' dd/mm/yyyy string'
        elif jschema != 'string':
            fmt_instruction = f'a JSON {jschema}'
        else:
            fmt_instruction = jschema

        prompt = (
            f" Output STRONGLY in format {fmt_instruction}."
            f" DO NOT OUTPUT ANY COMMENTARY."
            + prompt
        )
        prompt = identity + prompt

    return prompt


async def _call_llm(prompt: str) -> str:
    """
    Invokes the LLM via litellm, consulting the cache when available.
    Returns the raw text content of the response.
    """
    cache: QueryCache | None = Unishare.llm_cache

    if cache is not None:
        if (cached := cache.get(prompt)) is not None:
            return cached

    response = await acompletion(
        model=Unishare.llm_model,
        messages=[{'role': 'user', 'content': prompt}],
    )
    content: str = response.choices[0].message.content
    if cache is not None:
        cache.set(prompt, content)
    return content


def _parse_response(content: str, type_value: Any) -> Any:
    """
    Parses and validates the LLM response content.

    If type_value is str or 'date', returns the string as-is.
    Otherwise attempts JSON parsing and validates the resulting type.
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

    if not is_type(parsed, type_value):
        raise TypeError(
            f'LLM returned wrong type: got {type(parsed).__name__}, '
            f'expected {type_value}'
        )
    return parsed


# ---------------------------------------------------------------------------
# Public API: Q() and Qx()
# ---------------------------------------------------------------------------

_DEFAULT_IDENTITY = 'You are an intelligent and extremely smart assistant.'


def Q(
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

    async def _execute() -> Any:
        content = await _call_llm(final_prompt)
        return _parse_response(content, type_value)

    return _execute()


def Qx(str_prompt: str, type_value: Any = str) -> Any:
    """
    Calls the LLM without any formatting or system prompt.
    Useful for raw queries where the prompt is already fully formed.
    """
    return Q(str_prompt, type_value, format=False, extend=False)


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

# Model identifiers in litellm format: 'provider/model'
# Documentation: https://docs.litellm.ai/docs/providers
_PROVIDER_PREFIX: dict[str, str] = {
    'openai':   'openai',
    'groq':     'groq',
    'google':   'gemini',
    'gemini':   'gemini',
    'mistral':  'mistral',
    'xai':      'xai',
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

    # --- litellm configuration ---
    litellm.set_verbose = False        

    if provider == 'host':
        # Local / self-hosted model via an OpenAI-compatible API
        api_key = (
            os.environ.get(api_key_env)
            if api_key_env
            else None
        ) or 'llm-studio'

        litellm.api_base = address
        litellm.api_key = api_key

        # litellm reaches OpenAI-compatible endpoints via the 'openai/' prefix
        model_id = f'openai/{model}' if model else 'openai/local-model'

    elif provider in _PROVIDER_PREFIX:
        prefix = _PROVIDER_PREFIX[provider]
        model_id = f'{prefix}/{model}' if model else prefix

        if address:
            # Custom base_url for a cloud provider
            litellm.api_base = address

        if provider in ('google', 'gemini'):
            # Safety settings for Gemini are passed via extra_body
            extra_body.setdefault('safety_settings', _GEMINI_SAFETY_SETTINGS)

    else:
        logger.error('Unknown LLM provider: %s', provider)
        return

    # Store the model identifier and parameters in Unishare
    Unishare.llm_model = model_id
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