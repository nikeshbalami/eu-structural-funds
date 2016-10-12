"""A processor to convert the description file into a datapackage.

The conversion
    - 1) adds `name` fields in the metadata and in each resource
    - 2) duplicates fields from the first resource to other resources if they
         are missing (so that the description file can stay "dry")

"""

import json
import yaml

from slugify import slugify
from datapackage_pipelines.wrapper import spew, ingest
from common.config import DESCRIPTION_FILE, DATAPACKAGE_FILE


def create_datapackage(description):
    description['name'] = slugify(description['title'], separator='-')
    first_resource = description['resources'][0]

    for resource in description['resources']:
        for property_ in first_resource.keys():
            if property_ not in resource:
                resource[property_] = first_resource[property_]
        resource['name'] = slugify(resource['title'])

    return description


def load_description_file():
    with open(DESCRIPTION_FILE) as stream:
        return yaml.load(stream.read())


def save_datapackage_file(description):
    with open(DATAPACKAGE_FILE, 'w+') as stream:
        stream.write(json.dumps(description, indent=4))


if __name__ == '__main__':
    parameters, _, _ = ingest()
    save_ = parameters['save_datapackage']
    description_ = load_description_file()
    datapackage = create_datapackage(description_)
    if save_:
        save_datapackage_file(description_)
    spew(datapackage, [])