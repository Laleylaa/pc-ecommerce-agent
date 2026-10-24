"""Batch runner for single_intent; explicit --live required to call a model."""

from run_selection_cases import cli

if __name__ == "__main__":
    cli("single_intent")
