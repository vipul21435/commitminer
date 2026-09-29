.PHONY: help install lint fmt typecheck test cov check demo docker

UV ?= uv
IMAGE ?= commitminer:local

help: ## List the targets
	@grep -E '^[a-z]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

install: ## Install the locked environment and the pre-commit hook
	$(UV) sync --locked
	$(UV) run pre-commit install

lint: ## Ruff lint and format check
	$(UV) run ruff check .
	$(UV) run ruff format --check .

fmt: ## Apply ruff fixes and formatting
	$(UV) run ruff check --fix .
	$(UV) run ruff format .

typecheck: ## mypy --strict on src/
	$(UV) run mypy

test: ## Run the test suite
	$(UV) run pytest

cov: ## Run the tests with the coverage gate (fail_under in pyproject.toml)
	$(UV) run pytest --cov --cov-report=term-missing --cov-report=xml

check: lint typecheck cov ## Everything CI runs except Docker

demo: ## CLI smoke run (the core deliverable replaces this with a recorded-history demo)
	$(UV) run commitminer version
	$(UV) run commitminer --help

docker: ## Build the image, run it, prune this project's dangling images
	docker build -t $(IMAGE) .
	docker run --rm $(IMAGE) version
	docker image prune -f --filter label=project=commitminer
