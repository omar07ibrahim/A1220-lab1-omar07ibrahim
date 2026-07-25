PYTHON ?= python3

.PHONY: audit check dry-run lint run test typecheck

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
	@ruff check src tests
	@ruff format --check src tests

typecheck:
	@mypy --strict src tests

test:
	@PYTHONPATH=src pytest \
		--cov=receipt_extractor \
		--cov-branch \
		--cov-report=term-missing \
		-q

check: lint typecheck test
