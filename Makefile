.PHONY: clean setup run db lint \
        ensure-pip pip-compile autoupgrade \
        celery revision downgrade deploy lang \
		add-lang frontend embedder

SH = . venv/bin/activate &&

venv/bin/activate:
	uv venv venv --python=python3.11 -q

init: ## prepare project
	$(SH) pre-commit install
	@echo "Updating pre-commit hooks..."
	$(SH) pre-commit autoupdate

run: venv/bin/activate requirements/dev.txt ## run core
	@$(SH) watchmedo auto-restart -d core -R --patterns="*.py;*.txt" make devrun

devrun:
	@$(SH) python -X dev entry.py

db: venv/bin/activate ## create db
	$(SH) alembic upgrade head

revision: venv/bin/activate ## create db revision
	$(SH) python entry.py --revision

downgrade: venv/bin/activate ## downgrade db
	$(SH) python entry.py --downgrade

setup: venv/bin/activate  ## setup python environment
	$(SH) uv pip sync requirements/dev.txt -q

ensure-pip: venv/bin/activate ## ensure bundled pip is available inside the venv
	$(SH) python -m ensurepip --upgrade

pip-compile: ensure-pip  ## compile dependencies
	$(SH) pip-compile --generate-hashes -v --unsafe-package pip requirements/requirements.in -o requirements/requirements.txt
	$(SH) pip-compile --generate-hashes -v --unsafe-package pip requirements/dev.in -o requirements/dev.txt

pip-linux: ## compile dependencies inside Linux container so darwin-only wheel dependencies (ocrmac etc.) are skipped
	docker build -t pusk-pip-compile -f docker/pip-compile.Dockerfile .
	docker run --rm -v $(PWD):/workspace -w /workspace pusk-pip-compile sh -c "\
		pip-compile --generate-hashes --unsafe-package pip requirements/requirements.in -o requirements/requirements.txt && \
		pip-compile --generate-hashes --unsafe-package pip requirements/dev.in -o requirements/dev.txt"
	make setup

autoupgrade: ensure-pip ## upgrade dependencies
	$(SH) pre-commit autoupdate
	$(SH) pip-compile --upgrade --generate-hashes -v --allow-unsafe requirements/requirements.in -o requirements/requirements.txt
	$(SH) pip-compile --upgrade --generate-hashes -v --allow-unsafe requirements/dev.in -o requirements/dev.txt

# Celery and tasks
celery: venv/bin/activate ## start celery (core + crawler queues)
	$(SH) celery -A jobs.celery worker --beat --loglevel=DEBUG -Q celery,crawler

embedder: venv/bin/activate ## start dedicated embedder worker
	$(SH) celery -A jobs.celery worker --loglevel=INFO -Q embeddings --pool=solo

celery_stop: venv/bin/activate ## stop celery
	$(SH) celery -A jobs.celery control shutdown

task: venv/bin/activate  ## run a task
	$(SH) celery -A jobs.celery call ${@: 1}

lint: venv/bin/activate ## run linter
	$(SH) pre-commit run --hook-stage manual --files core jobs entry.py
	$(SH) ruff check --fix core jobs entry.py
# 	$(SH) mypy core jobs entry.py

frontend: venv/bin/activate ## build frontend
	$(SH) cd frontend && make deploy
	$(SH) cd frontend_chat && make deploy

add-lang: venv/bin/activate requirements/dev.txt ## add new lang: make add-lang LANG=ru
	$(SH) pybabel init -i core/translations/messages.pot -d core/translations -l $(LANG)

lang: venv/bin/activate requirements/dev.txt ## update lang
	$(SH) pybabel extract -F babel.cfg -o core/translations/messages.pot .
	$(SH) pybabel update -i core/translations/messages.pot -d core/translations
	$(SH) pybabel compile -d core/translations

translate: venv/bin/activate requirements/dev.txt ## translate using AI
	$(SH) pybabel extract -F babel.cfg -o core/translations/messages.pot .
	$(SH) pybabel update -i core/translations/messages.pot -d core/translations
	$(SH) python bin/translate.py core/translations
	$(SH) pybabel update -i core/translations/messages.pot -d core/translations
	$(SH) pybabel compile -d core/translations

deploy: venv/bin/activate  ## deploy on server project
	$(SH) uv pip sync requirements/requirements.txt --link-mode=copy
	@if [ "$$(uname -s)" = "Linux" ]; then \
	  echo "Installing Linux-specific requirements..."; \
	  $(SH) uv pip install -r requirements/req_linux.txt --link-mode=copy; \
	fi
	$(SH) alembic upgrade head
	$(SH) pybabel compile -d core/translations

tunnel:  ## make tunnel to prod database, use `psql postgresql://localhost:54327 -d core` to connect
	@echo "Start tunnel, keep this running...";
	@while true; do \
	  AUTOSSH_GATETIME=0 autossh -M 0 -N -o "ServerAliveInterval=10" -o "ServerAliveCountMax=3" \
	  -o "ExitOnForwardFailure=yes" \
	  -o "TCPKeepAlive=yes" \
	  -o "ServerAliveInterval=10" \
	  -o "ServerAliveCountMax=3" \
	  -o "ConnectTimeout=10" \
	  -L 54327:localhost:5432 deploy@core.com; \
	  echo "SSH tunnel dropped, reconnecting in 5 seconds..."; \
	  sleep 5; \
	done

ssl:  ## listen for core.me on localhost:443
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
	else \
		echo "...No branches to delete"; \
	fi

help: ## display this help message
	@echo "Please use \`make <target>' where <target> is one of"
	@grep '^[a-zA-Z]' $(MAKEFILE_LIST) | sort | awk -F ':.*?## ' 'NF==2 {printf "\033[36m  %-25s\033[0m %s\n", $$1, $$2}'
