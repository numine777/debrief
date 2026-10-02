PY ?= python3

.PHONY: build test test-py39 clean

build:
	$(PY) build.py

test:
	PYTHONPATH=src $(PY) -m unittest discover -s tests -t . $(if $(V),-v,)

clean:
	rm -rf dist
