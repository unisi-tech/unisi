# Copyright © 2024 UNISI Tech. All rights reserved.
import config, os, json, asyncio
from .utils import *
from .units import * 
from .containers import Block, Dialog
from .users import User
from .common import *
from .jsoncomparison import Compare, NO_DIFF

def obj2json(obj):
    return json.loads(toJson(obj))

if not os.path.exists(config.upload_dir):
    os.makedirs(config.upload_dir)

comparator = Compare(rules = {'toolbar': '*'}).check

class Recorder:
    def __init__(self):
        self.start(None)
        
    def accept(self, msg, response):  
        if not self.ignored_1message: 
           self.ignored_1message = True   
        else:           
            self.record_buffer.append(f"{toJson(msg)},\
              \n{'null' if response is None else toJson(response)}\n")
        
    def stop_recording(self, _, x):    
        button.spinner = None
        button.changed = button_clicked
        button.tooltip = 'Create autotest'
        full = len(self.record_buffer) > 1
        if full:             
            with open(self.record_file, mode='w') as file:    
                content = ',\n'.join(self.record_buffer)
                file.write(f"[\n{content}]")
        test_name = self.record_file
        self.record_file = None
        
        return Info(f'Test {test_name} is created.', button) if full else\
            Warning('Nothing to save!',button)

    def start(self,fname):
        self.record_file = fname        
        self.record_buffer = []
        if fname:
            self.ignored_1message = True
            module = User.last_user.screen_module
            self.accept(ArgObject(block = 'root', element = None,
                event = 'changed', value = module.name), module.screen)
            self.ignored_1message = False

recorder = Recorder()

def test(filename, user):
    filepath = f'{testdir}{divpath}{filename}'
    file = open(filepath, "r") 
    data = json.loads(file.read())    
    error = False
    for i in range(0, len(data), 2):
        message = data[i]
        expected = data[i + 1]
            
        result = asyncio.run(user.result4message(ReceivedMessage(message)))
        responce = user.prepare_result(result)
        jresponce = obj2json(responce)
        
        diff = comparator(expected, jresponce)
        if diff != NO_DIFF:
            print(f"\nTest {filename} is failed on message {message}:")            
            for key, obj in diff.items():                                             
                if key != '#name':
                    while True:
                        err = obj.get('_message')
                        if err:
                            print(f"  {err} \n")
                            break
                        else: 
                            content = obj.get('_content')
                            if content and len(obj) == 1:
                                obj = content
                            else:
                                for key, subobj in obj.items():
                                    if key != '_content': 
                                        if isinstance(key, str):  
                                            name = obj.get('#name', '')                                                                             
                                            if name:
                                                key = f'  {name}: {key}'
                                            print(f"  {key}")
                                        obj = subobj
                                        break                                                                
            error = True
    return not error

test_name = Edit('Name test file', '', focus = True)
rewrite = Switch('Overwrite existing', False, type = 'check')

def button_clicked(_,__):
    if not os.path.exists(testdir):
        os.makedirs(testdir)        
    test_name.value = User.last_user.screen.name
    test_name.complete = smart_complete(os.listdir(testdir))
    return Dialog('Create autotest..', ask_create_test, test_name, rewrite)

def create_test(fname):
    fname = f'{testdir}{divpath}{fname}'    
    if os.path.exists(fname) and not rewrite.value:
        return Warning(f'Test file {fname} already exists!')              
    
    button.spinner = True   
    button.tooltip = 'Stop test recording'
    button.changed = recorder.stop_recording
    recorder.start(fname)
    
    return Info('Test is recording.. press the same button to stop',button)     

def ask_create_test(_, bname):
    if bname == 'Ok':            
        return create_test(test_name.value) if test_name.value else\
            Warning('Test file name is not defined!')
    
button = Button('_Add test', button_clicked, right = True,
        icon='format_list_bulleted_add', tooltip='Create autotest')

if config.autotest:            
    User.toolbar.append(button)

def check_block(block, hash_elements):
    errors = []
    child_names = set()   
    
    if not hasattr(block, 'name') or not block.name:            
        errors.append(f"The block with {[str(type(gui)).split('.')[-1] for gui in flatten(block.value)]} does not contain name!")
        block.name = 'Unknown'          
    if not isinstance(block.name, str):
        errors.append(f"The block with name {block.name} is not a string!")
    for child in flatten(block.value):  
        hash_element = hash(child)
        if hash_element in hash_elements:
            errors.append(f'The block {block.name} contains already used "{child.name}" in block "{hash_elements[hash_element]}"!')
        else: 
            hash_elements[hash_element] = block.name         
            if not isinstance(child, Unit) or not child:
                errors.append(f'The block {block.name} contains invalid element {child} instead of Unit+ object!') 
            elif isinstance(child, Block):
                errors.append(f'The block {block.name} contains block {child.name}. Blocks cannot contain blocks!')                                                                                                       
            elif child.name in child_names and child.type != 'line':                        
                errors.append(f'The block {block.name} contains a duplicated element name "{child.name}"!')
            elif child.type == 'chart' and not hasattr(child, 'view'):
                errors.append(f'The block {block.name} contains a chart type "{child.name}", but not "view" option!')
            else:
                child_names.add(child.name)                
    return errors

def check_module(module):
    screen = module.screen
    errors =  []        
    block_names = set()      
    hash_elements = {}  
    if not hasattr(screen, 'name') or not screen.name:            
        errors.append(f"Screen file {module.__file__} does not contain name!")
        screen.name = 'Unknown'
    elif not isinstance(screen.name, str):
        errors.append(f"name' variable in screen file {module.__file__} {screen.name} is not a string!")
    if not isinstance(screen.blocks, list):
        errors.append(f"Screen file {module.__file__} 'blocks' is not a list!")
    else:
        toolbar = 'toolbar'
        block_names.add(toolbar)
        if screen.toolbar:
            errors += check_block(Block(toolbar, *screen.toolbar), hash_elements)
        for bl in flatten(screen.blocks):            
            if not isinstance(bl, Block):
                errors.append(f'The screen contains invalid element {bl} instead of Block object!')                                                    
            elif bl.name in block_names:
                errors.append(f'The screen contains a duplicated block name {bl.name}!')    
            else:            
                block_names.add(bl.name)
            errors += check_block(bl, hash_elements)
    if errors:
        errors.insert(0, f"\nErrors in screen {screen.name}, file name {module.__file__}:")
    return errors
    
def run_tests(user):
    errors = []
    for module in user.screens:
        errors += check_module(module)
    if errors:
        errors.insert(0, f'\n!!----Detected errors in screens:')
        print('\n'.join(errors), '\n')
    elif user.screens:
        print(f'\n----The screen definitions are correct.-----\n')
        
    files = config.autotest
    ok = True
    process = False
    if os.path.exists(testdir):
        for file in os.listdir(testdir):
            if not os.path.isdir(file) and (files == '*' or file in files):
                process = True
                if not test(file,user):
                    ok = False
    if process and ok:
        print('\n-----Autotests successfully passed.-----\n')  
    
    
        

            