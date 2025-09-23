# Copyright © 2024 UNISI Tech. All rights reserved.
from .common import Unishare
from langchain_groq import ChatGroq
from langchain_openai import ChatOpenAI
from langchain_mistralai import ChatMistralAI
from langchain_xai import ChatXAI
from langchain_google_genai import (
    ChatGoogleGenerativeAI,
    HarmBlockThreshold,
    HarmCategory,
)
from datetime import datetime
import os, inspect, re, json
from typing import get_origin, get_args, Any, Union

class QueryCache:
    ITEM_SEPARATOR = "§¶†‡◊•→±"
    ENTRY_SEPARATOR = "€£¥¢≠≈∆√"    
    
    def __init__(self, file_path):
        self.file_path = file_path
        self.cache = {}
        self._load_cache()

    def _load_cache(self):
        if os.path.exists(self.file_path):
            with open(self.file_path, 'r', encoding='utf-8') as f:
                content = f.read()
                entries = content.split(self.ENTRY_SEPARATOR)
                for entry in entries:
                    if not entry.strip():
                        continue
                    parts = entry.split(self.ITEM_SEPARATOR)
                    if len(parts) == 2:
                        query, result = parts
                        self.cache[query] = result

    def get(self, query):
        return self.cache.get(query)

    def set(self, query, result):        
        entry_data = f"{query}{self.ITEM_SEPARATOR}{result}"
        prepend_separator = bool(self.cache)
        
        try:
            with open(self.file_path, 'a', encoding='utf-8') as f:
                if prepend_separator:
                    f.write(self.ENTRY_SEPARATOR)
                f.write(entry_data)
            
            self.cache[query] = result
            return True
        except Exception as e:
            print(f"Error caching result: {e}")
            return False

def jstype(type_value):        
    if isinstance(type_value, type):         
        if type_value == int:
            return 'integer'
        elif type_value == float:
            return 'number'
        elif type_value == bool:
            return 'boolean'
        elif type_value == str:
            return 'string'
        elif type_value == dict:            
            return 'object'
        elif type_value == list:            
            return 'array'
        else:
            origin = get_origin(type_value) 
            args = get_args(type_value)
            if origin == list:
                return f'array of {jstype(args[0])} '
            elif origin == dict:
                return f'object of {jstype(args[0])} to {jstype(args[1])} structure.'
            else:
                return 'string'  
    else: 
        match type_value:
            case str():
                jtype = 'string'
            case int():
                jtype = 'integer'
            case float():
                jtype = 'number'
            case bool():
                jtype = 'boolean'
            case dict():
                if type_value:
                    ptypes = ','.join(f'"{k}": "[Type: {jstype(v)}]"' for k, v in type_value.items())
                    jtype = f'object with {{{ptypes}}} structure'
                else:
                    jtype = 'object'
            case list():  
                jtype = 'array'
            case _:
                jtype = 'string' 
    return jtype

def is_type(variable, expected_type):
    """
    Check if the variable matches the expected type hint.
    """
    # Handle explicit mapping of expected keys to types: {'name': str, 'age': int}
    if isinstance(expected_type, dict):
        if not isinstance(variable, dict):
            return False
        for key_pattern, sub_type in expected_type.items():
            # direct key match
            if key_pattern in variable:
                if not is_type(variable[key_pattern], sub_type):
                    return False
                continue            
        return True

    # Handle typing hints (e.g., List[int], Dict[str,int], Union[..., ...], Optional[T], Tuple[T,...])
    origin = get_origin(expected_type)
    args = get_args(expected_type)

    # No typing origin: expected_type may be a bare type or typing.Any
    if origin is None:
        # typing.Any
        if expected_type is Any:
            return True
        # None as expected type
        if expected_type is None:
            return variable is None
        # expected_type could be a builtin type or a tuple of types
        if isinstance(expected_type, type) or isinstance(expected_type, tuple):
            return isinstance(variable, expected_type)
        # fallback: try isinstance; for other unexpected cases, be permissive
        try:
            return isinstance(variable, expected_type)
        except Exception:
            return False

    # Handle Union / Optional
    if origin is Union:
        return any(is_type(variable, arg) for arg in args)

    # Handle List[...] and Set[...]
    if origin in (list, set):
        expected_container = list if origin is list else set
        if not isinstance(variable, expected_container):
            return False
        if not args:
            return True
        return all(is_type(item, args[0]) for item in variable)

    # Handle Tuple[...] or tuple
    if origin is tuple:
        if not isinstance(variable, tuple):
            return False
        if not args:
            return True
        # Homogeneous tuple: Tuple[T, ...]
        if len(args) == 2 and args[1] is Ellipsis:
            return all(is_type(item, args[0]) for item in variable)
        # Fixed-length tuple: Tuple[T1, T2, ...]
        if len(variable) != len(args):
            return False
        return all(is_type(item, typ) for item, typ in zip(variable, args))

    # Handle Dict[K, V]
    if origin is dict:
        if not isinstance(variable, dict):
            return False
        if not args:
            return True
        key_type, val_type = args
        return all(is_type(k, key_type) and is_type(v, val_type) for k, v in variable.items())

    # Handle Literal[...] (available in typing)
    try:
        from typing import Literal
        if origin is Literal:
            return any(variable == lit for lit in args)
    except Exception:
        pass

    # Fallback: check using isinstance against the origin if possible
    try:
        return isinstance(variable, origin)
    except Exception:
        return False

