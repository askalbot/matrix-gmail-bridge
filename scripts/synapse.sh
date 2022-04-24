#!/bin/bash
set -e
mkdir -p $PWD/.container_data/synapse

cp -r $PWD/scripts/synapse_configs/* $PWD/.container_data/synapse/
chmod -R 777 $PWD/.container_data/synapse || echo "$PWD/.container_data/synapse can't be chmod. Likely the permissions are changed by synapse container, so it should work. if it doesn't then 'chown' the directory before running the script  "


podman run --rm --name synapse \
	--net=host \
	-v $PWD/.container_data/synapse/:/data \
	docker.io/matrixdotorg/synapse:latest
