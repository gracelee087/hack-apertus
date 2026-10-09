.PHONY: run eval build

IMAGE ?= track2b-payment-agent
PORT ?= 8000
# pass .env to the container only if it exists (no key -> recorded Apertus responses are replayed)
ENV_FILE := $(if $(wildcard .env),--env-file .env,)

build:
	docker build -t $(IMAGE) .

# evaluate, then serve the reviewer web app on http://localhost:$(PORT)
run: build
	docker run --rm --init -p $(PORT):8000 $(ENV_FILE) $(IMAGE)

# evaluate only, print the results table and exit
eval: build
	docker run --rm $(ENV_FILE) $(IMAGE) python -m agent eval
