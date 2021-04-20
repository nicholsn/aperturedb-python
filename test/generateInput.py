import argparse
import random
from datetime import datetime

import pandas as pd

from itertools import product

def generate_person_csv(multiplier):

    names     = ["James", "Luis", "Sole", "Maria", "Tom", "Xavi", "Dimitris", "King"]
    lastnames = ["Ramirez", "Berlusconi", "Copola", "Tomson", "Ferro"]
    names     = names     * multiplier
    lastnames = lastnames * multiplier

    persons  = list(product(names, lastnames))

    entity   = [ "Person"                       for x in range(len(persons))]
    ids      = [int(1000000000* random.random()) for i in range(len(persons))]
    age      = [int(100* random.random())       for i in range(len(persons))]
    height   = [float(200* random.random())     for i in range(len(persons))]
    dog      = [ x > 100 for x in height ]
    birth    = [ datetime.now().isoformat()     for x in range(len(persons)) ]

    df = pd.DataFrame(persons, columns=['name', 'lastname'])
    df["EntityClass"] = entity
    df = df.reindex(["EntityClass", "name", "lastname"], axis=1)
    df["id"]       = ids
    df["age"]      = age
    df["height"]   = height
    df["has_dog"]  = dog
    df["date:dob"] = birth
    df["constraint_id"] = ids

    df = df.sort_values("id")

    df.to_csv("input/persons.adb.csv", index=False)

def main(params):

    generate_person_csv(params.multiplier)

def get_args():
    obj = argparse.ArgumentParser()

    # Run Config
    obj.add_argument('-multiplier', type=int, default=10)

    params = obj.parse_args()

    return params

if __name__ == "__main__":
    args = get_args()
    main(args)