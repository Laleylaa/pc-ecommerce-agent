.DEFAULT_GOAL := help
UV ?= uv
PYTHON_VERSION := 3.11
# Command-line overrides take precedence over Settings/.env.
ifneq ($(origin BACKEND_HOST),undefined)
export BACKEND_HOST
endif
ifneq ($(origin BACKEND_PORT),undefined)
export BACKEND_PORT
endif
ifneq ($(origin FRONTEND_PORT),undefined)
export FRONTEND_PORT
endif
ifneq ($(origin FRONTEND_HOST),undefined)
export FRONTEND_HOST
endif

.PHONY: help doctor install setup-local config-check backend-install frontend-install backend frontend infra-up infra-stop infra-status db-init db-status db-migrate demo-load demo-info knowledge-index model-prepare frontend-build spec-display spec-display-check

help:
	@echo "doctor / config-check        Check tools and configuration"
	@echo "install / setup-local        Install runtime / first-time empty-database setup"
	@echo "infra-up / infra-stop        Start/stop PostgreSQL; preserve data"
	@echo "infra-status / db-status     Inspect services and migration version"
	@echo "db-init / demo-info          Migrate + load fresh demo / inspect demo inputs"
	@echo "backend / frontend          Start application (separate terminals)"
	@echo "model-prepare               Prepare local policy embedding model"

doctor:
	./scripts/doctor.sh

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
	cd backend && $(UV) sync --locked --no-dev --python $(PYTHON_VERSION)

frontend-install:
	@node -e 'if (Number(process.versions.node.split(".")[0]) < 22) { console.error("Node.js 22+ required; recommended: .nvmrc"); process.exit(1); }'
	cd frontend && npm ci

config-check:
	cd backend && .venv/bin/python -m scripts.check_local_config

backend:
	PYTHONPATH=backend backend/.venv/bin/python -m scripts.run_local backend

frontend:
	PYTHONPATH=backend backend/.venv/bin/python -m scripts.run_local frontend

infra-up:
	./scripts/podman-infra.sh up

infra-stop:
	./scripts/podman-infra.sh stop

infra-status:
	./scripts/podman-infra.sh ps

db-status:
	cd backend && .venv/bin/alembic current

db-migrate:
	cd backend && .venv/bin/alembic upgrade head

db-init:
	$(MAKE) db-migrate
	$(MAKE) demo-load

demo-load:
	cd backend && .venv/bin/python -m scripts.demo.load

demo-info:
	cd backend && .venv/bin/python -m scripts.demo.load --summary

knowledge-index:
	cd backend && .venv/bin/python -m scripts.build_knowledge_vector_index

model-prepare:
	cd backend && .venv/bin/python -m scripts.prepare_knowledge_model

spec-display:
	cd backend && .venv/bin/python -m scripts.export_spec_display

spec-display-check:
	cd backend && .venv/bin/python -m scripts.export_spec_display --check

frontend-build: spec-display-check
	cd frontend && npm run build
