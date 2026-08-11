from unisi import *
ed = Edit('Number only', 2.5),

def get_path(*_):
    return Info(str(user.persist_location(ed)))

eblock = Block('Shared block',  Button('Get path', get_path),
        Range('Scaling', 0, options=[0.0,1.0,0.1]),                
        ed,   
        Block('Embedded block', 
            Edit('Edit string', 'xyz'),
            Switch('Switch', True)),
        persist = True
)