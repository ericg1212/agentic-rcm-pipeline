.PHONY: help up down restart logs topics status producer consumer install lint test snowflake-init

COMPOSE_FILE = infra/docker-compose.yml
PYTHON ?= python

help:
	@echo "P4 Agentic RCM Prevention Pipeline"
	@echo ""
	@echo "  make up              Start Kafka + Schema Registry + UI"
	@echo "  make down            Stop and remove containers"
	@echo "  make restart         Restart all services"
	@echo "  make logs            Tail logs from all services"
	@echo "  make topics          List Kafka topics and partition info"
	@echo "  make status          Show container health"
	@echo "  make producer        Run the live claim event generator"
	@echo "  make consumer        Run the NCCI gate consumer"
	@echo "  make install         Install Python dependencies"
	@echo "  make lint            Run ruff + bandit"
	@echo "  make test            Run pytest suite"
	@echo "  make snowflake-init  Run Snowflake RAW schema DDL"

up:
	docker compose -f $(COMPOSE_FILE) up -d
	@echo "Kafka UI: http://localhost:8080"
	@echo "Schema Registry: http://localhost:8081"

down:
	docker compose -f $(COMPOSE_FILE) down -v

restart:
	docker compose -f $(COMPOSE_FILE) restart

logs:
	docker compose -f $(COMPOSE_FILE) logs -f

topics:
	docker exec rcm-kafka kafka-topics.sh \
		--bootstrap-server localhost:9092 \
		--describe

status:
	docker compose -f $(COMPOSE_FILE) ps

producer:
	$(PYTHON) -m src.generator.producer

consumer:
	$(PYTHON) -m src.consumer.claim_consumer

install:
	$(PYTHON) -m pip install -r requirements.txt

lint:
	$(PYTHON) -m ruff check src/ tests/
	$(PYTHON) -m bandit -r src/ -ll

test:
	$(PYTHON) -m pytest tests/ -v --cov=src --cov-report=term-missing

snowflake-init:
	@echo "Run the DDL in Snowflake Worksheets:"
	@echo "  File: snowflake/raw/ddl.sql"
	@echo "  Account: gl20220 (app.snowflake.com/ca-central-1.aws/gl20220)"
