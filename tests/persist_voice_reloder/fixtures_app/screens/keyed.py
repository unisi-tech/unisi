from unisi import *

name = "Keyed"
order = 1

selector = Edit("Selector", "A")
single_key_field = Edit("Single key field", "", persist=lambda: (selector.value,))

city = Edit("City", "London")
zipc = Edit("Zip", "10001")
multi_key_field = Edit("Multi key field", "", persist=lambda: (city.value, zipc.value))

blocks = [Block("Root", selector, single_key_field, city, zipc, multi_key_field)]
