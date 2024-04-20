import multiprocessing, time, asyncio, logging
from .utils import start_logging
from config import froze_time, monitor_tick, profile, pool

def write_string_to(shared_array, input_string):    
    input_bytes = input_string.encode()    
    shared_array[:len(input_bytes)] = input_bytes

def read_string_from(shared_array):
    return shared_array[:].decode().rstrip('\x00')

_multiprocessing_pool = None



def multiprocessing_pool():
    global _multiprocessing_pool
    if not _multiprocessing_pool:
        _multiprocessing_pool = multiprocessing.Pool(pool)
    return _multiprocessing_pool

# Define an asynchronous function that will run the synchronous function in a separate process
""" argument example
def long_running_task(queue):
    for i in range(5):
        time.sleep(2)  # emulate long calculation
        queue.put(f"Task is {i*20}% complete")
    queue.put(None)

async def callback(string):
    await context_user().progress(str)
"""
async def run_external_process(long_running_task, *args, callback = False):
    if callback:
        queue = multiprocessing.Manager().Queue()    
        args = *args, queue
    result = multiprocessing_pool().apply_async(long_running_task, args)
    if callback:
        while not result.ready():
            if not queue.empty():
                message = queue.get()
                if message is None:
                    break
                await callback(message)
            await asyncio.sleep(0.1)  
    return result.get()

logging_lock = multiprocessing.Lock()

splitter = '~'

def monitor_process(monitor_shared_arr):            
    timer = None
    session_status = {}    
    sname = None    
    start_logging()
    while True:
        #Wait for data in the shared array
        while monitor_shared_arr[0] == b'\x00':
            time.sleep(0.005)  
            if timer is not None:
                timer -= monitor_tick                
                if timer < 0:
                    timer = None
                    
                    arr = list(session_status.items())
                    arr.sort(key = lambda s: s[1][1], reverse=True)
                    ct = time.time()
                    message = "Hangout is detected! Sessions in a queue and time waiting:" +\
                        ''.join(f'\n  {s[0]}, {s[1][0]}, {ct - s[1][1]} s' for s in arr)    
                    with logging_lock:
                        logging.warning(message)                    
                    timer = None
        
        # Read and process the data
        status = read_string_from(monitor_shared_arr).split(splitter)
        #free
        monitor_shared_arr[0] = b'\x00'
        sname = status[1]  
        match status[0]:
            case '+' | 'e': #exit external process                             
                session_status[sname] = [status[2], time.time()]
                timer = froze_time
            case '-':    
                event, tstart = session_status.get(sname, (None, 0))
                if event:                                        
                    duration = time.time() - tstart
                    if profile and duration > profile:
                        with logging_lock:
                            logging.warning(f'Event handler {event} was executed for {duration} seconds!')
                    del session_status[sname] 
                    timer = None
            case 'p': #call external process
                session_status[sname] = [status[2], time.time()]
                timer = None
                            
if froze_time or profile: 
    # Create a shared memory array
    monitor_shared_arr = multiprocessing.Array('c', 200)  
    monitor_shared_arr[0] != b'\x00'
    
    async def notify_monitor(status, session, event):
        s = f'{status}{splitter}{session}{splitter}{event}'        
        # Wait for the shared array to be empty
        while monitor_shared_arr[0] != b'\x00':
            await asyncio.sleep(monitor_tick)
        write_string_to(monitor_shared_arr, s)

    monitor_process = multiprocessing.Process(target=monitor_process, args=(monitor_shared_arr,))
    monitor_process.start()
else:
    notify_monitor = None
    


        