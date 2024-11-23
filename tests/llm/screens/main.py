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

tcapitals = Table('Europe', headers = ['Country','Capital'], tools = False)

topic = 'Managing personal notes' 
etopic = Edit('Topic', topic)
extext = TextArea('Main points')
equestions = TextArea(f'Questions for the topic')

async def geo_code():    
    country = ecountry.value.strip()
    if country:
        await user.progress(f'Information about {country}...')
        country_info = await Q("Provide information about {country}.", dict(capital = str, population = int, currency = str))
        ecountry_info.value = str(country_info)
    await user.progress(f'Information about Europe...')
    country2capital = await Q("What are the capitals of the European countries?", dict[str, str])        
    tcapitals.rows = [[country, capital] for country, capital in country2capital.items()]    
        
text = "Alpha Centauri A, also known as Rigil Kentaurus, is the principal member, or primary,\
 of the binary system. It is a solar-like main-sequence star with a similar yellowish colour, whose stellar \
 classification is spectral type G2-V; it is about 10% more massive than the Sun, with a radius about 22% larger.",
async def extract_info(*_):
    key_points = await Q("Extract key points from the following text: {text}. ", list[str], text=text) #and return them as a list
    etext = ''
    for i, key_point in enumerate(key_points):
        etext = f"{etext}{i+1}. {key_point}\n"
    extext.value = etext
    await parallel_execution()
    
async def parallel_execution():        
    questions = await Q("Suggest 3 questions about {topic}.", list[str], topic=topic)
    Qs = [Q(question, str) for question in questions]
    results = await asyncio.gather(*Qs) 
    etext = ''
    i = 1
    for question, result in zip(questions, results):
        etext = f"==={etext}{i}. {question}\n  => {result}\n"
        i += 1
    equestions.value = etext

async def test(*_):
    await user.progress('Calculating...')
    await geo_code()

button = Button("Run", test)
tblock = Block("Geo calculations", [ecountry, button], ecountry_info, tcapitals, width=400)

eblock = Block('Text operations', [etopic, Button('Extract info', extract_info)], extext, equestions)

blocks = [[block2, block1], tblock, eblock]