from unisi import *
name = 'Main'

table = Table('Persons', headers = ['Name', 'Date of birth','Occupation'], 
                llm = {'Date of birth': 'Name', 'Occupation': True},
    rows = [['Michael Jackson', None, None], ['Ronald Reagan', None, None]])

block1 = Block('Relations',Button('Calculate the selected', table.emit), table)

ename = Edit('Name')

ebirth = Edit('Date of birth', llm = True)

block2 = Block('Enter any name', [ename, Button('Calculate birth date', ebirth.emit)], [ebirth, Edit('Occupation', llm = ename)]) 

ecountry = Edit('Country', "France")

ecountry_info = Edit('Country info')

qcapitals =  "What are the capitals of the European countries?"

tcapitals = Table('Europe', headers = ['Country','Capital'], tools = False)

async def geo_code():    
    country = ecountry.value.strip()
    if country:
        await user.progress(f'Information about {country}...')
        country_info = await Q("Provide information about {country}.", dict(capital = str, population = int, currency = str))
        ecountry_info.value = str(country_info)
    await user.progress(f'Information about Europe...')
    country2capital = await Q(qcapitals, dict[str, str])        
    tcapitals.rows = [[country, capital] for country, capital in country2capital.items()]
        
query = "Create a user profile for {name} with fields: name, age, occupation."
async def create_user_profile(name = "John Doe"):
        user_info = await Q(query, name=name) #format for external defined query 
        print("User profile:", user_info)
text = "Alpha Centauri A, also known as Rigil Kentaurus, is the principal member, or primary,\
 of the binary system. It is a solar-like main-sequence star with a similar yellowish colour, whose stellar \
 classification is spectral type G2-V; it is about 10% more massive than the Sun, with a radius about 22% larger.",
async def extract_info():
    key_points = await Q("Extract key points from the following text: {text}. ", list[str], text=text)
    print("Key points:", key_points)

async def parallel_execution():
    topic = 'Managing personal notes' 
    questions = await Q("Suggest 3 questions about {topic}.", list[str])
    Qs = [Q(question, str) for question in questions]
    results = await asyncio.gather(*Qs) 
    for question, result in zip(questions, results):
        print(question, result)

async def test(*_):
    await user.progress('Calculating...')
    await geo_code()
    return
    await create_user_profile()
    await extract_info()
    await parallel_execution()
    purpose = 'Managing personal notes' 
    data_types = await Q(purpose_types, list[str])
    print('Types: ', data_types)
    arr = []
    for data_type in data_types:
        arr.append( Q(object_properties, dict, data_type=data_type))
    res = await asyncio.gather(*arr)
    for data_type in zip(data_types, res): 
        print(data_type)

button = Button("Run", test)
tblock = Block("Calculations", [button, ecountry], ecountry_info, tcapitals, width=400)

blocks = [[block2, block1], tblock]