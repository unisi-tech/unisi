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
import collections, inspect

def is_standard_type(obj):    
    return isinstance(obj, (collections.abc.Sequence, collections.abc.Mapping, 
        int, float, complex, bool, str, bytes, bytearray, range))
        
def Model(name, type_value = None, **parameters):
    model = {}
    if type_value is None:
        for k, v in parameters.items():
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
        self.format = Model(f'Question {Question.index}', type_value,  **format_model)
        Question.index += 1
        
    def __str__(self):
        return f'Qustion: {self.question} \n Format: {self.format}'     

    @lru_cache(maxsize=None) 
    def get(question, type_value = None, **format_model):
        return Question(question, type_value, **format_model)
        
def Q(question, type_value = None,  **format_model):
    """returns LLM async call for a question"""
    q = Question.get(question, type_value)        
    llm = Unishare.llm_model
    str_prompt = q.question
    if '{' in str_prompt:
        caller_frame = inspect.currentframe().f_back            
        format_model = caller_frame.f_locals | format_model if format_model else caller_frame.f_locals
        str_prompt = str_prompt.format(**format_model)            
    async def f():            
        io = await llm.ainvoke(str_prompt)
        js = io.content.strip('`')    
        js = js.replace('json', '').replace('\n', '')    
        return q.format.parse_raw(js).root
    return f()

def setup_llmrag():    
    import config #the module is loaded before config.py    
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

numeric_types = ['number', 'int', 'float', 'double']

async def get_property(name, context = '', type = 'string', options = None, attempts = 1, messages = None):
    if messages is None:
        limits = f'type is {type}'
        if type == 'date':
            limits = f'{limits}, use format "dd/mm/yyyy"'
        if options:            
            limits = f'{limits}, and its possible options are {",".join(opt for opt in options)}'        
        messages = [
            (
                "system",
                f"""You are an intelligent and extremely smart assistant."""        
            ),
            ("human",  f"""{context} . Reason and infer {name}, which {limits}. 
                Do not include any additional text or commentary in your answer, just exact the property value.""")
        ]
    ai_msg =  await Unishare.llm_model.ainvoke(messages)
    value = ai_msg.content
    log_error = ''
    if type in numeric_types:
        try:
            value = float(value)
        except:
            log_error = f'Invalid value {value} from llm-rag for {messages[1][1]}'
            return value
    else:
        value = value.strip('""')

    if not log_error and options and value not in options:
        attempts -= 1
        if attempts > 0:
            value = get_property(name, context, type, options, attempts, messages)
        else:
            log_error = f'Invalid value {value} from llm-rag for {messages[1][1]}'

    if log_error:
        Unishare.message_logger(log_error)
    return value