.PHONY: clean setup run db lint user security-check \
        ensure-pip pip-compile autoupgrade \
        celery revision downgrade deploy \
		frontend embedder embedder-worker docs \
		agent agent-check

MERMAID_FILTER_RENDERER ?= mmdc

EMBEDDER_POOL ?= solo
EMBEDDER_CONCURRENCY ?= 1

venv/bin/activate:
	uv venv venv --python=python3.11

init: ## prepare project
	. venv/bin/activate && pre-commit install
	@echo "Updating pre-commit hooks..."
	. venv/bin/activate && pre-commit autoupdate

run: venv/bin/activate requirements/dev.txt ## run vchat
	@. venv/bin/activate && watchmedo auto-restart -d vchat -R --patterns="*.py;*.txt" make devrun

devrun:
	@. venv/bin/activate && python -X dev entry.py

db: venv/bin/activate ## create db
	. venv/bin/activate && alembic upgrade head

revision: venv/bin/activate ## create db revision
	. venv/bin/activate && python entry.py --revision

downgrade: venv/bin/activate ## downgrade db
	. venv/bin/activate && python entry.py --downgrade

user: venv/bin/activate ## create user interactively
	. venv/bin/activate && python entry.py --create-user

setup: venv/bin/activate  ## setup python environment
	. venv/bin/activate && uv pip sync requirements/dev.txt
	. venv/bin/activate && python entry.py --model

ensure-pip: venv/bin/activate ## ensure bundled pip is available inside the venv
	. venv/bin/activate && python -m ensurepip --upgrade

pip-compile: ensure-pip  ## compile dependencies
	. venv/bin/activate && pip-compile --generate-hashes -v --unsafe-package pip requirements/requirements.in -o requirements/requirements.txt
	. venv/bin/activate && pip-compile --generate-hashes -v --unsafe-package pip requirements/dev.in -o requirements/dev.txt

pip-linux: ## compile dependencies inside Linux container so darwin-only wheel dependencies (ocrmac etc.) are skipped
	docker build -t pusk-pip-compile -f docker/pip-compile.Dockerfile .
	docker run --rm -v $(PWD):/workspace -w /workspace pusk-pip-compile sh -c "\
		pip-compile --generate-hashes --unsafe-package pip requirements/requirements.in -o requirements/requirements.txt && \
		pip-compile --generate-hashes --unsafe-package pip requirements/dev.in -o requirements/dev.txt"
	make setup

autoupgrade: ensure-pip ## upgrade dependencies
	. venv/bin/activate && pre-commit autoupdate
	. venv/bin/activate && pip-compile --upgrade --generate-hashes -v --allow-unsafe requirements/requirements.in -o requirements/requirements.txt
	. venv/bin/activate && pip-compile --upgrade --generate-hashes -v --allow-unsafe requirements/dev.in -o requirements/dev.txt

# Celery and tasks
celery: venv/bin/activate ## start celery (default queue + beat)
	@HOST=$$(hostname -s); \
	NODE_NAME=$${CELERY_NODENAME:-vchat-celery-$${HOST}-$$$$@$${HOST}}; \
	. venv/bin/activate && celery -A jobs.celery worker --beat --loglevel=DEBUG -Q celery -n "$$NODE_NAME"

embedder: venv/bin/activate ## start dedicated embedder workers for this host
	. venv/bin/activate && PYTHONMALLOC=malloc python -m jobs.embedder.launcher

embedder-worker: venv/bin/activate ## start a single dedicated embedder worker
	@HOST=$$(hostname -s); \
	NODE_NAME=$${EMBEDDER_NODENAME:-vchat-embedder-$${HOST}-$$$$-$${EMBEDDER_INSTANCE_INDEX:-1}@$${HOST}}; \
	. venv/bin/activate && celery -A jobs.celery worker --loglevel=INFO -Q embeddings --pool=$(EMBEDDER_POOL) --concurrency=$(EMBEDDER_CONCURRENCY) --max-tasks-per-child=1 -n "$$NODE_NAME"

celery_stop: venv/bin/activate ## stop celery
	. venv/bin/activate && celery -A jobs.celery control shutdown

task: venv/bin/activate  ## run a task
	. venv/bin/activate && celery -A jobs.celery call ${@: 1}

lint: venv/bin/activate ## run linter
	. venv/bin/activate && pre-commit run --hook-stage manual --files vchat jobs entry.py
	. venv/bin/activate && ruff check --fix vchat jobs entry.py
	. venv/bin/activate && ruff format vchat/ jobs/ entry.py
