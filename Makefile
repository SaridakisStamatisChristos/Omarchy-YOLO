.PHONY: test lint typecheck check install uninstall

test:
	python -m pytest

lint:
	python -m ruff check src tests

typecheck:
	python -m mypy src/omarchy_yolo

check: lint typecheck test

install:
	./install.sh

uninstall:
	./uninstall.sh
