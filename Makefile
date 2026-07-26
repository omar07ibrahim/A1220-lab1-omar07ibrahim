PYTHON ?= python3

.PHONY: audit check demo demo-check dry-run lint run test typecheck

run:
	@if [ -z "$$OPENAI_API_KEY" ]; then \
		printf '%s\n' 'OPENAI_API_KEY is required for live extraction' >&2; \
		exit 1; \
	fi
	@PYTHONPATH=src $(PYTHON) -m receipt_extractor.main receipts \
		--acknowledge-remote-upload \
		--output result.json

dry-run:
	@PYTHONPATH=src $(PYTHON) -m receipt_extractor.main receipts --dry-run

audit:
	@pip-audit --progress-spinner=off -r requirements.txt

lint:
	@ruff check src tests scripts
	@ruff format --check src tests scripts

typecheck:
	@MYPYPATH=src mypy --strict src tests scripts

test:
	@PYTEST_ADDOPTS= PYTHONPATH=src $(PYTHON) -m pytest \
		--cov=receipt_extractor \
		--cov-branch \
		--cov-report=term-missing \
		-q

demo:
	@PYTEST_ADDOPTS= PYTHONPATH=src $(PYTHON) -m pytest \
		--ignore=tests/test_demo_evidence.py \
		--cov=receipt_extractor \
		--cov-branch \
		--cov-report=term-missing \
		-q
	@$(PYTHON) -m coverage json --pretty -o .venv/demo-bootstrap-coverage.json
	@PYTHONPATH=src $(PYTHON) scripts/capture_demo.py \
		--output-root . \
		--coverage-json .venv/demo-bootstrap-coverage.json
	@$(MAKE) --no-print-directory test
	@$(PYTHON) -m coverage json --pretty -o .venv/demo-coverage.json
	@PYTHONPATH=src $(PYTHON) scripts/capture_demo.py \
		--output-root . \
		--coverage-json .venv/demo-coverage.json
	@$(MAKE) --no-print-directory test

demo-check: test
	@$(PYTHON) -m coverage json --pretty -o .venv/demo-coverage.json
	@if [ -d .venv/demo-check ]; then \
		find .venv/demo-check -mindepth 1 -depth -delete; \
	fi
	@PYTHONPATH=src $(PYTHON) scripts/capture_demo.py \
		--output-root .venv/demo-check \
		--coverage-json .venv/demo-coverage.json
	@diff --recursive --brief demo .venv/demo-check/demo
	@diff --recursive --brief docs/assets .venv/demo-check/docs/assets

check: lint typecheck demo-check
