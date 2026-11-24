.PHONY: dev full live test benchmark
PORT ?= 8000
PYTHON ?= python3

dev:
	$(PYTHON) local_server.py --no-browser --port $(PORT)

full:
	$(PYTHON) -m venv .venv
	.venv/bin/python -m pip install -r requirements.txt
	.venv/bin/python -m uvicorn app:app --port $(PORT)

live:
	$(PYTHON) -m venv .venv
	.venv/bin/python -m pip install -r requirements.txt
	.venv/bin/python live.py --port $(PORT)

test:
	$(PYTHON) -m unittest -v

benchmark:
	$(PYTHON) benchmark.py

