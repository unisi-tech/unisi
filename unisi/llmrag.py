# Copyright © 2024 UNISI Tech. All rights reserved.
from .common import Unishare
from langchain_groq import ChatGroq
from langchain_openai import ChatOpenAI
from langchain_google_genai import (
    ChatGoogleGenerativeAI,
    HarmBlockThreshold,
    HarmCategory,
)
from functools import lru_cache
from pydantic import RootModel, create_model, BaseModel
from datetime import datetime
import collections, inspect, re

def is_standard_type(obj):    
    return isinstance(obj, (collections.abc.Sequence, collections.abc.Mapping, 
        int, float, complex, bool, str, bytes, bytearray, range))
        
def Model(name, type_value):
    """type_value can be simple Python type or dict of {name: type}"""
    model = {}
    if isinstance(type_value, dict):
        for k, v in type_value.items():
            vtype = is_standard_type(v)
            if vtype:
                model[k] = (v, ...)
            else:
                model[k] = (vtype, v)
        return create_model(name, **model) if model else RootModel[str]
    return RootModel[type_value] 

class Question:
    index = 0
    """contains question, format of answer"""
    def __init__(self, question, type_value = None, **format_model):
        self.question = question        
        self.format = Model(f'Question {Question.index}', type_value)
        Question.index += 1
        
    def __str__(self):
        return f'Qustion: {self.question} \n Format: {self.format}'     

    @lru_cache(maxsize = None) 
    def get(question, type_value):
        return Question(question, type_value)
        
def Q(question, type_value = str,  **format_model):
    """returns LLM async call for a question"""
    q = Question.get(question, type_value)        
    llm = Unishare.llm_model
    str_prompt = q.question
    if '{' in str_prompt:
        caller_frame = inspect.currentframe().f_back            
        format_model = caller_frame.f_locals | format_model if format_model else caller_frame.f_locals
        str_prompt = str_prompt.format(**format_model) 
    if not re.search(r'json', str_prompt, re.IGNORECASE):           
        if type_value == int or type_value == float:                    
            jtype = 'number'
        elif type_value == bool:
            jtype = 'boolean'
        elif type_value == dict:
            jtype = 'object'
        elif type_value == list:
            jtype = 'array'
        else:
            jtype = 'string'        
        format = " dd/mm/yyyy string" if type_value == 'date' else f'a JSON {jtype}' if jtype != 'string' else jtype      
        str_prompt = f"System: You are an intelligent and extremely smart assistant. Output STRONGLY in {format} format." + str_prompt 
    async def f():            
        io = await llm.ainvoke(str_prompt)
        js = io.content.strip('`').replace('json', '')                 
        return js if type_value == str or type_value == 'date' else q.format.parse_raw(js).root
    return f()

def setup_llmrag():    
    import config #the module is loaded before config analisis    
    temperature = getattr(config, 'temperature', 0.0)
    if config.llm:
        match config.llm:
            case ['host', address]: 
                model = None
                type = 'host' #provider type is openai for local llms
            case [type, model, address]: ...
            case [type, model]: address = None
            case _:
                print(f'Error: Invalid llm configutation: {config.llm}') 
                return
                
        type = type.lower()
        match type:
            case 'host':            
                Unishare.llm_model = ChatOpenAI(
                    api_key = 'llm-studio',
                    temperature = temperature,
                    openai_api_base = address
                ) 
            case 'openai':
                Unishare.llm_model = ChatOpenAI(temperature=0.0)

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

async def get_property(name, context = '', type = str, options = None):  
    if type == str and re.search(r'date', name, re.IGNORECASE):
        type = 'date'
    limits = f'{limits}, which possible options are {",".join(opt for opt in options)}' if options else ''                    
    prompt = """Human: {context} . Output ONLY "{name}" explicit value {limits} based on the context. """    
    try:
        value = await Q(prompt, type)
    except Exception as e:        
        Unishare.message_logger(e)
        return None
    return value
    