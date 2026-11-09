# DOCI -- operator shortcuts
#
# `make help` lists everything. The deploy / logs / shell targets assume
# DOCI is running under /srv/doci with the standard compose
# layout (docker-compose.yml for prod, docker-compose.dev.yml for local).
# Override PROJECT_DIR for other hosts.

PROJECT_DIR     ?= $(CURDIR)
PROD_PROJECT    ?= /srv/doci
COMPOSE_DEV     = docker compose --project-directory $(PROJECT_DIR) -f $(PROJECT_DIR)/docker-compose.dev.yml
COMPOSE_PROD    = docker compose --project-directory $(PROD_PROJECT) -f $(PROD_PROJECT)/docker-compose.yml
APP_CT          = doci
DB_CT_DEV       = doci-db
DOCI_API_KEY    ?= dev-test-key-not-for-production

.DEFAULT_GOAL := help
.PHONY: help install lint analyse test api-test benchmark up down build deploy logs ps shell psql wipe-demo seed-demo clean

help: ## Show this help
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST) | sort

install: ## Install composer deps into vendor/
	composer install --no-interaction --prefer-dist

lint: ## php -l on every PHP source file
	@find lib api scripts tests -name '*.php' -type f -print0 \
	  | xargs -0 -n1 php -l > /tmp/doci-lint.log \
	  || (cat /tmp/doci-lint.log; exit 1)
	@echo "lint: OK"

analyse: ## phpstan level 5 on lib/ and api/
	composer analyse

test: ## Run PHPUnit unit suite
	composer test

api-test: ## Run tests/api-test.sh against a running dev container
	DOCI_API_KEY=$(DOCI_API_KEY) ./tests/api-test.sh

benchmark: ## Print the Live Benchmark Gate checklist (manual)
	@echo "Live Benchmark Gate (manual). See docs/06-testing-strategy.md for the contract."
	@echo "  1. Document create-edit-delete in a real browser; verify DB row, file, git commits."
	@echo "  2. Thread -> version -> reply chain; verify version bar in UI."
	@echo "  3. MCP doci_inbox call from a real client; item appears in list."
	@echo "  4. Behind a real reverse-proxy passing Remote-User; UI loads, attribution correct."
	@echo "  5. (Phase 5+) CSRF rejection: POST without token returns 403."
	@echo ""
	@echo "Record evidence in docs/live-benchmark-vX.Y.Z.md before tagging."

up: ## Start dev stack (embedded Postgres on 5433, UI on 8080)
	$(COMPOSE_DEV) up -d --build
	@sleep 3
	@curl -s -o /dev/null -w "/api/health.php HTTP %{http_code}\n" http://localhost:8080/api/health.php

down: ## Stop dev stack
	$(COMPOSE_DEV) down

build: ## Rebuild the doci image without restarting prod
	$(COMPOSE_PROD) build doci

deploy: lint analyse ## Build + restart prod container (preserves Traefik labels)
	$(COMPOSE_PROD) up -d --build doci
	@sleep 3
	@docker exec $(APP_CT) cat /var/www/html/VERSION

logs: ## Tail dev container logs
	$(COMPOSE_DEV) logs -f --tail 200 doci

ps: ## Container status
	@docker ps --filter "name=$(APP_CT)" --filter "name=$(DB_CT_DEV)" --format "table {{.Names}}\t{{.Status}}\t{{.Image}}"

shell: ## Drop into a shell inside the doci dev container
	docker exec -it $(APP_CT)-dev sh

psql: ## Open psql against the dev DB
	docker exec -it $(DB_CT_DEV) psql -U doci_app -d doci

wipe-demo: ## Clear seeded demo content from files/ and DB; ready for fresh start
	@docker exec $(APP_CT)-dev sh -c 'rm -rf /var/www/html/files/* /var/www/html/files/.data/threads/* /var/www/html/files/.data/versions/*'
	@docker exec $(DB_CT_DEV) psql -U doci_app -d doci -c 'TRUNCATE documents;' >/dev/null
	@touch $(PROJECT_DIR)/files/.gitkeep
	@echo "wiped. 'make seed-demo' or 'docker compose -f docker-compose.dev.yml restart doci' to re-seed."

seed-demo: ## Re-run the landing seed script (creates version + thread on index.md)
	@docker exec $(APP_CT)-dev sh /var/www/html/scripts/seed-demo.sh

clean: ## Remove vendor/ and composer.lock (use sparingly)
	rm -rf vendor composer.lock
