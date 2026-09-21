"""GeneticEngine package (additive, flag-gated off by default)."""
from .genetic_engine import (
    PARAM_ORDER, PARAM_SPEC, GeneticEngine, fitness_value, random_param,
)

__all__ = ["GeneticEngine", "PARAM_ORDER", "PARAM_SPEC", "fitness_value",
           "random_param"]
