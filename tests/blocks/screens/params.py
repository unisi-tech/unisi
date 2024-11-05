
from unisi import *
order = 2
name = 'Parameters'

def get_params(button, _):
    return Info(str(block.params))

block = ParamBlock('System parameters', Button('Show server params', get_params), 
    per_device_eval_batch_size=16,
    num_train_epochs=10, 
    warmup_ratio=0.1, 
    logging_steps= (10,[1,20,1]), 
    device = ('gpu', ['cpu', 'gpu']),
    load_best = True)

def html_handler(unit, event):
    return Info(event)

html = HTML('HTML',  '<button>Click me</button> <a href="#">Link</a> <input type="text">', html_handler)

html_block = Block('Block with HTML', [], html)

blocks = [block, html_block]
