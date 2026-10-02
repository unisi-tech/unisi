from unisi import *

name = "Keyed"
order = 1

selector = Edit("Selector", "A")
single_key_field = Edit("Single key field", "", persist=lambda: (selector.value,))

city = Edit("City", "London")
zipc = Edit("Zip", "10001")
multi_key_field = Edit("Multi key field", "", persist=lambda: (city.value, zipc.value))

# A record selector whose handler puts the default for the newly selected
# record into the keyed field -- the pattern keyed persist overrides.
def pick_record(unit, value):
    unit.value = value
    record_field.value = ""

record = Edit("Record", "A", pick_record)
record_field = Edit("Record field", "", persist=lambda: (record.value,))

blocks = [Block("Root", selector, single_key_field, city, zipc, multi_key_field),
          Block("Records", record, record_field)]
