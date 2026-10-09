.PHONY: help test check
help:   ## list targets
	@grep -hE '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/'

test:   ## run the test suite (no account, network or systemd needed)
	python3 -m unittest discover -s tests -v

check:  ## syntax-check every script
	@for f in $$(git ls-files '*.py'); do python3 -m py_compile $$f && echo "py OK $$f"; done
	@for f in $$(git ls-files '*.sh'); do bash -n $$f && echo "sh OK $$f"; done

cover:  ## run the suite under coverage (subprocesses included), gate at 95%
	python3 -m coverage erase
	COVERAGE_PROCESS_START=$(CURDIR)/.coveragerc COVERAGE_DATA_DIR=$(CURDIR) \
	  PYTHONPATH=$(CURDIR)/tests/_covshim \
	  python3 -m coverage run -m unittest discover -s tests -q
	python3 -m coverage combine -q
	python3 -m coverage report --fail-under=95