def remove_comments(json_str):
    # Regular expression to remove single-line comments (// ...)
    json_str = re.sub(r'//.*', '', json_str)
    # Regular expression to remove multi-line comments (/* ... */)
    json_str = re.sub(r'/\*.*?\*/', '', json_str, flags=re.DOTALL)
    return json_str

def Q(str_prompt, type_value = str, blank = True, extend = True, format = True, **format_model):
    """returns LLM async call for a question, `extend = True` adds system prompt, 
     'identity' in format_model can be used to set the assistant identity"""    
    llm = Unishare.llm_model    
    if format and '{' in str_prompt:
        caller_frame = inspect.currentframe().f_back            
        format_model = caller_frame.f_locals | format_model if format_model else caller_frame.f_locals
        str_prompt = str_prompt.format(**format_model) 
    if extend: 
        if type_value is not None:          
            jtype = jstype(type_value)
            format = " dd/mm/yyyy string" if type_value == 'date' else f'a JSON {jtype}' if jtype != 'string' else jtype      
            str_prompt = f" Output STRONGLY in format {format}. DO NOT OUTPUT ANY COMMENTARY." + str_prompt         
        str_prompt = format_model.get('identity', 'You are an intelligent and extremely smart assistant.') + str_prompt
    async def f():            
        if Unishare.llm_cache:            
            if content := Unishare.llm_cache.get(str_prompt):
                pass
            else:
                io = await llm.ainvoke(str_prompt)
                content = io.content
                Unishare.llm_cache.set(str_prompt, content)
        else:
            io = await llm.ainvoke(str_prompt)
            content = io.content
        js = content.strip().strip('`').replace('json', '')                      
        if type_value == str or type_value == 'date':
            return js  
        try:       
            clean_js = remove_comments(js)     
            parsed = json.loads(clean_js)
        except json.JSONDecodeError as e:
            raise ValueError(f'Invalid JSON: {js}, \n Query: {str_prompt}') 
        
        if not is_type(parsed, type_value):
            raise TypeError(f'Invalid type: {type(parsed)} != {type_value}')                    
        return parsed
    return f()

def Qx(str_prompt, type_value = str):
    """returns LLM async call for a question, without formatting or extending the prompt"""
    return Q(str_prompt, type_value, format = False, extend = False)

def setup_llmrag():    
    import config #the module is loaded before config analysis    
    temperature = getattr(config, 'temperature', 0.0)
    if config.llm:
        api_key_config = None
        model = ''
        match config.llm:
            case ['host', address]:                 
                type = 'host' #provider type is openai for local llms
            case ['host', address, api_key_config, model]:                 
                type = 'host' #provider type is openai for local llms
            case [type, model, address]: ...
            case [type, model]: address = None
            case _:
                print(f'Error: Invalid llm configutation: {config.llm}') 
                return
                
        type = type.lower()
        model_kwargs={}
        reasoning = getattr(config, 'reasoning', None)
        if reasoning:
            model_kwargs['reasoning'] = {"effort": reasoning, 'enabled': True}             

        match type:
            case 'host':  
                api_key_from_config = os.environ.get(api_key_config) if api_key_config else None
                api_key = api_key_from_config if api_key_from_config else 'llm-studio'                         
                Unishare.llm_model = ChatOpenAI(
                    api_key = api_key,
                    temperature = temperature,
                    openai_api_base = address,
                    model_kwargs=model_kwargs,
                    model = model
                ) 
            case 'openai':
                
                Unishare.llm_model = ChatOpenAI(temperature = temperature, model_kwargs=model_kwargs)

            case 'xai':
                Unishare.llm_model = ChatXAI(
                    model = model,
                    temperature = temperature,
                    max_tokens = None,
                    timeout = None,
                    max_retries = 2, 
                )

            case 'groq':
                Unishare.llm_model = ChatGroq(
                    model = model,
                    temperature = temperature,
                    max_tokens = None,
                    timeout = None,
                    max_retries = 2, 
                )
            case 'google' | 'gemini':
                Unishare.llm_model = ChatGoogleGenerativeAI(
                    model = model,
                    temperature = temperature,
                    max_tokens = None,
                    timeout = None,
                    max_retries = 2,
                    safety_settings = {
                        HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE
                    }
                )
            case 'mistral':
                Unishare.llm_model = ChatMistralAI(
                    model = model,
                    temperature=0,
                    max_retries=2,
                    # other params...
                )
        
        if hasattr(config, 'llm_cache'):
            Unishare.llm_cache = QueryCache(config.llm_cache)

async def get_property(name, context = '', type = str, options = None):  
    if type == str and re.search(r'date', name, re.IGNORECASE):
        type = 'date'
    limits = f', which possible options are {",".join(opt for opt in options)},' if options else ''                    
    prompt = """Output ONLY explicit value{limits} based on the context. Example: Context: Animal: Byrd. Query: Has beak: True. Context: {context}. Query: {name}:"""    
    try:
        value = await Q(prompt, type)
    except Exception as e:        
        Unishare.message_logger(e)
        return None
    return value
