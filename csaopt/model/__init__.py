import ujson

from enum import Enum
from typing import Dict, Any


class Precision(Enum):
    Float32 = 'float32'
    Float64 = 'float64'


class RandomDistribution(Enum):
    Normal = 'normal'
    Uniform = 'uniform'


class RequiredFunctions(Enum):
    Distribution = 'distribution'
    Precision = 'precision'
    Dimensions = 'dimensions'
    Initialize = 'initialize'
    GenerateNext = 'generate_next'
    Cool = 'cool'
    Evaluate = 'evaluate'
    Acceptance = 'acceptance_func'
    EmptyState = 'empty_state'


class Model:

    @staticmethod
    def from_dict(d: Dict[str, Any]):
        assert 'distribution' in d
        assert 'precision' in d
        assert 'globals' in d

        distribution: RandomDistribution = d['distribution']
        precision: Precision = d['precision']

        return Model(d['name'],
                     d['dimensions'],
                     precision.value,
                     distribution.value,
                     d['globals'],
                     d['functions'])

    def __init__(self, name: str,
                 dimensions: int,
                 precision: Precision,
                 distribution: RandomDistribution,
                 opt_globals: str,
                 functions: Dict[str, str]) -> None:
        self.name: str = name
        self.dimensions: int = dimensions
        self.distribution: RandomDistribution = distribution
        self.precision: Precision = precision
        self.globals: str = opt_globals
        self.functions: Dict[str, str] = functions

    def to_dict(self) -> Dict[str, Any]:
        return {
            'name': self.name,
            'dimensions': self.dimensions,
            'distribution': self.distribution.value,
            'precision': self.precision.value,
            'globals': self.globals,
            'functions': self.functions
        }

    def __repr__(self) -> str:
        return ujson.dumps(self, indent=4)
