from collections.abc import Iterable
from typing import Dict

import os.path

import jsonschema
import yaml

from .constants import (
    STATIC_DATA_DIR,
    BASE_CONFIG_FILENAME,
    SCHEMA_FILENAME,
)
from .utils import deep_merge_dict

def load_config(*config_files: Iterable[str]) -> Dict:
    base_cfg_fn = os.path.join(os.path.dirname(__file__), STATIC_DATA_DIR, BASE_CONFIG_FILENAME)
    with open(base_cfg_fn) as config_handle:
        config = yaml.safe_load(config_handle)

    for user_cfg_fn in config_files:
        with open(user_cfg_fn) as user_cfg_handle:
            config = deep_merge_dict(config, yaml.safe_load(user_cfg_handle))

    # this is a placeholder as there is no actual media proc config atm
    config['media'] = {}

    return config

def validate_config(config: Dict) -> None:
    schema_fn = os.path.join(os.path.dirname(__file__), STATIC_DATA_DIR, SCHEMA_FILENAME)
    with open(schema_fn) as schema_handle:
        schema = yaml.safe_load(schema_handle)

    jsonschema.validate(config, schema)
