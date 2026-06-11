VENV := .venv/bin

.PHONY: check lint test format

check: lint test

lint:
	$(VENV)/ruff check src tests scripts
	$(VENV)/ruff format --check src tests scripts

test:
	$(VENV)/python -m pytest -q

format:
	$(VENV)/ruff format src tests scripts
	$(VENV)/ruff check --fix src tests scripts
