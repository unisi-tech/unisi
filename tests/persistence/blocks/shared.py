from unisi import *
eblock = Block('Shared block',  Range('Scaling', 0, options=[0.0,1.0,0.1]),                
        Edit('Number only', 2.5),
        Block('Embedded block', 
            Edit('Edit string', 'xyz'),
            Switch('Switch', True)),
        persist = True
)