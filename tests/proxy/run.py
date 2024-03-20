#Interact with https://github.com/unisi-tech/vision
from unisi import Proxy, Event

proxy = Proxy('localhost:8000')

#image for analysis
image_file = '/home/george/Projects/save/animals/badger/0cf04d0dab.jpg'

if proxy.set_screen("Image analysis"):
    
    #optional: turn off search images for perfomance, we only need to classify the image
    #for that find Switch 'Search' and set it to False
    search_switch = proxy.element('Search')
    proxy.set_value(search_switch, False)

    command = proxy.element('Load an image')
    if command:
        if proxy.command_upload(command, image_file) & Event.update:
            table = proxy.element('Image classification')
            #print answer
            print('  Answer:')
            for row in table['rows']:
                print(row)

proxy.close()
