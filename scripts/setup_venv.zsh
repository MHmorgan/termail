#!/usr/bin/env zsh

python3.10 -m venv venv
source venv/bin/activate
pip install -U autopep8
pip install -r requirements.txt
pip install -e .
