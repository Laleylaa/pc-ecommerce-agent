.DEFAULT_GOAL := help
UV ?= uv
PYTHON_VERSION ?= 3.11

ifneq ($(strip $(RUNTIME)),)
ifneq ($(words $(RUNTIME)),1)
$(error RUNTIME must be one of legacy or strands)
endif
ifneq ($(filter $(RUNTIME),legacy strands),$(RUNTIME))
$(error RUNTIME must be legacy or strands)
endif
RUNTIME_ENV := AGENT_RUNTIME=$(RUNTIME)
endif
BACKEND_HOST ?= 127.0.0.1
BACKEND_PORT ?= 8000
DATASET_DIR ?= .cache/pc-part-dataset
DATASET_JSON_DIR := $(DATASET_DIR)/data/json
PART_TYPES := case-accessory,case-fan,case,cpu-cooler,cpu,external-hard-drive,fan-controller,headphones,internal-hard-drive,keyboard,memory,monitor,motherboard,mouse,optical-drive,os,power-supply,sound-card,speakers,thermal-paste,ups,video-card,webcam,wired-network-card,wireless-network-card

.PHONY: help doctor install setup-local config-check backend-install frontend-install backend frontend infra-up infra-stop infra-status db-init db-status db-migrate db-seed dataset data-import legacy-data-import knowledge-sync knowledge-index model-prepare backend-test agent-loop-test frontend-build check tools-test application-test legacy-test integration-test model-eval

help:
	@echo "trace-up/trace-stop/trace-status  Local Phoenix; independent persistent volume"
	@echo "trace-demo      Synthetic runtime -> local Phoenix only; no LLM/database"
	@echo "doctor          Check local tools and Podman connectivity (read-only)"
	@echo "install         Install locked backend and frontend dependencies"
	@echo "setup-local     First-time setup: install, validate config, init data/model"
	@echo "infra-up        Start PostgreSQL, Redis, Chroma and wait for readiness"
	@echo "infra-stop      Stop infrastructure; preserve containers and data"
	@echo "infra-status    Show configured container status"
	@echo "backend         Start backend; optional RUNTIME=strands|legacy (default strands; env overrides apply)"
	@echo "frontend        Start frontend (separate terminal)"
	@echo "db-status       Show applied and available migrations (read-only)"
	@echo "db-migrate      Apply migrations; changes target database"
	@echo "db-init         Apply migrations, import demo catalog, seed, sync Chroma"
	@echo "model-prepare   Download/warm the local knowledge embedding model"
	@echo "agent-loop-test Run offline loop tests (tools-test/application-test also available)"
	@echo "legacy-test     Explicit offline legacy Graph checks"
	@echo "integration-test Real isolated database writes; explicit only"
	@echo "model-eval      Real model comparison; consumes API credits; explicit only"
	@echo "check           Run offline current tests, lint and frontend build"

doctor:
	./scripts/doctor.sh

# Explicit recipes preserve order even when callers use make -j.
install:
	$(MAKE) backend-install
	$(MAKE) frontend-install

setup-local:
	$(MAKE) doctor
	$(MAKE) install
	$(MAKE) config-check
	$(MAKE) infra-up
	$(MAKE) db-init
	$(MAKE) model-prepare
	@echo "Setup complete. Run make backend and make frontend in separate terminals."

backend-install:
	@command -v $(UV) >/dev/null || { echo "Install uv first; see README."; exit 1; }
	cd backend && $(UV) sync --locked --extra dev --python $(PYTHON_VERSION)

frontend-install:
	@node -e 'if (Number(process.versions.node.split(".")[0]) < 22) { console.error("Node.js 22+ required; recommended: .nvmrc"); process.exit(1); }'
	cd frontend && npm ci

config-check:
	cd backend && .venv/bin/python -m scripts.check_local_config

backend:
	cd backend && $(RUNTIME_ENV) .venv/bin/uvicorn app.main:app --reload --host $(BACKEND_HOST) --port $(BACKEND_PORT)

frontend:
	cd frontend && npm run dev

infra-up:
	./scripts/podman-infra.sh up

infra-stop:
	./scripts/podman-infra.sh stop

infra-status:
	./scripts/podman-infra.sh ps

db-status:
	cd backend && .venv/bin/alembic current
	cd backend && .venv/bin/alembic heads

db-init:
	$(MAKE) db-migrate
	$(MAKE) data-import
	$(MAKE) db-seed
	$(MAKE) knowledge-sync

db-migrate:
	cd backend && .venv/bin/alembic upgrade head

db-seed:
	cd backend && .venv/bin/python -m scripts.seed_demo

dataset:
	test -d "$(DATASET_DIR)" || (mkdir -p "$(dir $(DATASET_DIR))" && git clone https://github.com/docyx/pc-part-dataset.git "$(DATASET_DIR)")

data-import:
	cd backend && .venv/bin/python -m scripts.import_compact_catalog

legacy-data-import: dataset
	cd backend && .venv/bin/python -m scripts.import_pc_part_dataset "$(abspath $(DATASET_JSON_DIR))" --part-types "$(PART_TYPES)"

knowledge-sync:
	cd backend && .venv/bin/python -m scripts.sync_knowledge

knowledge-index:
	cd backend && .venv/bin/python -m scripts.build_knowledge_vector_index

model-prepare:
	cd backend && .venv/bin/python -m scripts.prepare_knowledge_model

# All default checks are offline. Explicit legacy is excluded from this set.
backend-test:
	backend/.venv/bin/python tests/run.py -q
	backend/.venv/bin/ruff check --config backend/pyproject.toml backend/app backend/scripts tests evals

agent-loop-test:
	backend/.venv/bin/python tests/run.py tests/agent_loop -q

tools-test:
	backend/.venv/bin/python tests/run.py tests/tools -q

application-test:
	backend/.venv/bin/python tests/run.py tests/application -q

legacy-test:
	backend/.venv/bin/python tests/run.py tests/legacy -q

# Explicit, potentially destructive/paid entry points. Never dependencies of check.
integration-test:
	PYTHONPATH=backend:.:tests backend/.venv/bin/python -m tests.integration.verify_postgres $(ARGS)

model-eval:
	PYTHONPATH=backend:.:tests backend/.venv/bin/python -m evals.agent.compare_runtimes $(ARGS)

frontend-build:
	cd frontend && npm run build

check:
	$(MAKE) backend-test
	$(MAKE) frontend-build

.PHONY: trace-up trace-stop trace-status trace-demo
trace-up:
	./scripts/podman-trace.sh up

trace-stop:
	./scripts/podman-trace.sh stop

trace-status:
	./scripts/podman-trace.sh status

trace-demo:
	PYTHONPATH=backend:.:tests backend/.venv/bin/python -m tests.trace_demo
