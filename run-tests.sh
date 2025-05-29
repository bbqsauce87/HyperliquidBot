#!/bin/bash
# Install dependencies and run the test suite
set -e
pip install -r requirements.txt -r requirements-dev.txt
pytest "$@"
