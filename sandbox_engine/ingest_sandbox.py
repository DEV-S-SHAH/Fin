#!/usr/bin/env python3
"""
Thin entrypoint for the SEC filing ingestion pipeline.

This script delegates to the Typer CLI in ``sandbox_engine.cli``.
All pipeline logic lives in ``sandbox_engine.ingestion``.
"""

from sandbox_engine.cli import main

if __name__ == "__main__":
    main()
