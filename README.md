# UNISI #
UNified System Interface and GUI

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

### Handling events ###
All handlers are functions which have a signature
```
def handler_x(gui_object, value_x)
```
where gui_object is a Python object the user interacted with and value for the event.

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
| UpdateScreen, True | Redraw whole screen |
| Dialog(..) | Open a dialog with parameters |
| user.set_screen(screen_name) | switch to another screen |


Unisi	synchronizes GUI state on frontend-end automatically after calling a handler.

If a Gui object doesn't have 'changed' handler the object accepts incoming value automatically to the 'value' variable of gui object.

If 'value' is not acceptable instead of returning an object possible to return Error or Warning or Info. That functions can update a object list passed after the message argument.

```
def changed_range(guirange, value):
   if value < 0.5 and value > 1.0:       
       return Error(f‘The value of {guirange.name} has to be > 0.5 and < 1.0!', guirange) 
    #accept value othewise
    guirange.value = value

edit = Edit('Range of involving', 0.6, changed_range, type = 'number')
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

#### Events interception of shared elements ####
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

#### Layout of blocks. #### 
If the blocks are simply listed Unisi draws them from left to right or from top to bottom depending on the orientation setting. If a different layout is needed, it can be set according to the following rule: if the vertical area must contain more than one block, then the enumeration in the array will arrange the elements vertically one after another. If such an element enumeration is an array of blocks, then they will be drawn horizontally in the corresponding area.

#### Example ####
blocks = [ [b1,b2], [b3, [b4, b5]]]
#[b1,b2] - the first vertical area, [b3, [b4, b5]] - the second one.

![image](https://github.com/unisi-tech/unisi/assets/1247062/aa0c3623-ef57-45ce-a179-7ba53df119c3)

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
Button('Push me', changed = push_callback) 
```
Short form
```
Button('Push me', push_callback) 
```
Icon button 
```
Button('_Check', push_callback, icon = 'check')
```

### Load to server Button ###
Special button provides file loading from user device or computer to the Unisi server.
```
UploadButton('Load', handler_when_loading_finish, icon='photo_library')
```
handler_when_loading_finish(button_, the_loaded_file_filename) where the_loaded_file_filename is a file name in upload server folder. This folder name is defined in config.py .

### Edit and Text field. ###
```
Edit('Some field', '') #for string value
Edit('Number field', 0.9, type = 'number') #changed handler will get a number
```
If set edit = false it will be readonly field.
```
Edit('Some field', '', edit = false) 
#text
Text('Some text')
```
complete handler is optional function which accepts the current edit value and returns a string list for autocomplete.

```
def get_complete_list(gui_element, current_value):
    return [s for s in vocab if current_value in s]    

Edit('Edit me', 'value', complete = get_complete_list) #value has to be string or number
```

Optional 'update' handler is called when the user press Enter in the field.
It can return None if OK or objects for updating as usual 'changed' handler.

Optional selection property with parameters (start, end) is called when selection is happened.
Optional autogrow property uses for serving multiline fileds.


### Radio button ###
```
Switch(name, value, changed, type = ...)
value is boolean, changed is an optional handler.
Optional type can be 'check' for a status button or 'switch' for a switcher . 
```

### Select group. Contains options field. ###
```
Select('Select something', "choice1", selection_is_changed, options = ["choice1","choice2", "choice3"]) 
```
Optional type parameter can be 'toggles','list','dropdown'. Unisi automatically chooses between toogles and dropdown, if type is omitted,
if type = 'list' then Unisi build it as vertical select list.


### Image. ###
width,changed,height,header are optional, changed is called if the user select or touch the image.
When the user click the image, a check mark is appearing on the image, showning select status of the image.
It is usefull for image list, gallery, e.t.c
```
Image(image_name, selecting_changed, header = 'description',url = ...,  width = .., height = ..)
```

### Video. ###
width and height are optional.
```
Video(video_url, width = .., height = ..)
```

### Tree. The element for tree-like data. ###
```
Tree(name, selected_item_name, changed_handler, options = {name1: parent1, name2 : None, .})
```
options is a tree structure, a dictionary {item_name:parent_name}. 
parent_name is None for root items. changed_handler gets item key (name) as value. 

### Table. ###
Tables is common structure for presenting 2D data and charts. 
Optional append, delete, update handlers are called for adding, deleting and updating rows.


