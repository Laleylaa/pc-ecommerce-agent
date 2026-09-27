.DEFAULT_GOAL := help
UV ?= uv
PYTHON_VERSION ?= 3.11
BACKEND_HOST ?= 127.0.0.1
BACKEND_PORT ?= 8000
DATASET_DIR ?= .cache/pc-part-dataset
DATASET_JSON_DIR := $(DATASET_DIR)/data/json
PART_TYPES := case-accessory,case-fan,case,cpu-cooler,cpu,external-hard-drive,fan-controller,headphones,internal-hard-drive,keyboard,memory,monitor,motherboard,mouse,optical-drive,os,power-supply,sound-card,speakers,thermal-paste,ups,video-card,webcam,wired-network-card,wireless-network-card

.PHONY: help doctor install setup-local config-check backend-install frontend-install backend frontend infra-up infra-stop infra-status db-init db-status db-migrate db-seed dataset data-import legacy-data-import knowledge-sync knowledge-index model-prepare backend-test frontend-build check

help:
	@echo "doctor          Check local tools and Podman connectivity (read-only)"
	@echo "install         Install locked backend and frontend dependencies"
	@echo "setup-local     First-time setup: install, validate config, init data/model"
	@echo "infra-up        Start PostgreSQL, Redis, Chroma and wait for readiness"
	@echo "infra-stop      Stop infrastructure; preserve containers and data"
	@echo "infra-status    Show configured container status"
	@echo "backend         Start backend (separate terminal, no DB initialization)"
	@echo "frontend        Start frontend (separate terminal)"
	@echo "db-status       Show applied and available migrations (read-only)"
	@echo "db-migrate      Apply migrations; changes target database"
	@echo "db-init         Apply migrations, import demo catalog, seed, sync Chroma"
	@echo "model-prepare   Download/warm the local knowledge embedding model"
	@echo "check           Run backend tests/lint and frontend build"

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
	cd backend && .venv/bin/uvicorn app.main:app --reload --host $(BACKEND_HOST) --port $(BACKEND_PORT)

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

backend-test:
	cd backend && .venv/bin/pytest && .venv/bin/ruff check .

frontend-build:
	cd frontend && npm run build

check:
	$(MAKE) backend-test
	$(MAKE) frontend-build
