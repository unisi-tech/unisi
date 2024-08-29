import asyncio
from common import ArgObject, references

from langchain_groq import ChatGroq
from langchain_openai import ChatOpenAI

llm_model = None
#mixtral-8x7b-32768
def setup_llmrag():    
    #import config #the module is loaded before config.py
    config = ArgObject(llm = ('local', "http://localhost:1234/v1"))
    if hasattr(config, 'llm'):
        match config.llm:
            case ['local', address]: 
                model = None
                type = 'openai' #provider type is openai for local llms
            case [type, model, address]: ...
            case [type, model]: address = None
            case _:
                print(f'Error: Invalid llm configutation: {config.llm}') 
                return
        
        global llm_model
        type = type.lower()
        if type == 'openai':            
            llm_model = ChatOpenAI(
                api_key = 'llm-studio',
                temperature = 0.0,
                openai_api_base = address
            ) if address else ChatOpenAI(temperature=0.0)

        elif type == 'groq':
            llm_model = ChatGroq(
                model = model,
                temperature = 0.0,
                max_tokens = None,
                timeout = None,
                max_retries = 2, 
            )
setup_llmrag()

numeric_types = ['number', 'int', 'float', 'double']

async def get_property(name, json_context = '', type = 'string', options = None, attempts = 1, messages = None):
    if messages is None:
        limits = f'type is {type}'
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
    ai_msg =  await llm_model.ainvoke(messages)
    value = ai_msg.content
    if type in numeric_types:
        value = float(value)

    if options and value not in options:
        attempts -= 1
        if attempts > 0:
            value = get_property(name, json_context, type, options, attempts, messages)
        else:
            references.message_logger(f'Invalid value {value} from llm-rag for {messages[1][1]}')
    return value

if __name__ == "__main__":
    async def main():
        data = await get_property('Date of birth', dict(Name = 'Michael Jackson'))
        print(data)
    asyncio.run(main())
