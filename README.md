# UNISI #
UNified System Interface, GUI and Remote API

### Purpose ###
UNISI technology provides a unified system interface and advanced program functionality, eliminating the need for front-end and most back-end programming. It automates common tasks, as well as unique ones, significantly reducing the necessity for manual programming and effort.

### Provided functionality without programming ###
 - Automatic WEB GUI Client
 - Client-server data synchronization
 - Unified Remote API
 - Automatic configuring
 - Auto logging
 - Multi-user support
 - Hot reloading and updating
 - Integral autotesting
 - Protocol schema auto validation
 - Shared sessions
 - Monitoring and profiling
 - Database interactions
 - LLM-RAG interactions
 - Voice interaction

### Installing ###
```
pip install unisi
```

### Programming ###
UNISI tech provides a unified system interface and advanced program functionality, eliminating the need for front-end and most back-end programming. It automates common tasks by inner services, as well as unique ones, significantly reducing the necessity for manual programming and effort.
This document serves as a comprehensive guide on utilizing Unisi with Python, along with a compact yet highly efficient framework specifically designed for this purpose. Additionally, the library includes the web version of Unisi, providing developers with a comprehensive set of tools and resources for web application development. Supports Python 3.10+.


### High level - Screen ###
The program directory has to contain a screens folder which contains all screens which Unisi has to show.

Screen example tests/blocks/main.py
```
name = "Main"
blocks = [block] 
```
The block example with a table and a selector
```
table = Table('Videos', 0, headers = ['Video', 'Duration',  'Links', 'Mine'],rows = [
    ['opt_sync1_3_0.mp4', '30 seconds',  '@Refer to signal1', True],
    ['opt_sync1_3_0.mp4', '37 seconds',  '@Refer to signal8', False]    
])
#widgets are groped in a block (complex widget)
block = Block('X Block',
    [           
        Button('Clean table', icon = 'swipe'),
        Select('Select', value='All', options=['All','Based','Group'])
    ], table, icon = 'api')
```

| Screen global variables |	Status | Type | Description |
| :---: | :---: | :---: | :---: | 
| name  | Has to be defined | str | Unique screen name |
| blocks | Has to be defined | list |which blocks to show on the screen |
| user   | Always defined, read-only | User+ | Access to User(inherited) class which associated with a current user |
| header | Optional | str | show it instead of app name |
| toolbar | Optional | list | Unit elements to show in the screen toolbar |
| order | Optional | int | order in the program menu |
| icon  | Optional | str | MD icon of screen to show in the screen menu |
| prepare | Optional | def prepare() | Syncronizes Unit/GUI elements one to another and with the program/system data. It is called before screen appearing if defined. |


### Server start ###
tests/template/run.py
```
import unisi
unisi.start('Test app') 
```
UNISI builds the interactive app for the code above.
Connect a browser to localhast:8000 which are by default and will see:

