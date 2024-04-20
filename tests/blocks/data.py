import pandas as pd, time
table = pd.read_csv('zoo.csv')

def long_function(ticks, queue):
    queue.put('Run process')
    for i in range(ticks):        
        time.sleep(0.04)
        queue.put(f'{i} tick')
    return 5
