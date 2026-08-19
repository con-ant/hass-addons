# Repo conveniences. `make test` is the one-command way to run the Claude Jobs
# test-suite: it installs the test dependencies and runs unittest discovery from
# the right directory (the suite must run from claudecode/ so testlib is
# importable; dotted module names like `python3 -m unittest tests.test_jobdef`
# do not work). Run a single module with `make test P=test_runner.py`.

PYTHON ?= python3
P ?= test_*.py

.PHONY: test deps

deps:
	$(PYTHON) -m pip install -q -r claudecode/tests/requirements.txt

test: deps
	cd claudecode && $(PYTHON) -m unittest discover -s tests -p "$(P)"
