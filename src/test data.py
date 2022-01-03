import os
import pandas as pd
import json
from pathlib import Path

data_location = os.path.join(str(Path(__file__).parents[0]), 'data')

with open(os.path.join(data_location, 'train.table_col_type.json'), 'r', encoding='utf-8') as json_file:
    train_dict = json.load(json_file)

test=1