import jsonpickle

def flatten(*arr):
    for a in arr:
        if isinstance(a, list | tuple):
            yield from flatten(*a)
        else:
            yield a
            
class ArgObject:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

class ReceivedMessage:
    def __init__(self, data):
        self.__dict__.update(data)
        self.screen = data.get('screen')        
        self.value = data.get('value')        

def toJson(obj):
    return jsonpickle.encode(obj,unpicklable = False)




