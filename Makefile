.PHONY: help up down build verify test logs export-image import-image clean

IMAGE ?= live2mp3:latest
EXPORT_FILE ?= live2mp3-image.tar

help:
	@echo "live2mp3 — cibles disponibles :"
	@echo "  make up            Démarre app + worker + redis (docker compose)"
	@echo "  make down          Arrête la stack"
	@echo "  make build         Build l'image Docker"
	@echo "  make verify        Lance les tests d'acceptation T1-T10"
	@echo "  make export-image  docker save -> $(EXPORT_FILE)"
	@echo "  make import-image  docker load <- $(EXPORT_FILE)"

up:
	docker compose up -d

down:
	docker compose down

build:
	docker compose build

# verify s'exécute dans un venv local si présent, sinon via python3.
# Les tests utilisent des fixtures synthétiques (aucun téléchargement).
verify:
	@if [ -d .venv ]; then . .venv/bin/activate && python -m pytest tests/ -v; \
	else python3 -m pytest tests/ -v; fi

test: verify

logs:
	docker compose logs -f

export-image:
	docker save $(IMAGE) -o $(EXPORT_FILE)
	@echo "Image exportée -> $(EXPORT_FILE)"

import-image:
	docker load -i $(EXPORT_FILE)
	@echo "Image importée depuis $(EXPORT_FILE)"

clean:
	rm -rf projects/*/build projects/*/artwork/*.pdf