# 	. venv/bin/activate && mypy vchat jobs entry.py

security-check: ## run local Docker-based security checks
	./bin/security-check.sh

frontend: venv/bin/activate ## build frontend
	cd frontend && make deploy
	cd frontend_chat && make deploy

deploy: venv/bin/activate  ## deploy on server project
	. venv/bin/activate && uv pip sync requirements/requirements.txt --link-mode=copy
	@if [ "$$(uname -s)" = "Linux" ]; then \
	  echo "Installing Linux-specific requirements..."; \
	  . venv/bin/activate && uv pip install -r requirements/req_linux.txt --link-mode=copy; \
	fi
	. venv/bin/activate && python entry.py --model
	. venv/bin/activate && alembic upgrade head

tunnel:  ## make tunnel to prod database, use `psql postgresql://localhost:54327 -d vchat` to connect
	@echo "Start tunnel, keep this running...";
	@while true; do \
	  AUTOSSH_GATETIME=0 autossh -M 0 -N -o "ServerAliveInterval=10" -o "ServerAliveCountMax=3" \
	  -o "ExitOnForwardFailure=yes" \
	  -o "TCPKeepAlive=yes" \
	  -o "ServerAliveInterval=10" \
	  -o "ServerAliveCountMax=3" \
	  -o "ConnectTimeout=10" \
	  -L 54327:localhost:5432 deploy@vchat.com; \
	  echo "SSH tunnel dropped, reconnecting in 5 seconds..."; \
	  sleep 5; \
	done

ssl:  ## listen for vchat.me on localhost:443
	@echo "Starting Caddy server..."
	-@caddy stop
	caddy run --config Caddyfile

clean: ## cleanup project
	@echo "Cleaning up..."
	@-rm -rf .tox
	@-rm -rf .pytest_cache
	@-rm -rf *.egg-info
	@-find . -name '__pycache__' -prune -exec rm -rf "{}" \;
	@-find . -name '*.pyc' -delete
	@-rm -f MANIFEST
	@-rm -rf .coverage .coverage.* htmlcov
	@echo "Removing deleted branches from origin"
	@git remote prune origin
	@echo "Removing merged branches..."
	@branches=$$(git branch --merged master --no-contains master --format="%(refname:short)"); \
	if [ -n "$$branches" ]; then \
		git branch --delete $$branches; \
		echo "...Deleted $$branches"; \
	else \
		echo "...No branches to delete"; \
	fi

docs: ## convert all docs in docs/ to .docx
	@mkdir -p docs/word
	@set -e; \
	for f in docs/*.md; do \
		echo "Converting $$f to docs/word/$$(basename "$${f%.md}.docx")"; \
		MERMAID_FILTER_WIDTH=400 \
		MERMAID_FILTER_SCALE=3 \
		MERMAID_FILTER_IMAGE_WIDTH=72% \
		MERMAID_FILTER_FORCE_VERTICAL=1 \
		MERMAID_FILTER_FORCE_C4_ONE_COLUMN=1 \
		MERMAID_FILTER_RENDERER=$(MERMAID_FILTER_RENDERER) \
		PUPPETEER_EXECUTABLE_PATH=$$(find $(HOME)/.cache/puppeteer/chrome-headless-shell -name chrome-headless-shell -type f | sort | tail -n 1) \
		pandoc "$$f" \
			-f markdown \
			-t docx \
			--reference-doc=docs/template.dotx \
			--standalone \
			--lua-filter ./bin/pandoc-table-br.lua \
			--filter ./bin/mermaid-filter-fit.js \
			-o "docs/word/$$(basename "$${f%.md}.docx")"; \
	done

test: venv/bin/activate ## run tests with coverage
	venv/bin/pytest -v --tb=short --disable-warnings \
		--cov=vchat \
		--cov=jobs \
		--cov-report=term-missing:skip-covered \
		--cov-report=xml:coverage.xml \
		--cov-report=html:htmlcov

agent-check: test ## run the standard agent completion checks

agent: agent-check ## alias for the standard agent completion ritual

help: ## display this help message
	@echo "Please use \`make <target>' where <target> is one of"
	@grep '^[a-zA-Z]' $(MAKEFILE_LIST) | sort | awk -F ':.*?## ' 'NF==2 {printf "\033[36m  %-25s\033[0m %s\n", $$1, $$2}'
