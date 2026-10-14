SDK_PATH ?= ../prometheus_core/sdk
.PHONY: dev-setup test lint
dev-setup:
	uv sync
	uv pip install -e "$(SDK_PATH)"

test:
	uv run pytest

lint:
	uv run ruff check .

# Set CONTAINER_RUNTIME=docker or CONTAINER_RUNTIME=podman to override the
# automatic selection. Prefer a reachable Docker daemon; otherwise use Podman.
CONTAINER_RUNTIME ?= $(shell if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then printf docker; elif command -v podman >/dev/null 2>&1; then printf podman; else printf docker; fi)

# Avoid stale Docker Desktop credential helpers when Podman invokes Compose.
COMPOSE_CONFIG := $(if $(DOCKER_CONFIG),DOCKER_CONFIG=$(DOCKER_CONFIG),DOCKER_CONFIG=$$(mktemp -d))
COMPOSE := $(COMPOSE_CONFIG) $(CONTAINER_RUNTIME) compose

UNAME   := $(shell uname)

COMPOSE_BASE := docker-compose.yml
COMPOSE_NET_INTERNAL := docker-compose.net_internal.yml
COMPOSE_NET_EXTERNAL := docker-compose.net_external.yml

# Read only the external-mode selectors from .env. Docker Compose still reads
# .env itself for service configuration interpolation.
define read_dotenv
$(strip $(shell if [ -f .env ]; then sed -n 's/^[[:space:]]*$(1)[[:space:]]*=[[:space:]]*//p' .env | tail -n 1; fi))
endef

EXTERNAL_AGENT_HOST ?= $(call read_dotenv,EXTERNAL_AGENT_HOST)
EXTERNAL_AGENT_PORT ?= $(call read_dotenv,EXTERNAL_AGENT_PORT)
export EXTERNAL_AGENT_HOST EXTERNAL_AGENT_PORT

HAS_EXTERNAL_AGENT_HOST := $(if $(strip $(EXTERNAL_AGENT_HOST)),yes,no)
HAS_EXTERNAL_AGENT_PORT := $(if $(strip $(EXTERNAL_AGENT_PORT)),yes,no)

ifeq ($(HAS_EXTERNAL_AGENT_HOST)-$(HAS_EXTERNAL_AGENT_PORT),no-no)
NETWORK_MODE := internal
COMPOSE_NET := -f $(COMPOSE_NET_INTERNAL)
else ifeq ($(HAS_EXTERNAL_AGENT_HOST)-$(HAS_EXTERNAL_AGENT_PORT),yes-yes)
NETWORK_MODE := external
COMPOSE_NET := -f $(COMPOSE_NET_EXTERNAL)
else
NETWORK_MODE := invalid
COMPOSE_NET :=
endif

COMPOSE_LINUX :=
ifeq ($(NETWORK_MODE),external)
  ifeq ($(UNAME), Linux)
    COMPOSE_LINUX := $(if $(wildcard docker-compose.linux.yml),-f docker-compose.linux.yml,)
  endif
endif

COMPOSE_FILES := $(strip -f $(COMPOSE_BASE) $(COMPOSE_NET) $(COMPOSE_LINUX))

.PHONY: help validate-network up up-fg down logs

help: ##@ (Default) Print listing of key targets with their descriptions
	@printf "\nUsage: make <command>\n"
	@grep -F -h "##@" $(MAKEFILE_LIST) | grep -Fv grep | grep -Fv 'FS =' | sed 's/\\$$//' | \
	awk 'BEGIN {FS = ":*[[:space:]]*##@[[:space:]]*"}; \
	{ \
		if ($$2 == "") next; \
		else if ($$0 ~ /^#/) printf "\n\n%s\n", $$2; \
		else if ($$1 == "") printf "     %-20s%s\n", "", $$2; \
		else printf "\n    \033[34m%-20s\033[0m %s", $$1, $$2; \
	}'
	@printf "\n\n"

##@
##@ Compose
##@

validate-network:
ifeq ($(NETWORK_MODE),invalid)
	$(error EXTERNAL_AGENT_HOST and EXTERNAL_AGENT_PORT must be set together, or neither set)
else ifeq ($(NETWORK_MODE),internal)
	@if [ -z "$$($(CONTAINER_RUNTIME) network ls -q -f name=^prometheus_platform_agent-network$$)" ]; then \
		echo "\nThe Prometheus Platform must be launched first when internal network mode is selected."; \
		echo "For standalone agent testing/debugging launch with:"; \
		echo "EXTERNAL_AGENT_HOST=localhost EXTERNAL_AGENT_PORT=8000 make $(MAKECMDGOALS)\n"; \
		exit 1; \
	fi
endif

up: ##@ Build and start the agent (detached)
up: validate-network
	$(COMPOSE) $(COMPOSE_FILES) up --build -d

up-fg: ##@ Build and start the agent (foreground)
up-fg: validate-network
	$(COMPOSE) $(COMPOSE_FILES) up --build

down: ##@ Stop and remove the agent
down: validate-network
	$(COMPOSE) $(COMPOSE_FILES) down

logs: ##@ Tail agent logs (SERVICE=name for one service)
logs: validate-network
	$(COMPOSE) $(COMPOSE_FILES) logs -f $(SERVICE)
