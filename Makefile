.PHONY: help install lint fmt typecheck test cov check demo demo-explain demo-classify \
	demo-ledger demo-prs demo-report record-prs rules-doc docker verify-recording

UV ?= uv
IMAGE ?= commitminer:local
HISTORY := examples/tomli/history.jsonl.gz
TOMLI_URL := https://github.com/hukkin/tomli
TOMLI_REV := 5a77b12a7a9f052ce5a20c335d2825658f6aea52
WORK := .commitminer

help: ## List the targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-17s %s\n", $$1, $$2}'

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

demo: ## Mine the recorded tomli history offline; writes out/tomli-candidates.jsonl
	$(UV) run commitminer mine --history $(HISTORY) --top 10 --explain 1 \
		--out out/tomli-candidates.jsonl

demo-explain: ## Explain one candidate and one rejected commit of the recorded tomli history
	$(UV) run commitminer explain 948211d852 --history $(HISTORY)
	@echo
	$(UV) run commitminer explain 27be26fa4d --history $(HISTORY)

CLASSIFY_ROOT := examples/classify
CLASSIFY_PATHS := src/lib.rs internal/kind/kind_string.go web/static/bundle.js \
	web/src/app.test.ts vendor/github.com/acme/left/left.go fixtures/percent.json \
	tools/cli/main.go Cargo.lock README.md

demo-classify: ## Classify the multi-language sample tree (uses its commitminer.toml)
	$(UV) run commitminer classify --root $(CLASSIFY_ROOT) $(CLASSIFY_PATHS)

LEDGER_DEMO := $(WORK)/ledger-demo
LEDGER := $(LEDGER_DEMO)/ledger.sqlite3

demo-ledger: ## Claim upstream fixes, then mine a release branch and a fork against the ledger
	rm -rf $(LEDGER_DEMO)
	$(UV) run python examples/ledger/build_repos.py $(LEDGER_DEMO)
	$(UV) run commitminer mine $(LEDGER_DEMO)/upstream --repo-name demo/durations --explain 0 \
		--out $(LEDGER_DEMO)/upstream.jsonl
	$(UV) run commitminer ledger add $(LEDGER) $(LEDGER_DEMO)/upstream.jsonl --owner alice
	$(UV) run commitminer mine $(LEDGER_DEMO)/upstream --rev release --repo-name demo/durations \
		--explain 0 --ledger $(LEDGER)
	$(UV) run commitminer mine $(LEDGER_DEMO)/fork --repo-name demo/durations-fork --explain 0 \
		--ledger $(LEDGER)
	$(UV) run commitminer ledger list $(LEDGER)

PRS := examples/tomli/prs
PRS_DEMO := $(WORK)/prs-demo
PRS_ARGS := hukkin/tomli --limit 25 --replay $(PRS)

demo-prs: ## Rank tomli's merged pull requests from recorded API responses (offline)
	rm -rf $(PRS_DEMO)
	$(UV) run commitminer prs $(PRS_ARGS) --cache-dir $(PRS_DEMO)/cache --top 10 --explain 1 \
		--out out/tomli-prs.jsonl
	@echo
	@echo "Again with the same cache: every request is conditional and answered 304."
	$(UV) run commitminer prs $(PRS_ARGS) --cache-dir $(PRS_DEMO)/cache --top 0 --explain 0
	@echo
	@echo "The same fixes mined from the commit history are already in a ledger:"
	$(UV) run commitminer mine --history $(HISTORY) --top 0 --explain 0 \
		--out $(PRS_DEMO)/commits.jsonl
	$(UV) run commitminer ledger add $(PRS_DEMO)/ledger.sqlite3 $(PRS_DEMO)/commits.jsonl \
		--owner demo > $(PRS_DEMO)/ledger-add.txt && tail -1 $(PRS_DEMO)/ledger-add.txt
	$(UV) run commitminer prs $(PRS_ARGS) --ledger $(PRS_DEMO)/ledger.sqlite3 --top 0 --explain 0

EXPORT := out/tomli-candidates.jsonl

demo-report: ## Export the recorded tomli history and render it as Markdown and HTML reports
	$(UV) run commitminer mine --history $(HISTORY) --top 0 --explain 0 --out $(EXPORT)
	$(UV) run commitminer report $(EXPORT) --out out/tomli-report.md
	$(UV) run commitminer report $(EXPORT) --out out/tomli-report.html
	@echo
	@sed -n '1,22p' out/tomli-report.md

record-prs: ## Re-record the pull-request fixtures from GitHub (network; GITHUB_TOKEN optional)
	rm -f $(PRS)/*.json
	$(UV) run commitminer prs hukkin/tomli --limit 25 --record $(PRS) --top 0 --explain 0

rules-doc: ## Regenerate the rule table in docs/rules.md
	{ sed '/^| # | rule |/,$$d' docs/rules.md; $(UV) run commitminer rules --markdown; } \
		> docs/rules.md.tmp && mv docs/rules.md.tmp docs/rules.md

docker: ## Build the image, run the demo in it, prune this project's dangling images
	docker build -t $(IMAGE) .
	docker run --rm $(IMAGE) mine --history $(HISTORY) --top 5 --explain 1
	docker run --rm --network none $(IMAGE) prs $(PRS_ARGS) --top 5 --explain 0
	docker run --rm --entrypoint sh $(IMAGE) -c 'commitminer mine --history $(HISTORY) --top 0 \
		--explain 0 --out /tmp/c.jsonl && commitminer report /tmp/c.jsonl --top 3 | head -20'
	docker image prune -f --filter label=project=commitminer

verify-recording: ## Re-record tomli from GitHub and compare with the bundled file (network)
	rm -rf $(WORK)/tomli $(WORK)/verify
	git clone -q $(TOMLI_URL) $(WORK)/tomli
	$(UV) run commitminer record $(WORK)/tomli --rev $(TOMLI_REV) --repo-name hukkin/tomli \
		--url $(TOMLI_URL) --out $(WORK)/verify/history.jsonl
	gzip -dc $(HISTORY) | cmp - $(WORK)/verify/history.jsonl
	@echo "recording matches $(TOMLI_URL) at $(TOMLI_REV)"
