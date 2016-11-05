"""
MGEN Generic utilities
"""

import json
import decimal
import uuid


class JSONEncoder(json.JSONEncoder):
    '''Encode JSON special types'''

    def default(self, o):
        # handler decimals
        if isinstance(o, decimal.Decimal):
            return float(o)
            
        # handle uuid
        if isinstance(o, uuid.UUID):
            return o.hex
            
        # handle sqlalchemy models
        if hasattr(o, "to_json"):
            return getattr(o, "to_json")()
            
            
        # fallback 
        return super(JSONEncoder, self).default(o)