Assigning a handler for such action causes Unii to draw and activate an appropriate action icon button in the table header automatically.
```
table = Table('Videos', [0], row_changed, headers = ['Video', 'Duration', 'Owner', 'Status'],  
  rows = [
    ['opt_sync1_3_0.mp4', '30 seconds', 'Admin', 'Processed'],
    ['opt_sync1_3_0.mp4', '37 seconds', 'Admin', 'Processed']
  ], 
  multimode = false, update = update)
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
def table_updated(table_, tabval):
    value, position = tabval
    #check value
    ...
    if error_found:
        return Error('Can not accept the value!')
    accept_rowvalue(table_, tabval)
```

### Chart ###
Chart is a table with additional Table constructor parameter 'view' which explaines unisi how to draw a chart. The format is '{x index}-{y index1},{y index2}[,..]'. '0-1,2,3' means that x axis values will be taken from 0 column, and y values from 1,2,3 columns of row data.
'i-3,5' means that x axis values will be equal the row indexes in rows, and y values from 3,5 columns of rows data. If a table constructor got view = '..' parameter then unisi displays a chart icon at the table header, pushing it switches table mode to the chart mode. If a table constructor got type = 'chart' in addition to view parameter the table will be displayed as a chart on start. In the chart mode pushing the icon button on the top right switches back to table view mode.

### Graph ###
Graph supports an interactive graph.
```
graph = Graph('X graph', graph_value, graph_selection, 
    nodes = [
     { 'id' : 'node1', 'name': "Node 1" },
     { 'id' : 'node2', 'name': "Node 2" },
     { 'id' : 'node3', 'name': "Node 3" }    
  ], edges = [
     { 'id' : 'edge1', 'source': "node1", 'target': "node2", 'name' : 'extending' },
     { 'id' :'edge2' , 'source': "node2", 'target': "node3" , 'name' : 'extending'}     
  ])
```
where graph_value is a dictionary like {'nodes' : ["node1"], 'edges' : ['edge3']}, where enumerations are selected nodes and edges.
Constant graph_default_value == {'nodes' : [], 'edges' : []} i.e. nothing to select.

'changed' method graph_selector called when user (de)selected nodes or edges:
```
def graph_selection(_, val):
    _.value = val
    if 'nodes' in val:        
        return Info(f'Nodes {val["nodes"]}') 
    if 'edges' in val:
        return Info(f"Edges {val['edges']}") 
```
With pressed 'Shift' multi select works for nodes and edges.

id nodes and edges are optinal, if node ids are ommited then edge 'source' and 'target' have to point node index in nodes array.

### Dialog ###
```
Dialog(text, dialog_callback, commands = ['Ok', 'Cancel'], *content)
```
where buttons is a list of the dialog button names,
Dialog callback has the signature as other with a pushed button name value
```
def dialog_callback(current_dialog, pushed_button_name):
    if pushed_button_name == 'Yes':
        do_this()
    elif ..
```
content can be filled with Gui elements for additional dialog functionality.


### Popup windows ###
They are intended for non-blocking displaying of error messages and informing about some events, for example, incorrect user input and the completion of a long process on the server.
```
Info(info_message, *someGUIforUpdades)
Warning(warning_message, *someGUIforUpdades)
Error(error_message, *someGUIforUpdades)
```
They are returned by handlers and cause appearing on the top screen colored rectangles window for 3 second. someGUIforUpdades is optional GUI enumeration for updating.

For long time processes it is possible to create Progress window. It is just call user.progress in any place.
Open window 
```
user.progress("Analyze .. Wait..")
```
Update window message 
```
user.progress(" 1% is done..")
```
Progree window is automatically closed when the handler is finished.

### Milti-user support. ###
Unisi automatically creates and serves an environment for every user.
The management class is User contains all required methods for processing and handling the user activity. A programmer can redefine methods in the inherited class, point it as system user class and that is all. Such methods suit for history navigation, undo/redo and initial operations. The screen folder contains screens which are recreated for every user. The same about blocks. The code and modules outside that folders are common for all users as usual. By default Unisi uses the system User class and you do not need to point it. 
```
class Hello_user(unisi.User):
    def __init__(self):
        super().__init__()
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



### 'Become a serious web programmer in 1 hour.' is a crash course how to use UNISI ###
   https://www.unisi.tech/learn

Examples are in tests folder.

Demo project and UNISI part https://github.com/unisi-tech/vision

