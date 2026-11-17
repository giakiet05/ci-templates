# Pinned by digest; the image bundles shellcheck, which actionlint runs on every run: block.
ACTIONLINT_IMAGE := rhysd/actionlint:1.7.12@sha256:b1934ee5f1c509618f2508e6eb47ee0d3520686341fec936f3b79331f9315667

.PHONY: lint test

# Lint the workflow files (and the shell inside them) with actionlint.
lint:
	docker run --rm -v "$(CURDIR):/repo:ro" -w /repo $(ACTIONLINT_IMAGE) -color

# Unit tests for the plan action; standard library only.
test:
	python3 -m unittest discover -s actions/plan/tests -v
