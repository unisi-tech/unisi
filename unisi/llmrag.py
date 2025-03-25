# Copyright © 2024 UNISI Tech. All rights reserved.
from .common import Unishare
from langchain_groq import ChatGroq
from langchain_openai import ChatOpenAI
from langchain_mistralai import ChatMistralAI
from langchain_google_genai import (
    ChatGoogleGenerativeAI,
    HarmBlockThreshold,
    HarmCategory,
)
from datetime import datetime
import os, inspect, re, json
from typing import get_origin, get_args        

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
    origin = get_origin(expected_type) 
    if origin is None:
        return isinstance(variable, expected_type)
    args = get_args(expected_type)
    
    # Check if the type matches the generic type
    if not isinstance(variable, origin):
        return False
        
    if not args:
        return True
        
    if origin is list:
        return all(isinstance(item, args[0]) for item in variable)
    elif origin is dict:
        return all(isinstance(k, args[0]) and isinstance(v, args[1]) for k, v in variable.items())
    
    return False

def remove_comments(json_str):
    # Regular expression to remove single-line comments (// ...)
    json_str = re.sub(r'//.*', '', json_str)
    # Regular expression to remove multi-line comments (/* ... */)
    json_str = re.sub(r'/\*.*?\*/', '', json_str, flags=re.DOTALL)
    return json_str

def Q(str_prompt, type_value = str, blank = True, **format_model):
    """returns LLM async call for a question"""    
    llm = Unishare.llm_model    
    if '{' in str_prompt:
        caller_frame = inspect.currentframe().f_back            
        format_model = caller_frame.f_locals | format_model if format_model else caller_frame.f_locals
        str_prompt = str_prompt.format(**format_model) 
    if not re.search(r'json', str_prompt, re.IGNORECASE):           
        jtype = jstype(type_value)
        format = " dd/mm/yyyy string" if type_value == 'date' else f'a JSON {jtype}' if jtype != 'string' else jtype      
        str_prompt = f"System: You are an intelligent and extremely smart assistant. Output STRONGLY in format {format}. DO NOT OUTPUT ANY COMMENTARY." + str_prompt 
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
        if isinstance(type_value, dict):
            for k, v in type_value.items():
                if k not in parsed:
                    for k2, v2 in parsed.items():
                        if re.fullmatch(k, k2, re.IGNORECASE) is not None:
                            parsed[k] = parsed.pop(k2)
                            break
                    else:
                        if blank:
                            parsed[k] = None
                            continue                        
                        raise KeyError(f'Key {k} not found in {parsed}')
                        
                if not is_type(parsed[k], v):
                    raise TypeError(f'Invalid type for {k}: {type(parsed[k])} != {v}')
        else:
            if not is_type(parsed, type_value):
                raise TypeError(f'Invalid type: {type(parsed)} != {type_value}')                    
        return parsed
    return f()

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
        match type:
            case 'host':  
                api_key_from_config = os.environ.get(api_key_config) if api_key_config else None
                api_key = api_key_from_config if api_key_from_config else 'llm-studio'                         
                Unishare.llm_model = ChatOpenAI(
                    api_key = api_key,
                    temperature = temperature,
                    openai_api_base = address,
                    model = model
                ) 
            case 'openai':
                Unishare.llm_model = ChatOpenAI(temperature = temperature)

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
    