"""Batch runner for intent_boundaries; explicit --live required to call a model."""

from run_selection_cases import cli

if __name__ == "__main__":
    cli("intent_boundaries")
