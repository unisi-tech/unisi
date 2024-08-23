
from openai import OpenAI
client = OpenAI()

completion = client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {
            "role": "user",
            "content": "Write a haiku about recursion in programming."
        }
    ]
)

print(completion.choices[0].message)

import nltk
#nltk.download('wordnet')
from nltk.corpus import wordnet as wn


def get_hierarchy(synset):
    """ Recursively gets the hierarchy of synsets from the given synset to the root. """
    hierarchy = []
    hypernyms = synset.hypernyms()
    if not hypernyms:
        return [synset]
    for hypernym in hypernyms:
        hierarchy.extend(get_hierarchy(hypernym))
    hierarchy.append(synset)
    return hierarchy

def print_hierarchy(synset, level=0):
    """ Prints the hierarchy of synsets in an indented format. """
    print('  ' * level + synset.name())
    for hyponym in synset.hyponyms():
        print_hierarchy(hyponym, level + 1)

# Example usage
synset = wn.synset('griffon.n.02')
print("Hierarchy for 'dog.n.01':")
print_hierarchy(synset)