![image](https://github.com/unisi-tech/unisi/assets/1247062/dafebd1f-ae48-4790-9282-dea83d986749)  

### 'The fastest way to create Web applications in Python.' is a free crash course video how to use UNISI ###
   https://www.unisi.tech/learn

### Handling events ###
All handlers are functions which have a signature
```
def handler_x(unit : Unit, value_x) #or
async def handler_x(unit : Unit, value_x)
```
where unit is a Python object the user interacted with and value for the event.

#### UNISI supports synchronous and asynchronous handlers automatically adopting them for using. ####

All Unit objects except Button have a field ‘value’. 
For an edit field the value is a string or number, for a switch or check button the value is boolean, for table is row id or index, e.t.c.
When a user changes the value of the Unit object or presses Button, the server calls the ‘changed’ function handler.

```
def clean_table(_, value):
    table.rows = []
    
clean_button = Button('Clean the table’, clean_table)
```

| Handler returns |	Description |
| :---: | :---: | 
| None | Automatically update, Ok |
| Error(...), Warning(...), Info(...) | Show to user info about a state. |
| Dialog(..) | Open a dialog with parameters |

Unisi synchronizes units on frontend-end automatically after calling a handler.

If a Unit object doesn't have 'changed' handler the object accepts incoming value automatically to the 'value' variable of a unit.

If 'value' is not acceptable instead of returning an object possible to return Error or Warning or Info. That functions can update a object list passed after the message argument.

```
def changed(elem, value):
   if value == 4:       
       return Error(f‘The value can not be 4!', elem) 
    #accept value othewise
    elem.accept(value)

edit = Edit('Involving', 0.6, changed)
```

#### Events interception of shared blocks ####
Interception handlers have the same in/out format as usual handlers.
#### They are called before the inner element handler call. They cancel the call of inner element handler but you can call it as shown below.
For example above interception of select_mode changed event will be:
```
@handle(select_mode, 'changed')
def do_not_select_mode_x(selector, value):
    if value == 'Mode X':
        return Error('Do not select Mode X in this context', selector) # send old value for update select_mode to the previous state
    return selector.accept(value) #otherwise accept the value
```

### Block details ###
The width and height of blocks is calculated automatically depending on their children. It is possible to set the block width, or make it scrollable , for example for images list. Possible to add MD icon to the header, if required. width, scroll, height, icon are optional.
```
#Block(name, *children, **options)
block = Block(‘Pictures’,add_button, images)
```
 
The first Block child is a widget(s) which are drawn in the block header just after its name.
Blocks can be shared between the user screens with its states. Such a block has to be located in the 'blocks' folder .
Examples of such block tests/blocks/tblock.py:
```
from unisi import *
..
concept_block = Block('Concept block',
   [   #some Units
       Button('Run',run_proccess),
       Edit('Working folder','run_folder')
   ], result_table)
```
If some elements are enumerated inside an array, Unisi will display them on a line one after another, otherwise everyone will be displayed on a new own line(s).
 
Using a shared block in some screen:
```
from blocks.tblock import concept_block
...
blocks = [xblock, concept_block]
```

#### Layout of blocks. #### 
If the blocks are simply listed Unisi draws them from left to right or from top to bottom depending on the orientation setting. If a different layout is needed, it can be set according to the following rule: if the vertical area must contain more than one block, then the enumeration in the array will arrange the elements vertically one after another. If such an element enumeration is an array of blocks, then they will be drawn horizontally in the corresponding area.

#### Example ####
blocks = [ [b1,b2], [b3, [b4, b5]]]
#[b1,b2] - the first vertical area, [b3, [b4, b5]] - the second one.
![image](https://github.com/user-attachments/assets/16ab9909-08b3-429e-9205-9b388b10aba7)

### ParamBlock ###
ParamBlock(name, *units, row = 3, **parameters)

ParamBlock creates blocks with Unit elements formed from parameters. Parameters can be string, bool, number and optional types. Example:
```
block = ParamBlock('Learning parameters', Button('Start learning', learn_nn)
    per_device_eval_batch_size=16, num_train_epochs=10, warmup_ratio=0.1, 
    logging_steps=10, device = (‘cpu’,['cpu', 'gpu']),load_best = True)
```

If a string parameter has several options as a device in the example, its value is expressed as an option list and the first value is the initial value.
For optional types Select, Tree, Range the value has to contain the current value and its options. In the example
```
device = (‘cpu’,['cpu', 'gpu'])
```
means the current value of 'device' is 'cpu' and options are ['cpu', 'gpu'] .


### Basic information element - Unit ###
Normally they have type property which says unisi what data it contains and optionally how to operate and draw the element. 
#### If the element name starts from _ , unisi will hide its name on the screen. ####
if we need to paint an icon in an element, add 'icon': 'any MD icon name' to the element constructor.

#### Most constructor parameters are optional for Unit elements except the first one which is the element name. ####

Common form for element constructors:
```
Unit('Name', value = some_value, changed = changed_handler)
#It is possible to use short form, that is equal:
Unit('Name', some_value, changed_handler)
```
calling the method 
def accept(self, value) 
causes  a call changed handler if it defined, otherwise just save value to the element 'value'.

### Button ###
Normal button.
```
Button('Push me', changed = None, icon = None) 
```
Short form
```
Button('Name', changed_handler) 
```
Icon button, the name has to be started from _ for hiding 
```
Button('_Check', changed = None, icon = None)
```

### Load to server Button ###
Special button provides file loading from user device or computer to a Unisi system.
```
UploadButton('Load', handler_when_loading_finish, icon = 'photo_library')
```
handler_when_loading_finish(button_, the_loaded_file_filename) where the_loaded_file_filename is a file name in upload server folder. This folder name is defined in config.py .

### Edit and Text field. ###
```
Edit(name,value = '', changed_handler = None) #for string value
Edit(name, value: number, changed_handler = None) #changed handler gets a number in the value parameter
```
If set edit = False the element will be readonly.
```
Edit('Some field', '', edit = False) 
#text, it is equal
Text('Some field')
```
complete handler is optional function which accepts the current edit value and returns a string list for autocomplete.

```
def get_complete_list(unit, current_value):
    return [s for s in vocab if current_value in s]    

Edit('Edit me', 'value', complete = get_complete_list) #value has to be string or number
```

Optional 'update' handler is called when the user press Enter in the field.
It can return None if OK or objects for updating as usual 'changed' handler.

### Range ###
Number field for limited in range values.

Range('Name',  value = 0,  changed_handler = None, options=[min,max, step])

Example:  
```
Range('Scale content',  1, options=[0.25, 3, 0.25])
```


### Radio button ###
```
Switch(name, value = False, changed_handler = None, type = 'radio')
value is boolean, changed_handler is an optional handler.
Optional type can be 'check' for a status button or 'switch' for a switcher . 
```

### Select group. Contains options field. ###
```
Select(name, value = None, changed_handler = None, options = ["choice1","choice2", "choice3"]) 
```
Optional type parameter can be 'radio','list','select'. Unisi automatically chooses between 'radio' and 'select', if type is omitted.
If type = 'list' then Unisi build it as vertical select list.


### Image. ###
width,changed,height,header are optional, changed is called if the user select or touch the image.
When the user click the image, a check mark is appearing on the image, showning select status of the image.
It is usefull for image list, gallery, e.t.c
```
Image(image_path, value = False, changed_handler = None, label = None, url = None  width = None height = None)
```

### Video. ###
width and height are optional.
```
Video(video_url, width = None, height = None)
```

### Tree. The element for tree-like data. ###
```
Tree(name, value = None, changed_handler = None, options = {name1: parent1, name2 : None, .})
```
options is a tree structure, a dictionary {item_name:parent_name}. 
parent_name is None for root items. changed_handler gets selected item key (name) as value. 

### Table. ###
Tables is common structure for presenting 2D data and charts. 

Table(name, value = None, changed_handler = None, **options)

Optional append, delete, update handlers are called for adding, deleting and updating handlers for a table.

All editing table handlers for such action can be blocked by assigning edit  = False in a Table constructor.
```
table = Table('Videos', [0], row_changed, headers = ['Video', 'Duration', 'Owner', 'Status'],  
  rows = [
    ['opt_sync1_3_0.mp4', '30 seconds', 'Admin', 'Processed'],
    ['opt_sync1_3_0.mp4', '37 seconds', 'Admin', 'Processed']
  ], 
  multimode = False, update = update)
```
UNISI counts rows id as an index in a rows array. If table does not contain append, delete arguments, then it will be drawn without add and remove icons.  
value = [0] means 0 row is selected in multiselect mode (in array). multimode is False so switch icon for single select mode will be not drawn and switching to single select mode is not allowed.

| Table option parameter |	Description |
| :---: | :---: | 
| changed  | table handler accept the selected row number |
| complete |  Autocomplete handler as with value type (string value, (row index, column index)) that returns a string list of possible complitions |
| append |  A handler gets new row index and return filled row with proposed values, has system append_table_row by default |
| delete | A handler gets list or index of selected rows and remove them. system delete_table_row by default |
| update | called when the user presses the Enter in a table cell |
| modify | default = accept_rowvalue(table, value). called when the cell value is changed by the user |
| edit   | default True. if true user can edit table, using standart or overloaded table methods |
| tools  | default True, then  Table has toolbar with search field and icon action buttons. |
| show   | default False, the table scrolls to (the first) selected row, if True and it is not visible |
| multimode | default True, allows to select single or multi selection mode |


### Chart ###
Chart is a table with additional Table constructor parameter 'view' which explaines UNISI how to draw a chart. The format is '{x index}-{y index1},{y index2}[,..]'. '0-1,2,3' means that x axis values will be taken from 0 column, and y values from 1,2,3 columns of row data.
'i-3,5' means that x axis values will be equal the row indexes in rows, and y values from 3,5 columns of rows data. If a table constructor got view = '..' parameter then UNISI displays a chart icon at the table header, pushing it switches table mode to the chart mode. If a table constructor got type = 'chart' in addition to view parameter the table will be displayed as a chart on start. In the chart mode pushing the icon button on the top right switches back to table view mode.

### Graph ###
Graph supports an interactive graph.
```
graph = Graph('X graph', value = None, changed_handler = None, 
    nodes = [ Node("Node 1"),Node("Node 2", size = 20),None, Node("Node 3", color = "#3CA072")],
    edges = [ Edge(0,1, color = "#3CA072"), Edge(1,3,'extending', size = 6),Edge(3,4, size = 2), Edge(2,4)]])
```
where value is None or a dictionary like {'nodes' : [id1, ..], 'edges' : [id2, ..]}, where enumerations are selected nodes and edges.
Constant graph_default_value == {'nodes' : [], 'edges' : []} i.e. nothing to select.

'changed_handler' is called when the user (de)selected nodes or edges:
```
def changed_handler(graph, val):
    graph.value = val
    if 'nodes' in val:        
        return Info(f'Nodes {val["nodes"]}') 
    if 'edges' in val:
        return Info(f"Edges {val['edges']}") 
```
With pressed 'Shift' multi (de)select works for nodes and edges.

id nodes and edges are optinal, if node ids are ommited then edge 'source' and 'target' have to point node index in nodes array.
Graph can handle invalid edges and null nodes in the nodes array.   

### Dialog ###
```
Dialog(question, dialog_callback, commands = ['Ok', 'Cancel'], *content)
```
where buttons is a list of the dialog command names,
Dialog callback has the signature as others with a pushed button name value
```
def dialog_callback(current_dialog, command_button_name):
    if command_button_name == 'Ok':
        do_this()
    elif ..
```
content can be filled with Unit elements for additional dialog functionality like a Block.


### Popup windows ###
They are intended for non-blocking displaying of error messages and informing about some events, for example, incorrect user input and the completion of a long process on the server.
```
Info(info_message, *UnitforUpdades)
Warning(warning_message, *UnitforUpdades)
Error(error_message, *UnitforUpdades)
```
They are returned by handlers and cause appearing on the top screen colored rectangle window for 3 second. UnitforUpdades is optional Unit enumeration for updating on client side (GUI).

For long time processes it is possible to create Progress window. It is just call user.progress in any async handler.
Open window 
```
await user.progress("Analyze .. Wait..")
```
Update window message 
```
await user.progress(" 1% is done..")
```
Progress window is automatically closed when the handler is finished.

### Milti-user support. ###
UNISI automatically creates and serves an environment for every user.
The management class is User contains all required methods for processing and handling the user activity. A programmer can redefine methods in the inherited class, point it as system user class and that is all. Such methods suit for history navigation, undo/redo and initial operations. The screen folder contains screens which are recreated for every user. The same about blocks. The code and modules outside that folders are common for all users as usual. By default UNISI uses the system User class and you do not need to point it. 
```
class Hello_user(unisi.User):
    def __init__(self, session, share = None):
        super().__init__(session, share)
        print('New Hello user connected and created!')

unisi.start('Hello app', user_type = Hello_user)
```
In screens and blocks sources we can access the user by 'user' variable
```
print(isinstance(user, Hello_user))
```

### Unified Remote API ###
For using UNISI apps from remote programs Unified Remote API is an optimal choice.

| Proxy methods, properties | Description |
| :---: | :---: | 
| close() | Close session. |
| command_upload(element: str or dict, file_name: str) | upload file_name file  to  the server and execute element command (push the button). |
| command(element: str or dict) | Executes the element command.The element type is Button. |
| element(name:str) | returns an element with such name |
| elements(block’ :str or dict,  types’ : list[str]) | returns screen elements in json format, filtered by optional block and list of types. |
| interact(message: Object, pcallback`) | Sends a message, gets an answer and returns the type of response. pcallback is an optional  progress callback. |
| screen_menu | Returns the screen names. |
| set_screen(screen_name: str) | Set active  screen.  |
| set_value(element: str or dict, value: any) | Set the value to the element  |

 ‘  after a variable means it’s optional.
The UNISI Proxy creates a user session and operates in the same manner as a browsing user.

For example access to UNISI Vision  :
```
#Interact with https://github.com/unisi-tech/vision
from unisi import Proxy, Event

proxy = Proxy('localhost:8000')

#image for analysis
image_file = '/home/george/Projects/save/animals/badger/0cf04d0dab.jpg'

#It has Screen "Image analysis"
if proxy.set_screen("Image analysis"):    
    #optional: turn off search images for perfomance, we only need to classify the image
    #for that find Switch 'Search' and set it to False    
    proxy.set_value('Search', False)
    
    #push with parameter UploadButton 'Load an image'  on the screen
    if proxy.command_upload('Load an image', image_file) & Event.update:
        #get result table  after responce
        table = proxy.element('Image classification')        

        #and take parameters from the result table.
        print('  Answer:')
        for row in table['rows']:
            print(row)

proxy.close()
```

### Monitoring ###

Activation: froze_time = max_freeze_time  in config.py
The system monitor tracks current tasks and their execution time. If a task takes longer than  max_freeze_time time in seconds, the monitor writes a message in the log about the state of the system queue and the execution or waiting time of each session in the queue, and information about the event that triggered it. This allows you to uniquely identify the handler in your code and take action to correct the problem. 

### Profiling ###

Activation: profile = max_execution_time  in config.py
The system tracks current tasks and their execution time. If a task takes longer than  profile time in seconds, the system writes a message in the log about the task, the execution time and information about the event that triggered it. This allows you to uniquely identify the handler in your code and take action to correct the problem. 

### Database interactions ###
Programming database interactions usually requests knowledge of concrete DBMS, specific of its language, programming and administrative details, and a lot of time for setting and programming. UNISI automates all DBMS operations and a regular programmer or user event does not need to know how exactly the system gets and updates the program data. UNISI hides complexity of DBMS programming under inherited-from-list objects that project operations on its data into DBMS. 
UNISI database operates with named tables and graphs based on tables. The only difference between temporal data and persistent data is that the latter has an ID property, which serves as its system name. The UNISI DBMS supports tables, graphs, and Cypher queries on them.
A link to another persistent table can be established using the 'link' option. This can be set as:
- A table variable.
- A tuple containing a table variable and link properties (name to type dictionary).
- A tuple containing a table variable, link properties, and the index name in the database.

UNISI synchronizes all database changes between users, allowing them to see real-time updates made by others on persistent units.

Link properties are defined as a dictionary, where the keys are property names and the values are property values. These values are necessary for type detection.
UNISI supports now the following data types for persistent tables and links:
- Boolean (bool)
- Integer (int)
- Float (float)
- String (str)
- Datetime
- Date
- Bytes
- List

Table options multimode = True and value define relation type 1 -> 1 if equals None or 1 -> many if equals [].
UNISI is compatible with any database that implements the Database and Dbtable methods. Internally UNISI operates using the Kuzu graph database.
For using the functionality db_dir  in config.py has to be defined as a path to the database directory.

### Voice interaction. ###
This functionality allows users to interact with a user interface using voice commands instead of fingers or a mouse. It facilitates voice interaction with a graphical user interface composed of various Units. It recognizes spoken words, interprets them as commands or element selections, and performs corresponding actions. The system supports various modes of interaction, including text input, number input, element selection, screen navigation, and command execution. The user speaks commands or element names. The module recognizes words and updates the Mate block, which exposes the state of the module and what it expects to listen.

#### Modes. ####
Select Mode (Default): The user can select an interactive element or switch to another mode (e.g., "screen" to change a current screen).
Text Mode: Activated when a text input element is selected. The user can dictate text, and use commands like "left," "right," "backspace," "delete," "space," "undo," and "clean."


Number Mode: Activated when a number input element is selected. The user can dictate numbers or use number-related commands. 

Screen Mode: Allows the user to switch the current screen.

Command Mode: Activated when a command element is selected (e.g., a button). The user can execute the command using words like "push," "execute," or "run." Synonyms like "ok" and "okay" are also recognized.

Graph Mode: Supports graph element manipulation (nodes and edges). Currently documented but no specifics are provided on how to use it.

Table Mode: Supports table navigation and editing with commands like "page", "row", "column", "left", "right", "up", "down", "backspace", and "delete." Currently documented but no specifics are provided on how to use it.


Examples are in tests folder.

Demo project and UNISI part https://github.com/unisi-tech/vision

Support the development and get Comprehensive and Professional UNISI Documentation:
[Patreon](https://www.patreon.com/user/shop/comprehensive-and-professional-unisi-392229?u=119394296&utm_medium=clipboard_copy&utm_source=copyLink&utm_campaign=productshare_creator&utm_content=join_link)

