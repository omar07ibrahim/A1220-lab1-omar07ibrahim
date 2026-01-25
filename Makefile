.PHONY: run

DIR ?= receipts

run:
	OPENAI_API_KEY=$(OPENAI_API_KEY) PYTHONPATH=src python -m receipt_extractor.main $(DIR) --print
