import multiprocessing, time, asyncio

def write_string_to(shared_array, input_string):    
    input_bytes = input_string.encode()    
    shared_array[:len(input_bytes)] = input_bytes

def read_string_from(shared_array):
    return shared_array[:].decode().rstrip('\x00')

splitter = '~'
tick = 0.005

def ext(s):
    return s[1][1]

def monitor_process(shared_arr):        
    hangout_time = 2.0
    timer = None
    session_status = {}
    sname = None

    while True:
        #Wait for data in the shared array
        while shared_arr[0] == b'\x00':
            time.sleep(0.005)  
            if timer is not None:
                timer -= tick                
                if timer < 0:
                    timer = None
                    print("Hangout is detected! Sessions in a queue and time waiting:")
                    arr = list(session_status.items())
                    arr.sort(key = ext , reverse=True)
                    ct = time.time()
                    for s in arr:
                        print('  ', s[0],s[1][0], ct - s[1][1], 's')
                    timer = None
        
        # Read and process the data
        status = read_string_from(shared_arr).split(splitter)
        #free
        shared_arr[0] = b'\x00'
        sname = status[1]  
        match status[0]:
            case '+' | 'e': #exit external process                             
                session_status[sname] = [status[2], time.time()]
                timer = hangout_time
            case '-':                            
                del session_status[sname] 
                timer = None
            case 'p': #call external process
                session_status[sname] = [status[2], time.time()]
                timer = None
                            

if __name__ == "__main__": 
    async def run_monitor():
        # Create a shared memory array
        shared_arr = multiprocessing.Array('c', 100)  # Adjust size as needed
        shared_arr[0] != b'\x00'

        async def put_in_monitor(status, session, event):
            s = f'{status}{splitter}{session}{splitter}{event}'        
            # Wait for the shared array to be empty
            while shared_arr[0] != b'\x00':
                await asyncio.sleep(tick)
            write_string_to(shared_arr, s)

        # Start the synchronous process
        sync_process = multiprocessing.Process(target=monitor_process, args=(shared_arr,))
        sync_process.start()

        await put_in_monitor('+', 's1', 1)
        await asyncio.sleep(1)
        await put_in_monitor('+', 's2', 2)
        await asyncio.sleep(500)

    asyncio.run(run_monitor())

        