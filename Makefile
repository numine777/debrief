PY ?= python3
PY39 ?= python3.9

.PHONY: build test test-py39 dogfood clean

build:
	$(PY) build.py

test:
	PYTHONPATH=src $(PY) -m unittest discover -s tests -t . $(if $(V),-v,)

test-py39:
	PYTHONPATH=src $(PY39) -m unittest discover -s tests -t .

dogfood:
	$(PY) build.py && $(PY) dist/debrief.pyz install --harness none

clean:
	rm -rf dist
