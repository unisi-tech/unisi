from cymple import QueryBuilder as qb
from cymple.typedefs import Properties

def add_link( source_id, target_id, link_props=None):
    query = f"""
    MATCH (source), (target)
    WHERE source.ID = $source_id AND target.ID = $target_id
    CREATE (source)-[r:LINKS_TO]->(target)
    SET r = [{Properties(link_props)}]
    RETURN r
    """
    print(query)
    
    # Execute the query
    try:
        with self.db.transaction() as tx:
            result = tx.run(query)
            return result.get_next()
    except Exception as e:
        print(f"Error adding link: {e}")
        return None
        return None
    
add_link(1,2, {'f': 20})


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

