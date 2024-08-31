from .common import Unishare
from langchain_groq import ChatGroq
from langchain_openai import ChatOpenAI

def setup_llmrag():    
    import config #the module is loaded before config.py    
    if config.llm:
        match config.llm:
            case ['host', address]: 
                model = None
                type = 'openai' #provider type is openai for local llms
            case [type, model, address]: ...
            case [type, model]: address = None
            case _:
                print(f'Error: Invalid llm configutation: {config.llm}') 
                return
                
        type = type.lower()
        if type == 'openai':            
            Unishare.llm_model = ChatOpenAI(
                api_key = 'llm-studio',
                temperature = 0.0,
                openai_api_base = address
            ) if address else ChatOpenAI(temperature=0.0)

        elif type == 'groq':
            Unishare.llm_model = ChatGroq(
                model = model,
                temperature = 0.0,
                max_tokens = None,
                timeout = None,
                max_retries = 2, 
            )

numeric_types = ['number', 'int', 'float', 'double']

async def get_property(name, json_context = '', type = 'string', options = None, attempts = 1, messages = None):
    if messages is None:
        limits = f'type is {type}'
        if type == 'date':
            limits = f'{limits}, use format "dd/mm/yyyy"'
        if options:            
            limits = f'{limits}, and its possible options are {",".join(opt for opt in options)}'        
        messages = [
            (
                "system",
                f"""You are an intelligent and extremely concise assistant."""        
            ),
            ("human",  f"""{json_context} Reason and infer the "{name}" value, which {limits}. 
                Do not include any additional text or commentary in your answer, just exact property value.""")
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
            value = get_property(name, json_context, type, options, attempts, messages)
        else:
            log_error = f'Invalid value {value} from llm-rag for {messages[1][1]}'

    if log_error:
        Unishare.message_logger(log_error)
    return value


