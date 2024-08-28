# UNISI #
UNified System Interface, GUI and Remote API

### Purpose ###
UNISI technology provides a unified system interface and advanced program functionality, eliminating the need for front-end and most back-end programming. It automates common tasks, as well as unique ones, significantly reducing the necessity for manual programming and effort.

### Provided functionality without programming ###
 - Automatic WEB GUI
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

### Installing ###
```
pip install unisi
```

### Programming ###
This repo explains how to work with Unisi using Python and the tiny but optimal framework for that. Unisi web version is included in this library.  Supports Python 3.10 and up.


### High level - Screen ###
The program directory has to contain a screens folder which contains all screens which Unisi has to show.

Screen example tests/screens/main.py
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
| toolbar | Optional | list | Gui elements to show in the screen toolbar |
| order | Optional | int | order in the program menu |
| icon  | Optional | str | MD icon of screen to show in the screen menu |
| prepare | Optional | def prepare() | Syncronizes GUI elements one to another and with the program/system data. If defined then is called before screen appearing. |


### Server start ###
tests/template/run.py
```
import unisi
unisi.start('Test app') 
```
Unisi builds the interactive app for the code above.
Connect a browser to localhast:8000 which are by default and will see:

![image](https://github.com/unisi-tech/unisi/assets/1247062/dafebd1f-ae48-4790-9282-dea83d986749)  

### 'The fastest way to create Web applications in Python.' is a free crash course video how to use UNISI ###
   https://www.unisi.tech/learn

### Handling events ###
All handlers are functions which have a signature
```
def handler_x(gui_object, value_x) #or
async def handler_x(gui_object, value_x)
```
where gui_object is a Python object the user interacted with and value for the event.

#### UNISI supports synchronous and asynchronous handlers automatically adopting them for using. ####

All Gui objects except Button have a field ‘value’. 
For an edit field the value is a string or number, for a switch or check button the value is boolean, for table is row id or index, e.t.c.
When a user changes the value of the Gui object or presses Button, the server calls the ‘changed’ function handler.

```
def clean_table(_, value):
    table.rows = []
    return table
clean_button = Button('Clean the table’, clean_table)
```

| Handler returns |	Description |
| :---: | :---: | 
| Gui object |  Object to update |
| Gui object array or tuple |  Objects to update |
| None | Nothing to update, Ok |
| Error(...), Warning(...), Info(...) | Show to user info about a state. |
| True | Update whole screen |
| Redesign | Update and redesign whole screen |
| Dialog(..) | Open a dialog with parameters |

Unisi synchronizes GUI state on frontend-end automatically after calling a handler.

If a Gui object doesn't have 'changed' handler the object accepts incoming value automatically to the 'value' variable of gui object.

If 'value' is not acceptable instead of returning an object possible to return Error or Warning or Info. That functions can update a object list passed after the message argument.

```
def changed(elem, value):
   if value == 4:       
       return Error(f‘The value can not be 4!', elem) 
    #accept value othewise
    elem.value = value

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
    return _.accept(value) #otherwise accept the value
```

### Block details ###
The width and height of blocks is calculated automatically depending on their children. It is possible to set the block width, or make it scrollable , for example for images list. Possible to add MD icon to the header, if required. width, scroll, height, icon are optional.
```
#Block(name, *children, **options)
block = Block(‘Pictures’,add_button, images, width = 500, scroll = True,icon = 'api')
```
 
The first Block child is a widget(s) which are drawn in the block header just after its name.
Blocks can be shared between the user screens with its states. Such a block has to be located in the 'blocks' folder .
Examples of such block tests/blocks/tblock.py:
```
from unisi import *
..
concept_block = Block('Concept block',
   [   #some gui elements       
       Button('Run',run_proccess),
       Edit('Working folder','run_folder')
   ], result_table)
```
If some elements are enumerated inside an array, Unisi will display them on a line one after another, otherwise everyone will be displayed on a new own line(s).
 
Using a shared block in some screen:
```
from blocks.tblock import concept_block
...
blocks = [.., concept_block]
```

#### Layout of blocks. #### 
If the blocks are simply listed Unisi draws them from left to right or from top to bottom depending on the orientation setting. If a different layout is needed, it can be set according to the following rule: if the vertical area must contain more than one block, then the enumeration in the array will arrange the elements vertically one after another. If such an element enumeration is an array of blocks, then they will be drawn horizontally in the corresponding area.

#### Example ####
blocks = [ [b1,b2], [b3, [b4, b5]]]
#[b1,b2] - the first vertical area, [b3, [b4, b5]] - the second one.

![image](https://github.com/unisi-tech/unisi/assets/1247062/aa0c3623-ef57-45ce-a179-7ba53df119c3)

### ParamBlock ###
ParamBlock(name, *gui_elements, row = 3, **parameters)

ParamBlock creates blocks with Gui elements formed from parameters. Parameters can be string, bool, number and optional types. Example:
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


### Basic gui elements ###
Normally they have type property which says unisi what data it contains and optionally how to draw the element. 
#### If the element name starts from _ , unisi will hide its name on the screen. ####
if we need to paint an icon in an element, add 'icon': 'any MD icon name' to the element constructor.

#### Most constructor parameters are optional for Gui elements except the first one which is the element name. ####

Common form for element constructors:
```
Gui('Name', value = some_value, changed = changed_handler)
#It is possible to use short form, that is equal:
Gui('Name', some_value, changed_handler)
```
calling the method 
def accept(self, value) 
causes  a call changed handler if it defined, otherwise just save value to self.value

### Button ###
Normal button.
```
Button('Push me', changed = None, icon = None) 
```
Short form
```
Button('Push me', changed = None, icon = None) 
```
Icon button, the name has to be started from _ for hiding 
```
Button('_Check', changed = None, icon = None)
```

### Load to server Button ###
Special button provides file loading from user device or computer to the Unisi server.
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
def get_complete_list(gui_element, current_value):
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
parent_name is None for root items. changed_handler gets item key (name) as value. 

### Table. ###
Tables is common structure for presenting 2D data and charts. 

Table(name, value = None, changed_handler = None, **options)

Optional append, delete, update handlers are called for adding, deleting and updating handlers for a table.

Auto assigning handlers for such action can be blocked by assigning edit  = False to the Table constructor.
```
table = Table('Videos', [0], row_changed, headers = ['Video', 'Duration', 'Owner', 'Status'],  
  rows = [
    ['opt_sync1_3_0.mp4', '30 seconds', 'Admin', 'Processed'],
    ['opt_sync1_3_0.mp4', '37 seconds', 'Admin', 'Processed']
  ], 
  multimode = False, update = update)
```
Unisi counts rows id as an index in a rows array. If table does not contain append, delete arguments, then it will be drawn without add and remove icons.  
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


### Table handlers. ###
complete, modify and update have the same format as the others handlers, but value is consisted from the cell value and its position in the table.

```
def table_updated(table, tabval):
    value = tabval['value']    
    row_index = tabval['delta']
    cell_index = tabval['cell']
    #check value
    ...
    if error_found:
        return Error('Can not accept the value!')
    #call a standart handler
    accept_rowvalue(table, tabval)
```

### Chart ###
Chart is a table with additional Table constructor parameter 'view' which explaines unisi how to draw a chart. The format is '{x index}-{y index1},{y index2}[,..]'. '0-1,2,3' means that x axis values will be taken from 0 column, and y values from 1,2,3 columns of row data.
'i-3,5' means that x axis values will be equal the row indexes in rows, and y values from 3,5 columns of rows data. If a table constructor got view = '..' parameter then unisi displays a chart icon at the table header, pushing it switches table mode to the chart mode. If a table constructor got type = 'chart' in addition to view parameter the table will be displayed as a chart on start. In the chart mode pushing the icon button on the top right switches back to table view mode.

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
where buttons is a list of the dialog button names,
Dialog callback has the signature as other with a pushed button name value
```
def dialog_callback(current_dialog, command_button_name):
    if command_button_name == 'Yes':
        do_this()
    elif ..
```
content can be filled with Gui elements for additional dialog functionality like a Block.


### Popup windows ###
They are intended for non-blocking displaying of error messages and informing about some events, for example, incorrect user input and the completion of a long process on the server.
```
Info(info_message, *someGUIforUpdades)
Warning(warning_message, *someGUIforUpdades)
Error(error_message, *someGUIforUpdades)
```
They are returned by handlers and cause appearing on the top screen colored rectangles window for 3 second. someGUIforUpdades is optional GUI enumeration for updating.

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
Unisi automatically creates and serves an environment for every user.
The management class is User contains all required methods for processing and handling the user activity. A programmer can redefine methods in the inherited class, point it as system user class and that is all. Such methods suit for history navigation, undo/redo and initial operations. The screen folder contains screens which are recreated for every user. The same about blocks. The code and modules outside that folders are common for all users as usual. By default Unisi uses the system User class and you do not need to point it. 
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
Programming database interactions is not an easy task for real life apps. It requests knowledge of concrete DBMS, specific of its language, programming and administrative details, and a lot of time for setting and programming. UNISI automates all DBMS operations and a regular programmer or user event does not need to know how exactly the system gets and updates the program data. UNISI hides complexity of DBMS programming under inherited-from-list objects that project operations on its data into DBMS. 
UNISI database operates with named tables and graphs. The only difference between temporal data and persistent data is that the latter has an ID property, which serves as its system name. The UNISI DBMS supports tables, graphs, and Cypher queries on them.
A link to another persistent table can be established using the 'link' option. This can be set as:
- A table variable.
- A tuple containing a table variable and link properties (name to type dictionary).
- A tuple containing a table variable, link properties, and the index name in the database.

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


Examples are in tests folder.

Demo project and UNISI part https://github.com/unisi-tech/vision

