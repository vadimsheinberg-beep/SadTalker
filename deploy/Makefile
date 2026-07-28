SHELL := /bin/bash
CONTROL := docker compose -f docker-compose.control.yml
GPU := docker compose -f docker-compose.gpu.yml

.PHONY: help env up down logs ps smoke gpu-up gpu-down clean

help:
	@grep -E '^[a-z-]+:.*?##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/'

env: ## создать .env из шаблона
	@test -f .env || (cp .env.example .env && echo "создан .env — заполните секреты")

up: env ## поднять control plane (Contabo VPS, GPU не нужен)
	$(CONTROL) up -d --build
	@echo "оркестратор: http://127.0.0.1:8080/status"

down: ## остановить control plane
	$(CONTROL) down

logs: ## логи control plane
	$(CONTROL) logs -f --tail=100

ps: ## состояние сервисов
	$(CONTROL) ps

smoke: ## проверить, что конвейер и политика работают end-to-end
	python3 smoke_test.py

gpu-up: ## поднять GPU-ноду (запускать на хосте с NVIDIA)
	$(GPU) up -d --build

gpu-down:
	$(GPU) down

clean: ## снести всё вместе с томами
	$(CONTROL) down -v
